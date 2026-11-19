"""v1.3.11 故障注入与恢复单测（verified: offline，零网络）。

覆盖四项 cherry-pick 能力及其边界：
1. `SafetyMonitor._check_support()` 与 `State.patch_action()` 的速度感知与状态清洗；
2. `Executor.stop()` 在 `released=False` 下的 `ApiError("busy")` 传播与 `emergency()` 拦截；
3. `State.last_hurt_source()` 的最新伤害源读取、`spawn`/`death` 清零与无效字段防御；
4. `nav.dig_with_recovery()`、`gather_wood`/`mine_stone` 集成及 `nav.py` 既有原语回归守护；
5. `_craft_with_space()` 与 `CraftGear` 的 `no_window` 遗忘工作台及 `inventory_full` 谨慎吃食腾位。
"""
from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, Optional
import unittest
from unittest.mock import Mock

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import KNOWN_TABLES, _craft_with_space, has_known_table
from mcagent.runtime import Executor, Replan, RuntimeConfig, SafetyMonitor, State
from mcagent.skills import make, nav
from mcagent.skills.base import _COOLDOWN, failure
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import voxel_cache_for
from mcagent.skills.world_memo import memo_for
from tests.test_loop_soak import RichGridWorld


class _MonitorFakeClient:
    def __init__(self) -> None:
        self.st: dict[str, Any] = {
            "ready": True,
            "alive": True,
            "health": 20,
            "pos": [0.5, 64.0, 0.5],
            "on_ground": True,
            "dimension": "overworld",
        }
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.block_calls: list[list[int]] = []
        self.stops = 0
        self.stop_result: Any = {"stopped": True, "released": True, "wait_ms": 0}
        self.evs: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self.st)

    def inventory(self) -> dict[str, Any]:
        return {"items": []}

    def events(self, **kwargs: Any) -> dict[str, Any]:
        evs, self.evs = self.evs, []
        return {"events": evs, "last_seq": kwargs.get("since", 0) + len(evs)}

    def get_block(self, pos: list[int]) -> dict[str, Any]:
        self.block_calls.append(list(pos))
        return {"name": self.blocks.get(tuple(pos), "air")}

    def stop(self) -> Any:
        self.stops += 1
        return self.stop_result


class _DigRecoveryWorld(RichGridWorld):
    """支持 `dig_unconfirmed` 注入、`rng` 记录与自定义状态钩子的离线测试世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        blocks: Optional[dict] = None,
        items: Optional[dict] = None,
    ) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st["dimension"] = "overworld"
        self.dig_errors: list[Any] = []
        self.move_calls: list[dict[str, Any]] = []
        self.on_move_to: Optional[Any] = None

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        if self.dig_errors:
            err = self.dig_errors.pop(0)
            if err is not None:
                self.calls.append(("dig", {"pos": list(pos), "error": getattr(err, "code", str(err))}))
                raise err
        return super().dig(pos, collect=collect, stop_on=stop_on)

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.move_calls.append({"x": x, "y": y, "z": z, "pos": list(pos) if pos is not None else None, "rng": rng})
        if self.on_move_to is not None:
            return self.on_move_to(pos=pos, rng=rng)
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)


class _CraftRecoveryWorld(RichGridWorld):
    """支持 `no_window`/`inventory_full` 故障注入与槽位/附魔/命名控制的测试世界。"""

    def __init__(
        self,
        pos=(0.5, 64.0, 0.5),
        blocks: Optional[dict] = None,
        items: Optional[dict] = None,
        *,
        inv_entries: Optional[list[dict[str, Any]]] = None,
        empty_slots: int = 10,
        food: int = 20,
    ) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st["dimension"] = "overworld"
        self.st["food"] = food
        self.empty_slots = empty_slots
        self.inv_entries: Optional[list[dict[str, Any]]] = copy.deepcopy(inv_entries)
        self.craft_errors: list[Any] = []

    def inventory(self, timeout: float = 10) -> dict:
        if self.inv_entries is None:
            base = super().inventory(timeout=timeout)
            base["empty_slots"] = self.empty_slots
            return base
        return {
            "items": [copy.deepcopy(e) for e in self.inv_entries if e.get("count", 0) > 0],
            "empty_slots": self.empty_slots,
        }

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        if self.craft_errors:
            err = self.craft_errors.pop(0)
            if err is not None:
                self.calls.append(("craft", {"item": item, "count": count, "error": getattr(err, "code", str(err))}))
                raise err
        return super().craft(item, count=count, stop_on=stop_on)

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        if self.inv_entries is None:
            before = self.items.get(item, 0)
            res = super().use_item(item, target=target, ms=ms, timeout_ms=timeout_ms, source=source, stop_on=stop_on)
            if before > 0 and self.items.get(item, 0) == 0:
                self.empty_slots += 1
            return res

        self.calls.append(("use_item", {"item": item}))
        for entry in self.inv_entries:
            if entry.get("name") == item and entry.get("count", 0) > 0:
                entry["count"] -= 1
                if entry["count"] == 0:
                    self.empty_slots += 1
                self.st["food"] = min(20, int(self.st.get("food", 20)) + 6)
                return {"used": item, "food": self.st["food"]}
        raise ApiError("missing_item", f"背包没有 {item}")


class SupportAndPatchActionTests(unittest.TestCase):
    def _new_monitor(self):
        c = _MonitorFakeClient()
        s = State(replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(c, s, log=lambda *_: None)
        mon = SafetyMonitor(c, s, ex)
        return c, s, ex, mon

    def _tick(self, c: _MonitorFakeClient, s: State, mon: SafetyMonitor, **status: Any) -> Optional[dict]:
        c.st.update(status)
        s.status_at = float("-inf")
        return mon.tick()

    def test_negative_velocity_off_ground_detects_falling_on_first_sample(self) -> None:
        for void_guard in (False, True):
            with self.subTest(void_guard=void_guard):
                c, s, _, mon = self._new_monitor()
                if void_guard:
                    s.set_risk_context(void_guard=True)
                self.assertIsNone(mon._last_y)
                self._tick(c, s, mon, pos=[0.5, 65.0, 0.5], on_ground=False, velocity=[0.0, -0.4, 0.0])
                self.assertEqual(c.stops, 1)
                self.assertIsNotNone(s.safety_intent)
                self.assertEqual(s.safety_intent["kind"], "void_risk")

    def test_negative_velocity_on_ground_does_not_misclassify_as_falling(self) -> None:
        # 普通干地落地残余负速度
        c, s, _, mon = self._new_monitor()
        self._tick(c, s, mon, pos=[0.5, 64.0, 0.5], on_ground=True, velocity=[0.0, -0.5, 0.0])
        self.assertEqual(c.stops, 0)
        self.assertIsNone(s.safety_intent)
        self.assertEqual(s.get_support(), {})

        # void_guard=True 下单格桥 AABB 外探支撑同样不能误判坠落
        c2, s2, _, mon2 = self._new_monitor()
        s2.set_risk_context(void_guard=True)
        c2.blocks = {(-47, 63, 102): "air", (-46, 63, 102): "dirt"}
        self._tick(
            c2,
            s2,
            mon2,
            pos=[-46.3, 64.0, 102.5],
            on_ground=True,
            sneaking=False,
            velocity=[0.0, -0.5, 0.0],
        )
        self.assertEqual(c2.stops, 0)
        self.assertIsNone(s2.safety_intent)
        self.assertEqual(s2.support.get("aabb_support"), {"pos": [-46, 63, 102], "name": "dirt"})

    def test_positive_velocity_off_ground_under_void_guard_skips_block_query(self) -> None:
        c, s, _, mon = self._new_monitor()
        s.set_risk_context(void_guard=True)
        self._tick(c, s, mon, pos=[0.5, 64.2, 0.5], on_ground=False, velocity=[0.0, 0.42, 0.0])
        self.assertEqual(c.block_calls, [])
        self.assertEqual(c.stops, 0)
        self.assertIsNone(s.safety_intent)

    def test_missing_or_malformed_velocity_falls_back_to_prev_y_deltas(self) -> None:
        bad_velocities: list[Any] = [
            "ABSENT",
            None,
            [0.0, float("nan"), 0.0],
            [0.0, float("inf"), 0.0],
            [0.0, float("-inf"), 0.0],
            [0.0, -2.0],
            [0.0, True, 0.0],
            "invalid",
        ]
        for bad_vel in bad_velocities:
            with self.subTest(velocity=bad_vel):
                # 1) void_guard=True 起跳上升：y - prev_y > 0.05 -> 跳过 get_block
                c_up, s_up, _, mon_up = self._new_monitor()
                s_up.set_risk_context(void_guard=True)
                c_up.blocks[(0, 63, 0)] = "stone"
                if bad_vel != "ABSENT":
                    c_up.st["velocity"] = bad_vel
                self._tick(c_up, s_up, mon_up, pos=[0.5, 64.0, 0.5], on_ground=True)
                c_up.block_calls.clear()
                self._tick(c_up, s_up, mon_up, pos=[0.5, 64.5, 0.5], on_ground=False)
                self.assertEqual(c_up.block_calls, [])
                self.assertEqual(c_up.stops, 0)

                # 2) 普通地形下落：首帧 prev_y is None 不误报；次帧 prev_y - y >= void_fall_delta 才报 void_risk
                c_down, s_down, _, mon_down = self._new_monitor()
                if bad_vel != "ABSENT":
                    c_down.st["velocity"] = bad_vel
                self._tick(c_down, s_down, mon_down, pos=[0.5, 65.0, 0.5], on_ground=False)
                self.assertEqual(c_down.stops, 0)
                self._tick(c_down, s_down, mon_down, pos=[0.5, 64.0, 0.5], on_ground=False)
                self.assertEqual(c_down.stops, 1)
                self.assertEqual(s_down.safety_intent["kind"], "void_risk")

    def test_patch_action_clears_speed_and_velocity_except_place(self) -> None:
        s = State()
        s.set_status({
            "pos": [0, 64, 0],
            "health": 20,
            "speed": 1.25,
            "velocity": [0.1, -0.3, 0.0],
        })
        before_at = s.status_at

        s.patch_action({"pos": [2, 64, 0]}, action="place")
        self.assertEqual(s.pos, [0, 64, 0])
        self.assertEqual(s.st["speed"], 1.25)
        self.assertEqual(s.st["velocity"], [0.1, -0.3, 0.0])
        self.assertEqual(s.status_at, before_at)

        s.patch_action({"pos": [2, 64, 0]})
        self.assertEqual(s.pos, [2, 64, 0])
        self.assertNotIn("speed", s.st)
        self.assertNotIn("velocity", s.st)
        self.assertEqual(s.status_at, before_at)


class ExecutorStopTests(unittest.TestCase):
    def test_stop_unreleased_raises_busy_and_blocks_emergency_escape(self) -> None:
        c = _MonitorFakeClient()
        c.stop_result = {"stopped": True, "released": False, "wait_ms": 5000}
        logs: list[str] = []
        ex = Executor(c, State(), log=logs.append)

        with self.assertRaises(ApiError) as ctx:
            ex.stop(raise_on_error=True)
        self.assertEqual(ctx.exception.code, "busy")
        self.assertEqual(ctx.exception.message, "/stop 等待后动作仍未退出")
        self.assertEqual(ctx.exception.extra.get("wait_ms"), 5000)

        escape_fn = Mock(return_value="escaped")
        with self.assertRaises(ApiError) as em_ctx:
            ex.emergency("escape", escape_fn)
        self.assertEqual(em_ctx.exception.code, "busy")
        self.assertEqual(em_ctx.exception.message, "/stop 等待后动作仍未退出")
        escape_fn.assert_not_called()

        logs.clear()
        ex.stop()
        self.assertTrue(any("失败" in line for line in logs))

    def test_stop_released_legacy_and_none_allow_emergency_escape(self) -> None:
        for resp in ({"stopped": True, "released": True}, {"stopped": True}, None):
            with self.subTest(stop_result=resp):
                c = _MonitorFakeClient()
                c.stop_result = resp
                ex = Executor(c, State(), log=lambda *_: None)
                ex.stop(raise_on_error=True)
                res = ex.emergency("escape", lambda _: "escaped")
                self.assertEqual(res, "escaped")
                self.assertEqual(c.stops, 2)


class LastHurtSourceTests(unittest.TestCase):
    def _consume(self, s: State, events: list[dict[str, Any]]) -> None:
        c = _MonitorFakeClient()
        c.evs = list(events)
        s.drain(c)

    def test_returns_latest_hurt_source_and_resets_on_spawn_or_death(self) -> None:
        s = State()
        self.assertIsNone(s.last_hurt_source())

        self._consume(s, [
            {"seq": 1, "type": "hurt", "source": "fall"},
            {"seq": 2, "type": "hurt", "source": "drown"},
            {"seq": 3, "type": "chat", "text": "hi"},
        ])
        self.assertEqual(s.last_hurt_source(), "drown")

        self._consume(s, [{"seq": 4, "type": "hurt", "source": "in_fire"}])
        self.assertEqual(s.last_hurt_source(), "in_fire")

        for reset_type in ("spawn", "death"):
            with self.subTest(reset_type=reset_type):
                st = State()
                self._consume(st, [
                    {"seq": 1, "type": "hurt", "source": "fall"},
                    {"seq": 2, "type": reset_type},
                ])
                self.assertIsNone(st.last_hurt_source())

    def test_invalid_missing_or_stale_hurt_source_returns_none(self) -> None:
        for hurt_ev in (
            {"seq": 1, "type": "hurt"},
            {"seq": 1, "type": "hurt", "source": None},
            {"seq": 1, "type": "hurt", "source": ""},
            {"seq": 1, "type": "hurt", "source": 123},
        ):
            with self.subTest(hurt_ev=hurt_ev):
                s = State()
                self._consume(s, [hurt_ev])
                self.assertIsNone(s.last_hurt_source())

        # 旧 hurt 有 source，但更新的 hurt 没有 source 时，不能回退返回旧值
        s = State()
        self._consume(s, [
            {"seq": 1, "type": "hurt", "source": "fall"},
            {"seq": 2, "type": "hurt"},
        ])
        self.assertIsNone(s.last_hurt_source())


class DigRecoveryAndNavGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def _base_world(self) -> _DigRecoveryWorld:
        return _DigRecoveryWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "grass_block",
                (1, 63, 0): "grass_block",
                (2, 63, 0): "grass_block",
                (2, 64, 0): "oak_log",
            },
        )

    def test_dig_unconfirmed_shifts_to_safe_footing_and_retries_once(self) -> None:
        world = self._base_world()
        world.dig_errors = [ApiError("dig_unconfirmed", "block still exists")]
        ex, state = world.session()

        steps = drive(nav.dig_with_recovery(ex, state, [2, 64, 0]), ex)
        self.assertEqual(ex.acts, ["dig", "move_to", "dig"])
        self.assertEqual([s["op"] for s in steps], ["dig", "move_to", "dig"])
        self.assertEqual(steps[0]["error_code"], "dig_unconfirmed")
        self.assertEqual(world.move_calls, [{"x": None, "y": None, "z": None, "pos": [1.5, 64.0, 0.5], "rng": 0.35}])
        self.assertNotIn((2, 64, 0), world.blocks)

    def test_second_dig_unconfirmed_raises_replan_without_infinite_retry(self) -> None:
        world = self._base_world()
        world.dig_errors = [
            ApiError("dig_unconfirmed", "first unconfirmed"),
            ApiError("dig_unconfirmed", "second unconfirmed"),
        ]
        ex, state = world.session()

        with self.assertRaises(Replan) as ctx:
            drive(nav.dig_with_recovery(ex, state, [2, 64, 0]), ex)
        self.assertEqual(failure(ctx.exception)[0], "dig_unconfirmed")
        self.assertEqual(ex.acts, ["dig", "move_to", "dig"])

    def test_unsafe_preconditions_raise_dig_unconfirmed_without_move(self) -> None:
        cases = (
            "no_adjacent_footing",
            "sneaking",
            "in_water",
            "in_lava",
            "not_on_ground",
            "unsupported_current_footing",
            "target_already_air",
        )
        for case in cases:
            with self.subTest(case=case):
                world = self._base_world()
                world.dig_errors = [ApiError("dig_unconfirmed", "unconfirmed")]
                if case == "no_adjacent_footing":
                    world.blocks.pop((1, 63, 0), None)
                elif case == "sneaking":
                    world.st["sneaking"] = True
                elif case == "in_water":
                    world.st["in_water"] = True
                elif case == "in_lava":
                    world.st["in_lava"] = True
                elif case == "not_on_ground":
                    world.st["on_ground"] = False
                elif case == "unsupported_current_footing":
                    world.blocks.pop((0, 63, 0), None)
                elif case == "target_already_air":
                    orig_get_block = world.get_block
                    calls = {"n": 0}

                    def get_block_air(p, orig=orig_get_block, state_c=calls):
                        if list(p) == [2, 64, 0]:
                            state_c["n"] += 1
                            return {"name": "air"}
                        return orig(p)

                    world.get_block = get_block_air  # type: ignore[method-assign]

                ex, state = world.session()
                with self.assertRaises(Replan) as ctx:
                    drive(nav.dig_with_recovery(ex, state, [2, 64, 0]), ex)
                self.assertEqual(failure(ctx.exception)[0], "dig_unconfirmed")
                self.assertEqual(ex.acts, ["dig"])

    def test_post_move_guards_abort_without_second_dig(self) -> None:
        for guard_case in ("short_move", "dimension_changed", "target_changed"):
            with self.subTest(guard_case=guard_case):
                world = self._base_world()
                world.dig_errors = [ApiError("dig_unconfirmed", "unconfirmed")]

                def bad_move(pos=None, rng=1, mode=guard_case, w=world):
                    if mode == "short_move":
                        w.st["pos"] = [1.0, 64.0, 0.5]  # 距 [1.5, 64.0, 0.5] 为 0.5 > 0.35
                    elif mode == "dimension_changed":
                        w.st["pos"] = list(pos)
                        w.st["dimension"] = "the_nether"
                    elif mode == "target_changed":
                        w.st["pos"] = list(pos)
                        w.blocks[(2, 64, 0)] = "cobblestone"
                    return {"pos": list(w.st["pos"])}

                world.on_move_to = bad_move
                ex, state = world.session()
                with self.assertRaises(Replan) as ctx:
                    drive(nav.dig_with_recovery(ex, state, [2, 64, 0]), ex)
                self.assertEqual(failure(ctx.exception)[0], "dig_unconfirmed")
                self.assertEqual(ex.acts, ["dig", "move_to"])

    def test_non_dig_unconfirmed_error_propagates_without_shifting(self) -> None:
        world = self._base_world()
        world.dig_errors = [ApiError("protected_region", "spawn protection")]
        ex, state = world.session()

        with self.assertRaises(Replan) as ctx:
            drive(nav.dig_with_recovery(ex, state, [2, 64, 0]), ex)
        self.assertEqual(failure(ctx.exception)[0], "protected_region")
        self.assertEqual(ex.acts, ["dig"])

    def test_gather_wood_and_mine_stone_recover_from_dig_unconfirmed(self) -> None:
        wood_world = self._base_world()
        wood_world.dig_errors = [ApiError("dig_unconfirmed", "retry from closer footing")]
        ex_w, state_w = wood_world.session()
        drive(make("gather_wood", {"count": 1}), ex_w, state_w)
        state_w.refresh_inventory(wood_world)
        self.assertEqual(state_w.count("oak_log"), 1)
        self.assertEqual(ex_w.acts, ["dig", "move_to", "dig"])

        stone_world = _DigRecoveryWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (0, 63, 0): "grass_block",
                (1, 63, 0): "grass_block",
                (2, 63, 0): "grass_block",
                (2, 64, 0): "stone",
            },
            items={"wooden_pickaxe": 1},
        )
        stone_world.st["held"] = {"name": "wooden_pickaxe"}
        stone_world.dig_errors = [ApiError("dig_unconfirmed", "retry from closer footing")]
        ex_s, state_s = stone_world.session()
        drive(make("mine_stone", {"count": 1}), ex_s, state_s)
        state_s.refresh_inventory(stone_world)
        self.assertEqual(state_s.count("cobblestone"), 1)
        self.assertEqual(ex_s.acts, ["dig", "move_to", "dig"])

    def test_nav_constants_and_existing_primitives_regression_guard(self) -> None:
        self.assertEqual(nav.PLACE_REACH, 3.5)
        self.assertEqual(nav.SELF_OVERLAP, 0.81)
        self.assertEqual(nav.PLAYER_HEIGHT, 1.8)

        w_breach = GridWorld(
            blocks={(1, 65, 0): "stone", (1, 64, 0): "stone"},
            items={"wooden_pickaxe": 1, "stone_pickaxe": 1},
        )
        ex_b, st_b = w_breach.session()
        breach_steps = drive(nav.breach_1x2(ex_b, st_b, [[1, 65, 0], [1, 64, 0], [1, 66, 0]]), ex_b)
        self.assertEqual([s["op"] for s in breach_steps], ["equip", "dig", "dig"])

        w_bridge = GridWorld(items={"dirt": 3, "cobblestone": 8})
        ex_br, st_br = w_bridge.session()
        bridge_steps = drive(nav.sneak_bridge(ex_br, st_br, [[x, 63, 0] for x in range(1, 6)]), ex_br)
        self.assertEqual(
            [s["op"] for s in bridge_steps],
            ["posture", "place", "place", "place", "move_to_bridge", "place", "place", "posture"],
        )

        w_step = GridWorld(items={"dirt": 5})
        ex_su, st_su = w_step.session()
        step_steps = drive(nav.step_up_1x2(ex_su, st_su, [[1, 64, 0], [0, 64, 0], [0, 65, 0]], "dirt"), ex_su)
        self.assertEqual([s["op"] for s in step_steps], ["place", "step_up_to", "place", "place"])


class CraftSpaceAndNoWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self) -> None:
        reset_world_memo()
        _COOLDOWN.clear()

    def test_no_window_with_extra_table_forgets_table_marks_memo_and_invalidates_vc(self) -> None:
        table_pos = [2, 64, 0]
        world = _CraftRecoveryWorld(blocks={(2, 64, 0): "crafting_table"}, items={"oak_planks": 3, "stick": 2})
        world.craft_errors = [ApiError("no_window", "table closed", {"table": table_pos})]
        ex, state = world.session()

        KNOWN_TABLES.append((2.0, 64.0, 0.0))
        memo = memo_for(state)
        memo.record_seen("station", "crafting_table", table_pos, dimension="overworld")
        vc = voxel_cache_for(state)
        vc.ingest_find_blocks({"blocks": [{"name": "crafting_table", "pos": table_pos}]}, dimension="overworld", client=world)

        with self.assertRaises(Replan) as ctx:
            drive(_craft_with_space(ex, state, "wooden_pickaxe"), ex)
        self.assertEqual(failure(ctx.exception)[0], "no_window")
        self.assertFalse(has_known_table(state.pos))
        self.assertTrue(memo.is_blocked(table_pos, dimension="overworld", category="station"))

        # 体素缓存必须已失效：再次查询会穿透到底层 client.get_block
        probe_client = Mock()
        probe_client.get_block = Mock(return_value={"name": "air"})
        probe_client._block_cache_gen = getattr(world, "_block_cache_gen", 0)
        vc.get_block(probe_client, table_pos, dimension="overworld")
        probe_client.get_block.assert_called_once_with(table_pos)

    def test_no_window_without_extra_table_forgets_active_table_in_craft_gear(self) -> None:
        table_pos = [2, 64, 0]
        world = _CraftRecoveryWorld(blocks={(2, 64, 0): "crafting_table"}, items={"oak_planks": 3, "stick": 2})
        world.craft_errors = [ApiError("no_window", "table closed without extra")]
        ex, state = world.session()
        vc = voxel_cache_for(state)
        vc.ingest_find_blocks({"blocks": [{"name": "crafting_table", "pos": table_pos}]}, dimension="overworld", client=world)

        with self.assertRaises(Replan) as ctx:
            drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        self.assertEqual(failure(ctx.exception)[0], "no_window")
        self.assertFalse(has_known_table(state.pos))
        self.assertTrue(memo_for(state).is_blocked(table_pos, dimension="overworld", category="station"))
        self.assertNotIn(("overworld", 2, 64, 0), vc._blocks)

    def test_inventory_full_eats_singleton_unenchanted_food_and_retries_once(self) -> None:
        world = _CraftRecoveryWorld(
            blocks={(2, 64, 0): "crafting_table"},
            items={"oak_planks": 3, "stick": 2, "bread": 1},
            empty_slots=0,
            food=16,
        )
        world.craft_errors = [ApiError("inventory_full", "backpack full")]
        ex, state = world.session()

        steps = drive(_craft_with_space(ex, state, "wooden_pickaxe", table_pos=[2, 64, 0]), ex)
        self.assertEqual(ex.acts, ["craft", "use_item", "craft"])
        self.assertEqual([s["op"] for s in steps], ["craft", "use_item", "craft"])
        state.refresh_inventory(world)
        self.assertEqual(state.count("wooden_pickaxe"), 1)
        self.assertEqual(state.count("bread"), 0)

    def test_inventory_full_refuses_protected_stacked_or_full_hunger_food(self) -> None:
        refuse_cases = (
            (
                "enchanted_food",
                16,
                [
                    {"name": "oak_planks", "count": 3, "slot": 0},
                    {"name": "stick", "count": 2, "slot": 1},
                    {"name": "bread", "count": 1, "slot": 2, "enchantments": [{"id": "unbreaking", "lvl": 1}]},
                ],
                1,
            ),
            (
                "custom_named_food",
                16,
                [
                    {"name": "oak_planks", "count": 3, "slot": 0},
                    {"name": "stick", "count": 2, "slot": 1},
                    {"name": "bread", "count": 1, "slot": 2, "custom_name": "Lucky Bread"},
                ],
                1,
            ),
            (
                "stacked_food",
                16,
                [
                    {"name": "oak_planks", "count": 3, "slot": 0},
                    {"name": "stick", "count": 2, "slot": 1},
                    {"name": "bread", "count": 2, "slot": 2},
                ],
                2,
            ),
            (
                "two_separate_stacks_same_food",
                16,
                [
                    {"name": "oak_planks", "count": 3, "slot": 0},
                    {"name": "stick", "count": 2, "slot": 1},
                    {"name": "bread", "count": 1, "slot": 2},
                    {"name": "bread", "count": 1, "slot": 3},
                ],
                2,
            ),
            (
                "full_hunger",
                20,
                [
                    {"name": "oak_planks", "count": 3, "slot": 0},
                    {"name": "stick", "count": 2, "slot": 1},
                    {"name": "bread", "count": 1, "slot": 2},
                ],
                1,
            ),
        )
        for label, food_val, entries, expected_bread in refuse_cases:
            with self.subTest(case=label):
                world = _CraftRecoveryWorld(
                    blocks={(2, 64, 0): "crafting_table"},
                    inv_entries=entries,
                    empty_slots=0,
                    food=food_val,
                )
                world.craft_errors = [ApiError("inventory_full", "backpack full")]
                ex, state = world.session()

                with self.assertRaises(Replan) as ctx:
                    drive(_craft_with_space(ex, state, "wooden_pickaxe"), ex)
                self.assertEqual(failure(ctx.exception)[0], "inventory_full")
                self.assertEqual(ex.acts, ["craft"])
                state.refresh_inventory(world)
                self.assertEqual(state.count("bread"), expected_bread)

    def test_inventory_full_retry_failure_raises_replan_without_looping(self) -> None:
        world = _CraftRecoveryWorld(
            blocks={(2, 64, 0): "crafting_table"},
            items={"oak_planks": 3, "stick": 2, "bread": 1},
            empty_slots=0,
            food=16,
        )
        world.craft_errors = [
            ApiError("inventory_full", "first full"),
            ApiError("inventory_full", "still full"),
        ]
        ex, state = world.session()

        with self.assertRaises(Replan) as ctx:
            drive(_craft_with_space(ex, state, "wooden_pickaxe"), ex)
        self.assertEqual(failure(ctx.exception)[0], "inventory_full")
        self.assertEqual(ex.acts, ["craft", "use_item", "craft"])

    def test_inventory_full_with_positive_gained_returns_without_eating(self) -> None:
        world = _CraftRecoveryWorld(
            blocks={(2, 64, 0): "crafting_table"},
            items={"oak_planks": 3, "stick": 2, "bread": 1},
            empty_slots=0,
            food=16,
        )
        world.craft_errors = [ApiError("inventory_full", "partial output", {"gained": 1})]
        ex, state = world.session()

        steps = drive(_craft_with_space(ex, state, "wooden_pickaxe"), ex)
        self.assertEqual(ex.acts, ["craft"])
        self.assertEqual([s["op"] for s in steps], ["craft"])
        state.refresh_inventory(world)
        self.assertEqual(state.count("bread"), 1)


if __name__ == "__main__":
    unittest.main()
