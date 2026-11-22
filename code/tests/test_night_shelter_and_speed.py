"""离线回归单测：放宽版夜间挖洞封口避难（NightShelter）、夜间策略门控与高 RTT 提速优化。"""
from __future__ import annotations

import time
import unittest
from typing import Any

from mcagent.client import Client, Config
from mcagent.loop.engine import _roadside_structure_blocks
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import Executor, Replan, RuntimeConfig, Safety, SafetyMonitor, State
from mcagent.skills import REGISTRY, contract_errors
from mcagent.skills import base as skill_base
from mcagent.skills.field import scan_blocks
from mcagent.skills.night_shelter import NightShelter, can_dig_night_shelter, is_night_shelter_window
from mcagent.skills.scripted_world import ScriptedWorld
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import reset_all_world_memos
from tests.test_d0_core import FakeResponse, FakeSession


class _ShelterGridWorld(GridWorld):
    """支持时间推进、get_blocks_batch 计数与下挖自动落地的离线世界夹具。"""

    def __init__(
        self,
        *,
        blocks: dict[tuple[int, int, int], str],
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        items: dict[str, int] | None = None,
        tod_seq: list[int] | None = None,
    ) -> None:
        super().__init__(blocks=blocks, pos=pos, items=items or {})
        self.st["dimension"] = "overworld"
        self.st["is_day"] = False
        self.st["time_of_day"] = (tod_seq[0] if tod_seq else 18000)
        self._tod_seq = list(tod_seq or [18000, 18200, 23600])
        self._tod_idx = 0
        self.batch_calls: int = 0
        self.batch_total_cells: int = 0

    def status(self, timeout: float = 10) -> dict:
        if self._tod_idx < len(self._tod_seq):
            tod = self._tod_seq[self._tod_idx]
            self._tod_idx += 1
        else:
            tod = self._tod_seq[-1]
        self.st["time_of_day"] = tod
        self.st["is_day"] = not (13000 <= tod % 24000 <= 23000)
        return super().status(timeout=timeout)

    def get_blocks_batch(self, positions, *, max_workers: int = 8, use_cache: bool = True) -> dict:
        self.batch_calls += 1
        pos_list = list(positions)
        self.batch_total_cells += len(pos_list)
        out = {}
        for p in pos_list:
            key = (int(p[0]), int(p[1]), int(p[2]))
            out[key] = self.get_block(list(key))
        return out

    def dig(self, pos, collect: bool = True, *, stop_on=None) -> dict:
        cell = (int(pos[0]), int(pos[1]), int(pos[2]))
        old_name = self.blocks.get(cell, "air")
        res = super().dig(pos, collect=collect, stop_on=stop_on)
        if collect and old_name in ("dirt", "grass_block"):
            self.items["dirt"] = self.items.get("dirt", 0) + 1
        elif collect and old_name in ("stone", "cobblestone"):
            self.items["cobblestone"] = self.items.get("cobblestone", 0) + 1
        # 模拟脚下挖空后自然落入坑内
        px, py, pz = self.st["pos"]
        if cell == (int(px // 1), int(py // 1) - 1, int(pz // 1)):
            self.st["pos"] = [px, py - 1.0, pz]
        return res


def _flat_terrain(*, y3_block: str = "dirt") -> dict[tuple[int, int, int], str]:
    blocks: dict[tuple[int, int, int], str] = {}
    for x in range(-5, 6):
        for z in range(-5, 6):
            blocks[(x, 63, z)] = "grass_block"
            blocks[(x, 62, z)] = "dirt"
            blocks[(x, 61, z)] = y3_block
            blocks[(x, 60, z)] = "stone"
            blocks[(x, 59, z)] = "stone"
    return blocks


def _make_state(
    *,
    items: dict[str, int] | None = None,
    is_day: bool = False,
    time_of_day: int | None = 18000,
    health: float = 20.0,
    food: float = 20.0,
    armor_points: float = 0.0,
    pos: tuple[float, float, float] = (20.5, 64.0, 20.5),
    nearest_hostile: dict[str, Any] | None = None,
) -> State:
    st = State()
    payload: dict[str, Any] = {
        "ready": True,
        "alive": True,
        "dimension": "overworld",
        "is_day": is_day,
        "health": health,
        "food": food,
        "armor_points": armor_points,
        "pos": list(pos),
    }
    if time_of_day is not None:
        payload["time_of_day"] = time_of_day
    if nearest_hostile is not None:
        payload["nearest_hostile"] = nearest_hostile
    st.set_status(payload)
    inv_items = [{"name": k, "count": v, "slot": i} for i, (k, v) in enumerate((items or {}).items()) if v > 0]
    st.set_inventory({"items": inv_items})
    return st


class NightShelterSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()

    def test_registered_and_contract_clean(self) -> None:
        self.assertIn("night_shelter", REGISTRY)
        self.assertEqual(REGISTRY["night_shelter"].verified, "live")
        self.assertEqual(contract_errors(NightShelter), [])

    def test_dig3_fill1_full_cycle_and_lazy_batch_budget(self) -> None:
        # 挖三填一完整闭环：预检 -> 下挖 3 格 -> 封顶 y-1 -> 洞内等待 -> 天亮拆顶并沿阶梯安全出坑
        w = _ShelterGridWorld(
            blocks=_flat_terrain(y3_block="dirt"),
            pos=(0.5, 64.0, 0.5),
            items={"dirt": 4},
            tod_seq=[18000, 18000, 18000, 18050, 18100, 18200, 23600, 23650],
        )
        ex, state = w.session()
        sk = NightShelter(max_wait_s=10, poll_interval_s=0.0)
        steps = drive(sk, ex, state)
        ops = [s["op"] for s in steps]
        self.assertIn("dig", ops)
        self.assertIn("place", ops)
        self.assertIn("shelter_wait", ops)
        self.assertIn("move_to", ops)
        # 懒加载批查：平地应远少于旧版 100 格
        self.assertLessEqual(w.batch_total_cells, 45)
        # 退出后 sealed_shelter 必然已清理且验证通过
        self.assertIsNone(state.get_sealed_shelter())
        self.assertEqual(sk.verify_done(state), "")
        self.assertGreaterEqual(w.st["pos"][1], 63.0)

    def test_dig2_anchor_fill1_empty_handed_on_stone_y3(self) -> None:
        # 空手无镐且 y-3 为石头：自动降级为挖二 + 侧锚点 + 封顶，不困死在坑里
        w = _ShelterGridWorld(
            blocks=_flat_terrain(y3_block="stone"),
            pos=(0.5, 64.0, 0.5),
            items={},
            tod_seq=[18000, 18000, 18000, 23600, 23650],
        )
        ex, state = w.session()
        sk = NightShelter(max_wait_s=10, poll_interval_s=0.0)
        steps = drive(sk, ex, state)
        place_steps = [s for s in steps if s["op"] == "place"]
        self.assertEqual(len(place_steps), 2, "dig2_anchor_fill1 should place anchor + cap")
        self.assertEqual(sk.verify_done(state), "")

    def test_alcove_mode_on_hillside(self) -> None:
        blocks = _flat_terrain(y3_block="stone")
        # 在 +X 方向构建 3×3×3 实心土山坡（可供 1×2×2 侧坡壁龛挖入）
        for x in (1, 2, 3):
            for y in (64, 65, 66):
                for z in (-1, 0, 1):
                    blocks[(x, y, z)] = "dirt"
        w = _ShelterGridWorld(
            blocks=blocks,
            pos=(0.5, 64.0, 0.5),
            items={"dirt": 4},
            tod_seq=[18000, 18000, 18000, 23600, 23650],
        )
        ex, state = w.session()
        sk = NightShelter(max_wait_s=10, poll_interval_s=0.0)
        steps = drive(sk, ex, state)
        place_steps = [s for s in steps if s["op"] == "place"]
        self.assertEqual(len(place_steps), 2, "alcove should seal 2 entrance blocks")
        self.assertEqual(sk.verify_done(state), "")

    def test_rejects_cave_void_below(self) -> None:
        blocks = _flat_terrain(y3_block="dirt")
        del blocks[(0, 60, 0)]  # y-4 悬空洞穴
        del blocks[(0, 61, 0)]  # y-3 悬空
        w = _ShelterGridWorld(blocks=blocks, pos=(0.5, 64.0, 0.5), items={"dirt": 4})
        ex, state = w.session()
        with self.assertRaises(Replan):
            drive(NightShelter(max_wait_s=10, poll_interval_s=0.0), ex, state)

    def test_sealed_shelter_suppresses_roof_mob_flee_and_breaks_on_hurt(self) -> None:
        st = _make_state(
            is_day=False,
            pos=(0.5, 61.0, 0.5),
            nearest_hostile={"id": 1, "name": "creeper", "dist": 2.2},
        )
        safety = Safety(RuntimeConfig())
        # 未封洞前，2.2m 苦力怕立即触发 urgent flee
        haz_open = safety.hazards(st)
        self.assertTrue(any(h["kind"] == "flee" and h["severity"] == "urgent" for h in haz_open))

        # 登记已封洞后，洞顶游荡苦力怕不得触发误逃生拆顶
        st.set_sealed_shelter({"pos": [0, 61, 0], "cap": [0, 63, 0], "kind": "dig3_fill1", "dim": "overworld", "entered_at": time.monotonic() - 2.0})
        haz_sealed = safety.hazards(st)
        self.assertFalse(any(h["kind"] == "flee" for h in haz_sealed))

        # 若洞内真的受击掉血，立即解除屏蔽恢复逃生
        st.last_hurt_at = time.monotonic()
        st.last_hurt_info = {"source": "mob_attack"}
        haz_hurt = safety.hazards(st)
        self.assertTrue(any(h["kind"] == "flee" for h in haz_hurt))


class RelaxedNightPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()

    def test_dusk_11900_triggers_night_shelter_before_mob_spawns(self) -> None:
        planner = RulePlanner(min_verified="live")
        st_dusk = _make_state(items={"stone_sword": 1, "stone_pickaxe": 1}, is_day=True, time_of_day=11900, armor_points=0.0)
        dec = planner.choose_survival_or_safe_mode(st_dusk)
        self.assertIsNotNone(dec)
        assert dec is not None and dec.skill is not None
        self.assertEqual(dec.skill.name, "night_shelter")

    def test_partial_armor_and_low_hp_relaxed_conditions(self) -> None:
        planner = RulePlanner(min_verified="live")
        # 穿半套甲（armor_points=6 < 10）夜间也触发挖洞
        st_partial = _make_state(items={"stone_sword": 1}, is_day=False, time_of_day=15000, armor_points=6.0)
        dec1 = planner.choose_survival_or_safe_mode(st_partial)
        self.assertIsNotNone(dec1)
        assert dec1 is not None and dec1.skill is not None
        self.assertEqual(dec1.skill.name, "night_shelter")

        # 即使穿全套铁甲（armor=15）+ 持盾，血量 <= 14 也允许挖洞避险
        st_low_hp = _make_state(items={"shield": 1, "iron_sword": 1}, is_day=False, time_of_day=15000, armor_points=15.0, health=13.0)
        dec2 = planner.choose_survival_or_safe_mode(st_low_hp)
        self.assertIsNotNone(dec2)
        assert dec2 is not None and dec2.skill is not None
        self.assertEqual(dec2.skill.name, "night_shelter")

    def test_mob_at_7_5m_prefers_dig_in_while_close_mob_flees_first(self) -> None:
        planner = RulePlanner(min_verified="live")
        # 7.5m 僵尸（介于 6.5m 与无甲逃跑半径 9m 之间）：优先直接挖洞封口而非盲跑拉更多怪
        st_mid = _make_state(
            items={"stone_sword": 1}, is_day=False, time_of_day=16000, armor_points=0.0,
            nearest_hostile={"name": "zombie", "dist": 7.5},
        )
        dec_mid = planner.choose_urgent_survival_or_safe_mode(st_mid)
        self.assertIsNotNone(dec_mid)
        assert dec_mid is not None and dec_mid.skill is not None
        self.assertEqual(dec_mid.skill.name, "night_shelter")

        # 4.0m 贴脸僵尸：必须先 emergency_escape 拉距，绝不原地挨打挖洞
        st_close = _make_state(
            items={"stone_sword": 1}, is_day=False, time_of_day=16000, armor_points=0.0,
            nearest_hostile={"name": "zombie", "dist": 4.0},
        )
        dec_close = planner.choose_urgent_survival_or_safe_mode(st_close)
        self.assertIsNotNone(dec_close)
        assert dec_close is not None and dec_close.skill is not None
        self.assertEqual(dec_close.skill.name, "emergency_escape")

    def test_early_sleep_window_12600_triggers_bed_before_13000(self) -> None:
        planner = RulePlanner(min_verified="live")
        st = _make_state(items={"white_bed": 1}, is_day=True, time_of_day=12600, armor_points=0.0)
        dec = planner.choose_survival_or_safe_mode(st)
        self.assertIsNotNone(dec)
        assert dec is not None and dec.skill is not None
        self.assertEqual(dec.skill.name, "sleep_in_bed")

    def test_underground_explore_fallback_skips_survey_island(self) -> None:
        planner = RulePlanner(min_verified="live")
        st_ug = _make_state(items={"stone_pickaxe": 1}, is_day=True, time_of_day=6000, pos=(100.5, 52.0, -50.5))
        sk, _, _ = planner._choose_explore_fallback(
            st_ug, scout_radius=36, survey_reason="survey", scout_reason="scout",
        )
        self.assertIsNotNone(sk)
        assert sk is not None
        self.assertEqual(sk.name, "scout_island")


class HighRttThroughputTests(unittest.TestCase):
    def test_scan_blocks_broad_census_skips_serial_get_block(self) -> None:
        world = ScriptedWorld(pos=(0.5, 64.0, 0.5))
        world.st["dimension"] = "overworld"
        world.queue("find_blocks", {
            "blocks": {
                "stone": {"count": 10, "nearest": [2, 64, 0], "dist": 2.0},
                "dirt": {"count": 10, "nearest": [3, 64, 0], "dist": 3.0},
                "oak_log": {"count": 5, "nearest": [4, 64, 0], "dist": 4.0},
            }
        })
        get_block_calls = 0
        orig_get_block = world.get_block

        def _counting_get_block(pos):
            nonlocal get_block_calls
            get_block_calls += 1
            return orig_get_block(pos)

        world.get_block = _counting_get_block
        ex, state = world.session()
        res = scan_blocks(ex, state, types=None, radius=32, limit=64)
        self.assertEqual(res["status"], "found")
        self.assertEqual(len(res["blocks"]), 3)
        self.assertEqual(get_block_calls, 0, "broad census scan_blocks(types=None) must not issue serial get_block")

    def test_client_rtt_adaptive_cache_ttl_and_batch_worker_cap(self) -> None:
        cfg = Config("https://mock.local/api/v1", "u", "p", token_file=None, block_cache_ttl_s=0.3)
        c = Client(cfg, log=lambda *_: None)
        c.token = "tok"
        self.assertAlmostEqual(c._effective_block_cache_ttl(), 0.3)

        c._record_rtt("get_block", 0.9)
        self.assertGreaterEqual(c.smoothed_rtt_s, 0.85)
        self.assertGreaterEqual(c._effective_block_cache_ttl(), 5.0)

        # 验证高 RTT 时 SafetyMonitor 自动放宽轮询退避
        st = State()
        st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "is_day": True, "time_of_day": 6000})
        mon = SafetyMonitor(c, st, Executor(c, st), continuous=True)
        self.assertGreaterEqual(mon._continuous_wait_s(), 0.8)

    def test_roadside_structure_blocks_excludes_invalid_glow_berries(self) -> None:
        blocks = _roadside_structure_blocks("overworld")
        self.assertNotIn("glow_berries", blocks)
        self.assertIn("cave_vines_plant", blocks)
        self.assertIn("cave_vines", blocks)


if __name__ == "__main__":
    unittest.main()
