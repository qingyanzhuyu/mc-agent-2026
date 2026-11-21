"""Sprint 1.5 全链离线 TDD 单测（4 项 Sprint 1 缺陷修复 + 出生点防爆避难所 + 三区仓库 + 下界止损收紧）。

运行方式（在 code/ 目录下）：
    uv run python -m unittest tests/test_sprint15_shelter_and_fixes.py -v
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.client import ApiError
from mcagent.loop.engine import LoopConfig, MainLoop
from mcagent.loop.goals import (
    GOAL_MAP,
    GOALS,
    NETHER_END_HIGH_RISK_GOALS,
    survival_readiness_multiplier,
    validate_ledger,
)
from mcagent.loop.policy import CampaignPlanner, RulePlanner
from mcagent.loop.reserve import ReservationBook, bind_reservation_book
from mcagent.loop.starter_skills import PROTECTED_ZONES
from mcagent.runtime import Executor, RuntimeConfig, State
from mcagent.skills import REGISTRY, make
from mcagent.skills.base import Skill
from mcagent.skills.camp_base import (
    ManageCampChest,
    compute_deposit_plan,
    compute_withdraw_plan,
    has_surplus_for_deposit,
)
from mcagent.skills.camp_plan import classify_chest_role
from mcagent.skills.furnace_lease import lease_book_for, reset_all_furnace_leases
from mcagent.skills.items import bare
from mcagent.skills.spawn_shelter import BuildSpawnShelter, shelter_blueprint
from mcagent.skills.survival_food import (
    GatherFood,
    cooked_food_count,
    has_ready_food_lease,
    raw_meat_count,
)
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _flat_ground(cx: int, cy: int, cz: int, radius: int = 12, block: str = "grass_block") -> dict[tuple[int, int, int], str]:
    return {(cx + dx, cy - 1, cz + dz): block for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)}


class _Sprint15World(GridWorld):
    """支持多箱独立库存、装备槽位、保护区故障注入与异步延迟熔炉的离线测试世界。"""

    def __init__(
        self,
        *,
        pos=(0.5, 64.0, 0.5),
        dimension="overworld",
        blocks=None,
        items=None,
        containers=None,
        protected_cells=(),
        is_day=True,
        empty_slots=20,
    ):
        super().__init__(pos=pos, blocks=blocks or {}, items=items or {})
        self.st["dimension"] = dimension
        self.st["ready"] = True
        self.st["alive"] = True
        self.st["on_ground"] = True
        self.st["is_day"] = is_day
        self.st["time_of_day"] = 6000 if is_day else 15000
        self.containers: dict[tuple[int, int, int], dict[str, int]] = {
            tuple(k): dict(v) for k, v in (containers or {}).items()
        }
        self.furnace_in: dict[tuple[int, int, int], dict[str, int]] = {}
        self.furnace_out: dict[tuple[int, int, int], dict[str, int]] = {}
        self.furnace_fuel: dict[tuple[int, int, int], dict[str, int]] = {}
        self.protected_cells: set[tuple[int, int, int]] = {tuple(p) for p in protected_cells}
        self.equipped: dict[str, str] = {}
        self.empty_slots = empty_slots
        self.move_kwargs: list[dict] = []

    def find_blocks(self, *, types=None, radius=32, limit=16, timeout=10.0, **kwargs) -> dict:
        wanted = {bare(t) for t in (types or [])}
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in self.blocks.items():
            bname = bare(name)
            if wanted and bname not in wanted:
                continue
            dist = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if dist <= float(radius):
                hits.append({"name": bname, "pos": [bx, by, bz], "distance": round(dist, 2)})
        hits.sort(key=lambda h: h["distance"])
        return {"blocks": hits[: int(limit)]}

    def get_entities(self, *, radius=32, **kwargs) -> dict:
        return {"entities": []}

    def inventory(self, timeout: float = 10) -> dict:
        inv = super().inventory(timeout)
        inv["empty_slots"] = self.empty_slots
        inv["armor"] = {
            slot: {"name": item, "count": 1}
            for slot, item in self.equipped.items()
            if slot in ("head", "torso", "legs", "feet")
        }
        if "off-hand" in self.equipped:
            inv["offhand"] = {"name": self.equipped["off-hand"], "count": 1}
        return inv

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.move_kwargs.append({"pos": pos, "rng": rng, "timeout": timeout})
        return super().move_to(x=x, z=z, y=y, entity_id=entity_id, rng=rng, pos=pos, timeout=timeout, stop_on=stop_on)

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        cell = tuple(int(v) for v in pos)
        if cell in self.protected_cells:
            raise ApiError("protected_region", f"spawn protected at {cell}")
        res = super().place(item, pos, stop_on=stop_on)
        if bare(item) == "chest":
            self.containers.setdefault(cell, {})
        return res

    def equip(self, item, slot="hand", *, stop_on=None) -> dict:
        norm = {"chest": "torso", "offhand": "off-hand"}.get(slot, slot)
        if self.items.get(item, 0) < 1:
            raise ApiError("missing_item", f"no {item}")
        self.equipped[norm] = item
        self.calls.append(("equip", {"item": item, "slot": norm}))
        return {"equipped": item, "slot": norm}

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if item.endswith("_planks"):
            log = item.removesuffix("_planks") + "_log"
            self.items[log] = max(0, self.items.get(log, 0) - count)
            self.items[item] = self.items.get(item, 0) + 4 * count
            return {"crafted": item, "count": 4 * count}
        if item == "chest":
            for k in list(self.items):
                if k.endswith("_planks") and self.items[k] >= 8:
                    self.items[k] -= 8
                    break
            self.items["chest"] = self.items.get("chest", 0) + count
            return {"crafted": "chest", "count": count}
        if item == "shield":
            self.items["iron_ingot"] = max(0, self.items.get("iron_ingot", 0) - 1)
            for k in list(self.items):
                if k.endswith("_planks") and self.items[k] >= 6:
                    self.items[k] -= 6
                    break
            self.items["shield"] = self.items.get("shield", 0) + 1
            return {"crafted": "shield", "count": 1}
        return super().craft(item, count, stop_on=stop_on)

    def window_view(self, target, *, stop_on=None) -> dict:
        pos = tuple(int(v) for v in target["pos"])
        bname = bare(self.blocks.get(pos, "air"))
        self.calls.append(("window_view", {"target": target}))
        if bname in ("furnace", "smoker", "blast_furnace"):
            return {
                "window": bname,
                "sections": {
                    "input": [{"name": k, "count": v} for k, v in self.furnace_in.get(pos, {}).items() if v > 0],
                    "fuel": [{"name": k, "count": v} for k, v in self.furnace_fuel.get(pos, {}).items() if v > 0],
                    "output": [{"name": k, "count": v} for k, v in self.furnace_out.get(pos, {}).items() if v > 0],
                },
            }
        bag = self.containers.setdefault(pos, {})
        return {
            "window": "generic_9x3",
            "sections": {"container": [{"name": k, "count": v} for k, v in bag.items() if v > 0]},
        }

    def window_act(self, target, ops, *, stop_on=None) -> dict:
        pos = tuple(int(v) for v in target["pos"])
        bname = bare(self.blocks.get(pos, "air"))
        self.calls.append(("window_act", {"target": target, "ops": ops}))
        for op in ops:
            kind, item, cnt = op["op"], op.get("item", ""), int(op.get("count", 1) or 1)
            if bname in ("furnace", "smoker", "blast_furnace"):
                if kind == "put":
                    self.items[item] = max(0, self.items.get(item, 0) - cnt)
                    slot_map = self.furnace_fuel if op.get("to") == "fuel" else self.furnace_in
                    slot_map.setdefault(pos, {})[item] = slot_map.get(pos, {}).get(item, 0) + cnt
                elif kind == "take":
                    out_bag = self.furnace_out.setdefault(pos, {})
                    for k, v in list(out_bag.items()):
                        if (not item or k == item) and v > 0:
                            take = min(v, cnt)
                            out_bag[k] = v - take
                            self.items[k] = self.items.get(k, 0) + take
            else:
                bag = self.containers.setdefault(pos, {})
                if kind == "put":
                    take = min(cnt, self.items.get(item, 0))
                    self.items[item] = self.items.get(item, 0) - take
                    bag[item] = bag.get(item, 0) + take
                elif kind == "take":
                    take = min(cnt, bag.get(item, 0))
                    bag[item] = bag.get(item, 0) - take
                    self.items[item] = self.items.get(item, 0) + take
        return self.window_view(target)


def _make_state(*, items=None, pos=(0.5, 64.0, 0.5), dim="overworld", is_day=True, hp=20.0, food=20.0, empty_slots=20) -> State:
    st = State(RuntimeConfig())
    st.set_status({
        "ready": True, "alive": True, "health": hp, "food": food, "pos": list(pos),
        "dimension": dim, "is_day": is_day, "time_of_day": 6000 if is_day else 15000, "on_ground": True,
    })
    st.set_inventory({
        "items": [{"name": k, "count": v, "slot": i} for i, (k, v) in enumerate((items or {}).items()) if v > 0],
        "empty_slots": empty_slots,
    })
    return st


class Sprint15CoreFixesAndShelterTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        reset_all_furnace_leases()
        PROTECTED_ZONES.clear()

    def tearDown(self) -> None:
        reset_all_world_memos()
        reset_all_furnace_leases()
        PROTECTED_ZONES.clear()

    def test_p0_cold_start_auto_camp_setup_and_shield_with_logs(self) -> None:
        planner = RulePlanner(min_verified="offline")
        # 1) 石镐 + 熔炉 + 造箱（及工作台）木料且尚无营地箱 -> 自动派发 manage_camp_chest(mode="setup")
        st = _make_state(items={"stone_pickaxe": 1, "furnace": 1, "oak_log": 3, "cooked_beef": 4})
        dec = planner.choose_survival_or_safe_mode(st)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.skill.name, "manage_camp_chest")
        self.assertEqual(dec.skill.args["mode"], "setup")

        # 2) 建箱后不再重复 setup；有铁锭 + 原木(>=2根，0木板) -> 盾牌快路径生效且能自动拆板合盾
        memo_for(st).set_camp([0, 64, 0], chest=[1, 64, 0], stored_items={})
        st_shield = _make_state(items={"iron_pickaxe": 1, "iron_ingot": 1, "oak_log": 2, "cooked_beef": 4})
        memo_for(st_shield).set_camp([0, 64, 0], chest=[1, 64, 0], stored_items={})
        dec_sh = planner.choose_survival_or_safe_mode(st_shield)
        self.assertIsNotNone(dec_sh)
        self.assertEqual(dec_sh.skill.name, "craft_iron_gear")
        self.assertEqual(dec_sh.skill.args["item"], "shield")

        world = _Sprint15World(
            blocks={**_flat_ground(0, 64, 0), (1, 64, 0): "crafting_table"},
            items={"iron_pickaxe": 1, "iron_ingot": 1, "oak_log": 2},
        )
        ex, ws = world.session()
        drive(make("craft_iron_gear", {"item": "shield"}), ex, ws)
        self.assertEqual(world.items.get("shield", 0), 1)

    def test_p0_cook_raw_meat_uses_lease_and_retrieves_when_raw_meat_zero(self) -> None:
        fpos = (2, 64, 0)
        world = _Sprint15World(
            blocks={**_flat_ground(0, 64, 0), fpos: "furnace"},
            items={"beef": 2, "coal": 1},
            is_day=True,
        )
        ex, state = world.session()
        sleeps: list[float] = []
        with patch("mcagent.skills.survival_food.time.sleep", side_effect=sleeps.append):
            drive(GatherFood(target_restore=16, cook=True), ex, state)
        # 烤肉轮询必须真实等待（非零等待空转），且延迟未出炉时保留 FurnaceLease 防止遗弃
        self.assertGreater(len(sleeps), 0)
        book = lease_book_for(state)
        leases = book.active_leases(dimension="overworld")
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].expected_output, "cooked_beef")
        self.assertEqual(raw_meat_count(state), 0)

        # 模拟 20s 后熟肉烤熟且租约到期（即使背包 raw_meat_count == 0 且已是夜晚）
        world.furnace_in[fpos].clear()
        world.furnace_out[fpos] = {"cooked_beef": 2}
        leases[0].ready_at = 0.0
        world.st["is_day"] = False
        world.st["time_of_day"] = 16000
        state.set_status(world.status())
        self.assertTrue(has_ready_food_lease(state))
        self.assertEqual(GatherFood.blocked(state), "")

        planner = RulePlanner(min_verified="offline")
        dec = planner.choose_survival_or_safe_mode(state)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.skill.name, "gather_food")

        # 再次执行自动回炉取回熟肉并结清租约
        drive(GatherFood(target_restore=16, cook=True), ex, state)
        self.assertEqual(world.items.get("cooked_beef", 0), 2)
        self.assertEqual(cooked_food_count(state), 2)
        self.assertEqual(book.active_leases(dimension="overworld"), [])

        # P2 校验：有生肉但无燃料且无待取租约时，policy 不盲目触发烤肉
        st_nofuel = _make_state(items={"beef": 3, "stone_sword": 1, "bread": 3})
        memo_for(st_nofuel).record_station("furnace", fpos, dimension="overworld")
        self.assertIsNone(planner.choose_survival_or_safe_mode(st_nofuel))

    def test_p1_reservation_protection_deposit_and_three_zone_sorting(self) -> None:
        st = _make_state(
            items={"obsidian": 10, "diamond": 4, "iron_ingot": 12, "cooked_beef": 16, "cobblestone": 100},
            empty_slots=3,
        )
        book = ReservationBook()
        self.assertTrue(book.reserve(st, "goal:enter_the_nether", "obsidian", 10, mode="consume"))
        self.assertTrue(book.reserve(st, "goal:mine_diamond", "diamond", 3, mode="consume"))
        bind_reservation_book(st, book)

        plan = dict(compute_deposit_plan(st))
        self.assertNotIn("obsidian", plan, "预留的 10 黑曜石绝不能被存走")
        self.assertEqual(plan.get("diamond"), 1, "4 颗钻预留 3 颗后仅允许存 1 颗")
        self.assertEqual(plan.get("iron_ingot"), 8)
        self.assertEqual(plan.get("cooked_beef"), 4)
        self.assertEqual(plan.get("cobblestone"), 36, "empty_slots<=4 应触发大宗圆石卸货")
        self.assertTrue(has_surplus_for_deposit(st))

        # 三区分类规则验证
        self.assertEqual(classify_chest_role("cooked_beef"), "kit")
        self.assertEqual(classify_chest_role("iron_sword"), "kit")
        self.assertEqual(classify_chest_role("diamond"), "vault")
        self.assertEqual(classify_chest_role("obsidian"), "vault")
        self.assertEqual(classify_chest_role("cobblestone"), "bulk")

        # 三区分箱实存验证
        kit_pos, vault_pos, bulk_pos = (1, 64, 0), (-1, 64, 0), (0, 64, 2)
        world = _Sprint15World(
            blocks={**_flat_ground(0, 64, 0), kit_pos: "chest", vault_pos: "chest", bulk_pos: "chest"},
            items={"cooked_beef": 16, "diamond": 3, "cobblestone": 96},
            empty_slots=3,
        )
        ex, ws = world.session()
        memo_for(ws).set_camp([0, 64, 0], chests={"kit": kit_pos, "vault": vault_pos, "bulk": bulk_pos}, shelter_tier=2)
        drive(ManageCampChest(mode="deposit"), ex, ws)
        self.assertEqual(world.containers[kit_pos].get("cooked_beef", 0), 4)
        self.assertEqual(world.containers[vault_pos].get("diamond", 0), 3)
        self.assertEqual(world.containers[bulk_pos].get("cobblestone", 0), 32)

    def test_p1_withdraw_auto_equips_armor_shield_and_falls_back_to_materials(self) -> None:
        kit_pos = (1, 64, 0)
        world = _Sprint15World(
            blocks={**_flat_ground(0, 64, 0), kit_pos: "chest"},
            items={},
            containers={kit_pos: {"iron_chestplate": 1, "iron_boots": 1, "shield": 1, "iron_sword": 1, "cooked_beef": 8}},
        )
        ex, state = world.session()
        memo_for(state).set_camp([0, 64, 0], chest=kit_pos, stored_items=world.containers[kit_pos])
        drive(ManageCampChest(mode="withdraw"), ex, state)
        self.assertEqual(world.equipped.get("torso"), "iron_chestplate")
        self.assertEqual(world.equipped.get("feet"), "iron_boots")
        self.assertEqual(world.equipped.get("off-hand"), "shield")

        # 箱内无成品装备但有基础料：允许取出铁锭/圆石/木板/煤用于室内快造
        st_empty = _make_state(items={})
        mat_plan = dict(compute_withdraw_plan(st_empty, {"iron_ingot": 10, "cobblestone": 20, "oak_planks": 12, "coal": 8}))
        self.assertEqual(mat_plan.get("iron_ingot"), 6)
        self.assertEqual(mat_plan.get("cobblestone"), 12)
        self.assertEqual(mat_plan.get("oak_planks"), 8)
        self.assertEqual(mat_plan.get("coal"), 4)

    def test_p1_protected_region_retry_and_bounded_move_to(self) -> None:
        protected = {(dx, 64, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1)}
        world = _Sprint15World(
            blocks=_flat_ground(0, 64, 0, radius=16),
            items={"chest": 1},
            protected_cells=protected,
        )
        ex, state = world.session()
        drive(ManageCampChest(mode="setup"), ex, state)
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertIsNotNone(camp)
        self.assertIsNotNone(camp["chest"])
        self.assertNotIn(tuple(camp["chest"]), protected)
        self.assertTrue(len(PROTECTED_ZONES) >= 1)
        self.assertTrue(all(k["timeout"] == 30.0 for k in world.move_kwargs))

    def test_tier1_spawn_shelter_and_nether_bunker_with_chest_headroom(self) -> None:
        self.assertIn("build_spawn_shelter", REGISTRY)
        blocks = _flat_ground(0, 64, 0, radius=14)
        # 在箱子上方 cy+1 故意放一块杂草，验证建造时会清空箱顶保持 AIR
        bp = shelter_blueprint(0, 64, 0)
        blocks[bp["chest_headroom"][0]] = "short_grass"
        world = _Sprint15World(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={
                "cobblestone": 64,
                "chest": 3,
                "furnace": 1,
                "crafting_table": 1,
                "torch": 2,
                "white_bed": 1,
                "oak_door": 1,
            },
        )
        ex, state = world.session()
        memo_for(state).set_spawn([0, 64, 0], dimension="overworld")
        skill = BuildSpawnShelter(mode="tier1_shelter")
        drive(skill, ex, state)
        self.assertEqual(skill.verify_done(state), "")
        camp = memo_for(state).get_camp(dimension="overworld")
        self.assertGreaterEqual(camp["shelter_tier"], 1)
        self.assertIsNotNone(camp["mine_entry"])
        self.assertEqual(set(camp["chests"].keys()), {"kit", "vault", "bulk"})
        # 校验所有箱子上方 y+1 严格为 AIR，且任意两箱非相邻（防合大箱）
        chest_cells = [tuple(p) for p in camp["chests"].values()]
        for cx, cy, cz in chest_cells:
            self.assertEqual(bare(world.blocks.get((cx, cy + 1, cz), "air")), "air")
        for i, a in enumerate(chest_cells):
            for b in chest_cells[i + 1 :]:
                self.assertGreater(abs(a[0] - b[0]) + abs(a[2] - b[2]), 1)

        # 下界门 3x3 圆石碉堡验证：不破坏/覆盖 nether_portal 与 obsidian，落箱登记 nether_portal_depot
        nblocks = _flat_ground(0, 64, 0, radius=8, block="netherrack")
        nblocks[(0, 64, 0)] = "nether_portal"
        nblocks[(0, 65, 0)] = "nether_portal"
        nblocks[(2, 64, 0)] = "obsidian"
        nworld = _Sprint15World(
            pos=(1.5, 64.0, 1.5),
            dimension="the_nether",
            blocks=nblocks,
            items={"cobblestone": 32, "chest": 1},
        )
        nex, nstate = nworld.session()
        nskill = BuildSpawnShelter(mode="nether_bunker")
        drive(nskill, nex, nstate)
        self.assertEqual(nskill.verify_done(nstate), "")
        self.assertEqual(nworld.blocks[(0, 64, 0)], "nether_portal")
        self.assertEqual(nworld.blocks[(2, 64, 0)], "obsidian")
        self.assertTrue(memo_for(nstate).has_landmark("nether_portal_depot", dimension="the_nether"))

    def test_respawn_three_step_no_naked_corpse_recovery(self) -> None:
        planner = RulePlanner(min_verified="offline")
        # 裸装无营地箱：绝不允许裸奔捡尸
        st_naked = _make_state(items={}, pos=(0.5, 64.0, 0.5), hp=20.0)
        memo_for(st_naked).record_death([20, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1})
        dec_naked = planner.choose_survival_or_safe_mode(st_naked)
        self.assertTrue(dec_naked is None or dec_naked.skill.name != "manage_camp_chest" or dec_naked.skill.args["mode"] != "recover_corpse")

        # 裸装且营地有热备箱：Step 1 先开箱重装（withdraw），绝不跳过开箱直接捡尸
        memo_for(st_naked).set_camp([0, 64, 0], chest=[1, 64, 0], stored_items={"iron_sword": 1, "cooked_beef": 8})
        dec_step1 = planner.choose_survival_or_safe_mode(st_naked)
        self.assertIsNotNone(dec_step1)
        self.assertEqual(dec_step1.skill.name, "manage_camp_chest")
        self.assertEqual(dec_step1.skill.args["mode"], "withdraw")

        # 已武装（持剑 + 熟食）：Step 2 允许受控捡尸
        st_armed = _make_state(items={"iron_sword": 1, "cooked_beef": 4}, pos=(0.5, 64.0, 0.5), hp=20.0)
        memo_for(st_armed).record_death([20, 64, 0], cause="zombie", lost_items={"iron_pickaxe": 1})
        dec_step2 = planner.choose_survival_or_safe_mode(st_armed)
        self.assertIsNotNone(dec_step2)
        self.assertEqual(dec_step2.skill.name, "manage_camp_chest")
        self.assertEqual(dec_step2.skill.args["mode"], "recover_corpse")

    def test_world_memo_spawn_pos_checkpoint_and_nether_end_stop_loss(self) -> None:
        st = _make_state(pos=(12.3, 65.0, -7.8))
        memo = memo_for(st)
        memo.set_spawn([12.3, 65.0, -7.8], dimension="overworld")
        memo.set_spawn([99, 64, 99], dimension="overworld", overwrite=False)
        self.assertEqual(memo.spawn_pos, [12, 65, -8])
        memo.set_camp([20, 65, -8], chests={"kit": [19, 65, -9], "vault": [21, 65, -9]}, shelter_tier=1, mine_entry=[20, 64, -7], kit_depleted=True)
        ctx_sum = memo.summary_for_context(dimension="overworld")
        self.assertEqual(ctx_sum["camp_shelter_tier"], 1)
        self.assertEqual(ctx_sum["camp_chest_roles"], ["kit", "vault"])
        self.assertTrue(ctx_sum["has_spawn_anchor"])
        self.assertNotIn("12", json.dumps(ctx_sum))

        with tempfile.TemporaryDirectory() as tmp:
            cp = Path(tmp) / "memo.json"
            memo.save_checkpoint(cp)
            st2 = _make_state()
            memo2 = memo_for(st2)
            self.assertTrue(memo2.load_checkpoint(cp))
            self.assertEqual(memo2.spawn_pos, [12, 65, -8])
            c2 = memo2.get_camp(dimension="overworld")
            self.assertEqual(c2["shelter_tier"], 1)
            self.assertEqual(c2["mine_entry"], [20, 64, -7])
            self.assertTrue(c2["kit_depleted"])

        # 下界/末地止损收紧与热备保护验证
        self.assertEqual(validate_ledger(GOALS), [])
        self.assertEqual(GOAL_MAP["enter_the_nether"].budget_s, 120.0)
        self.assertEqual(GOAL_MAP["find_fortress"].budget_s, 180.0)
        self.assertEqual(GOAL_MAP["enter_the_end"].budget_s, 240.0)
        self.assertEqual(GOAL_MAP["find_end_city"].budget_s, 270.0)
        for gid in NETHER_END_HIGH_RISK_GOALS:
            self.assertTrue(GOAL_MAP[gid].stop_loss)
            self.assertLessEqual(GOAL_MAP[gid].max_fails_per_ep, 2)

        st_no_camp = _make_state(items={"iron_pickaxe": 1, "iron_chestplate": 1, "shield": 1, "iron_sword": 1, "cooked_beef": 10})
        m_no = survival_readiness_multiplier(GOAL_MAP["enter_the_nether"], GOAL_MAP["enter_the_nether"], st_no_camp, ())
        memo_for(st_no_camp).set_camp([0, 64, 0], chest=[1, 64, 0], shelter_tier=1)
        m_yes = survival_readiness_multiplier(GOAL_MAP["enter_the_nether"], GOAL_MAP["enter_the_nether"], st_no_camp, ())
        self.assertLess(m_no, m_yes)


if __name__ == "__main__":
    unittest.main()
