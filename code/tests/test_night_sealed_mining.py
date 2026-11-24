"""离线单测：夜间未睡眠转封口挖矿策略、自然矿洞避让与挖穿即堵、矿道井口封堵与回程自动拆封。"""
from __future__ import annotations

import time
import unittest

import mcagent.loop  # noqa: F401
from mcagent.loop.policy import RulePlanner
from mcagent.runtime import State
from mcagent.skills import base as skill_base
from mcagent.skills.mine_return import return_to_surface_from_mine
from mcagent.skills.mine_shaft import (
    branch_mine_level,
    column_touches_natural_cave,
    horizontal_step,
    navigate_to_shaft,
    plug_cave_breach,
    seal_shaft_entrance,
    unseal_shaft_entrance,
)
from mcagent.skills.mining import MineStepUnsafe, mine_step
from mcagent.skills.testing import GridWorld
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


class _TunnelWorld(GridWorld):
    """实心岩体地下世界（默认全石块，可局部挖空矿道或嵌入自然洞穴）。"""

    def __init__(self, *, pos=(0.5, 50.0, 0.5), overrides=None, items=None, surface_y=64):
        super().__init__(pos=pos, items=items or {"stone_pickaxe": 1, "cobblestone": 16})
        self.st["dimension"] = "overworld"
        self.st["on_ground"] = True
        self.st["held"] = {"name": "stone_pickaxe"}
        self.surface_y = surface_y
        self.overrides: dict[tuple[int, int, int], str] = dict(overrides or {})
        self.dug: list[tuple[int, int, int]] = []
        self.placed: list[tuple[str, tuple[int, int, int]]] = []

    def get_block(self, pos):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        if key in self.overrides:
            name = self.overrides[key]
            return {"name": name, "pos": list(key), "diggable": name not in ("air", "cave_air", "bedrock")}
        if key[1] >= self.surface_y:
            return {"name": "air", "pos": list(key), "diggable": False}
        return {"name": "stone", "pos": list(key), "diggable": True}

    def get_blocks_batch(self, positions, **kw):
        return {(int(p[0]), int(p[1]), int(p[2])): self.get_block(p) for p in positions}

    def find_blocks(self, types=None, radius=16, limit=8):
        return {"blocks": []}

    def equip(self, item, slot="hand", *, stop_on=None):
        self.st["held"] = {"name": item}
        return {"equipped": item, "slot": slot}

    def move_to(self, x=None, z=None, *, y=None, pos=None, rng=1, timeout=None, retries=None, stop_on=None, entity_id=None):
        if pos is not None:
            self.st["pos"] = [float(pos[0]), float(pos[1]), float(pos[2])]
        elif x is not None and z is not None:
            self.st["pos"] = [float(x), float(y if y is not None else self.st["pos"][1]), float(z)]
        return {"pos": list(self.st["pos"])}

    def dig(self, pos, collect=True, *, stop_on=None):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        old = self.get_block(key)["name"]
        self.overrides[key] = "air"
        self.dug.append(key)
        if collect and old == "stone":
            self.items["cobblestone"] = self.items.get("cobblestone", 0) + 1
        return {"dug": old, "pos": list(key)}

    def place(self, item, pos=None, *, stop_on=None):
        key = (int(pos[0]), int(pos[1]), int(pos[2]))
        self.overrides[key] = item
        self.items[item] = max(0, self.items.get(item, 0) - 1)
        self.placed.append((item, key))
        return {"placed": item, "pos": list(key)}


def _run_gen(gen):
    steps = []
    try:
        while True:
            steps.append(next(gen))
    except StopIteration as stop:
        return steps, stop.value


def _make_night_state(
    *,
    items=None,
    is_day=False,
    time_of_day=15000,
    health=20.0,
    food=20.0,
    armor_points=0.0,
    pos=(0.5, 64.0, 0.5),
    nearest_hostile=None,
    safe_mode=False,
) -> State:
    st = State()
    st.st = {
        "ready": True,
        "alive": True,
        "dimension": "overworld",
        "is_day": is_day,
        "time_of_day": time_of_day,
        "health": health,
        "food": food,
        "oxygen": 20.0,
        "armor_points": armor_points,
        "pos": list(pos),
        "on_ground": True,
        "in_water": False,
        "submerged": False,
        "in_lava": False,
        "on_fire": False,
        "nearest_hostile": nearest_hostile,
    }
    inv_items = [{"name": k, "count": v} for k, v in (items or {}).items() if v > 0]
    st.inventory = {"items": inv_items, "held": inv_items[0] if inv_items else None, "empty_slots": 20}
    st.inventory_dirty = False
    if safe_mode:
        st.safe_mode_until = time.monotonic() + 60.0
    return st


class CaveAvoidanceAndPluggingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_detects_cave_air_and_enclosed_air_pocket_without_false_positive_in_solid(self) -> None:
        # 1) 全实心矿道：玩家站在 (0, 50, 0)，自身净空 (0, 50..51, 0) 为 air
        overrides = {(0, 50, 0): "air", (0, 51, 0): "air"}
        w_solid = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides=overrides)
        ex, _ = w_solid.session()
        self.assertFalse(column_touches_natural_cave(ex, [(1, 50, 0), (1, 51, 0)], known_air=[(0, 50, 0), (0, 51, 0)], player_cell=(0, 50, 0)))

        # 2) 前方 (2, 50, 0) 为 cave_air -> 识别为紧邻自然矿洞
        w_cave_air = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides={**overrides, (2, 50, 0): "cave_air"})
        ex2, _ = w_cave_air.session()
        self.assertTrue(column_touches_natural_cave(ex2, [(1, 50, 0), (1, 51, 0)], known_air=[(0, 50, 0), (0, 51, 0)], player_cell=(0, 50, 0)))

        # 3) 封闭岩体内前方存在 2+ 连通 air 洞室 -> 识别为自然矿洞
        w_air_cave = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides={**overrides, (2, 50, 0): "air", (3, 50, 0): "air"})
        ex3, _ = w_air_cave.session()
        self.assertTrue(column_touches_natural_cave(ex3, [(1, 50, 0), (1, 51, 0)], known_air=[(0, 50, 0), (0, 51, 0)], player_cell=(0, 50, 0)))

    def test_plug_cave_breach_immediately_seals_dug_cell_and_mine_step_turns_away(self) -> None:
        # 挖破 (1, 50, 0) 后紧贴 (2, 50, 0) cave_air -> 立即用 cobblestone 回填并触发转向
        overrides = {
            (0, 50, 0): "air", (0, 51, 0): "air", (0, 52, 0): "air",
            (2, 49, 0): "cave_air", (2, 50, 0): "cave_air",
        }
        w = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides=overrides)
        ex, state = w.session()

        with self.assertRaises(MineStepUnsafe) as caught:
            list(mine_step(ex, state, [1, 49, 0]))
        self.assertEqual(caught.exception.stage, "cut")
        # 挖破的格被立即回填实心方块，没有继续踏入
        self.assertTrue(any(item == "cobblestone" for item, _ in w.placed))
        self.assertEqual(w.st["pos"], [0.5, 50.0, 0.5])

        # horizontal_step 同样支持挖穿即堵
        w2 = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides={(0, 50, 0): "air", (0, 51, 0): "air", (2, 51, 0): "cave_air"})
        ex2, st2 = w2.session()
        with self.assertRaises(MineStepUnsafe):
            list(horizontal_step(ex2, st2, [1, 50, 0], avoid_caves=False, plug_caves=True))
        self.assertEqual(w2.overrides.get((1, 51, 0)), "cobblestone")

    def test_branch_mine_level_steers_away_from_cave_direction(self) -> None:
        # +X 主干道前方存在自然矿洞 cave_air，鱼骨挖矿法应安全开凿左右支道且不得向 +X 挖入矿洞
        overrides = {
            (0, 50, 0): "air", (0, 51, 0): "air",
            (2, 50, 0): "cave_air", (2, 51, 0): "cave_air",
        }
        w = _TunnelWorld(pos=(0.5, 50.0, 0.5), overrides=overrides)
        ex, state = w.session()
        memo_for(state).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=50, lowest_pos=[0, 50, 0], heading=(1, 0))
        _run_gen(branch_mine_level(
            ex, state, level_y=50, target_ores=("iron_ore",), max_length=2, scan_stride=2, dimension="overworld",
        ))
        # 不得向 +X (1, 50, 0) 挖入矿洞，且左右支道均已开凿
        self.assertNotIn((1, 50, 0), w.dug)
        self.assertIn((0, 50, 1), w.dug)
        self.assertIn((0, 50, -1), w.dug)

    def test_herringbone_digs_16_left_and_16_right_then_advances_spine_by_3(self) -> None:
        # 默认鱼骨挖矿法：固定 Y=-58 层，左支道笔直 16 格 (+Z) -> 回主干道 -> 右支道笔直 16 格 (-Z) -> 主干道推进 3 格 (+X)
        overrides = {(0, -58, 0): "air", (0, -57, 0): "air"}
        w = _TunnelWorld(
            pos=(0.5, -58.0, 0.5),
            overrides=overrides,
            items={"iron_pickaxe": 1, "cobblestone": 16},
        )
        w.st["held"] = {"name": "iron_pickaxe"}
        ex, state = w.session()
        memo_for(state).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=-58, lowest_pos=[0, -58, 0], heading=(1, 0))

        steps1, hits1 = _run_gen(branch_mine_level(
            ex, state, level_y=-58, target_ores=("deepslate_diamond_ore",),
            allowed_picks=frozenset({"iron_pickaxe"}), dimension="overworld",
        ))
        self.assertEqual(hits1, [])
        dug_set = set(w.dug)
        # 左支道 z=1..16、右支道 z=-1..-16 全部开凿 1x2 通道，且未越界到 17
        for z in range(1, 17):
            self.assertIn((0, -58, z), dug_set)
            self.assertIn((0, -57, z), dug_set)
        for z in range(-16, 0):
            self.assertIn((0, -58, z), dug_set)
            self.assertIn((0, -57, z), dug_set)
        self.assertNotIn((0, -58, 17), dug_set)
        self.assertNotIn((0, -58, -17), dug_set)
        # 主干道推进 3 格到 x=3（保留 x=1, x=2 两格实心岩壁）
        for x in (1, 2, 3):
            self.assertIn((x, -58, 0), dug_set)
        self.assertEqual(w.st["pos"], [3.5, -58.0, 0.5])
        self.assertTrue(any(s.get("reason") == "herringbone_return_spine" for s in steps1))

        # 第二节鱼骨从 x=3 起步，左 16 + 右 16 与第一节间隔 2 格实心岩壁互不重叠，完成后推进到 x=6
        _, hits2 = _run_gen(branch_mine_level(
            ex, state, level_y=-58, target_ores=("deepslate_diamond_ore",),
            allowed_picks=frozenset({"iron_pickaxe"}), dimension="overworld",
        ))
        self.assertEqual(hits2, [])
        dug_set2 = set(w.dug)
        self.assertIn((3, -58, 16), dug_set2)
        self.assertIn((3, -58, -16), dug_set2)
        # 岩壁 x=1, x=2 在支道侧未被挖穿
        self.assertNotIn((1, -58, 5), dug_set2)
        self.assertNotIn((2, -58, -5), dug_set2)
        self.assertEqual(w.st["pos"], [6.5, -58.0, 0.5])

    def test_herringbone_stops_cave_rib_and_still_completes_opposite_16_rib(self) -> None:
        # 左支道 z=5 处为自然矿洞 cave_air -> 左支道在 z=3 处安全止步不打穿，回主干道后仍完整挖完右支道 16 格
        overrides = {
            (0, 16, 0): "air", (0, 17, 0): "air",
            (0, 16, 5): "cave_air", (0, 17, 5): "cave_air",
        }
        w = _TunnelWorld(pos=(0.5, 16.0, 0.5), overrides=overrides)
        ex, state = w.session()
        memo_for(state).set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=16, lowest_pos=[0, 16, 0], heading=(1, 0))

        _run_gen(branch_mine_level(
            ex, state, level_y=16, target_ores=("iron_ore",), dimension="overworld",
        ))
        dug_set = set(w.dug)
        self.assertIn((0, 16, 3), dug_set)
        self.assertNotIn((0, 16, 4), dug_set)  # 紧邻 cave_air 的第 4 格未被挖穿
        for z in range(-16, 0):
            self.assertIn((0, 16, z), dug_set)
        self.assertEqual(w.st["pos"], [3.5, 16.0, 0.5])


class ShaftEntranceSealAndUnsealTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    @unittest.skip("封矿井口已按需求停用：mine_shaft.seal_shaft_entrance 现在直接返回 []，不再堵洞口")
    def test_seal_entrance_blocks_surface_and_auto_unseals_on_return_and_reentry(self) -> None:
        # 构造从地表 (0, 64, 0) 沿 +X 向下 3 级的阶梯矿道，玩家已下至 (3, 61, 0)
        overrides: dict[tuple[int, int, int], str] = {}
        for step in range(4):
            for dy in (0, 1, 2):
                overrides[(step, 64 - step + dy, 0)] = "air"
        w = _TunnelWorld(pos=(3.5, 61.0, 0.5), overrides=overrides, surface_y=64)
        ex, state = w.session()
        memo = memo_for(state)
        memo.set_mine_shaft([0, 64, 0], dimension="overworld", lowest_y=61, lowest_pos=[3, 61, 0], heading=(1, 0))

        # 1) 进入矿道后封堵地下与地面井口喉道
        seal_steps, sealed = _run_gen(seal_shaft_entrance(ex, state, dimension="overworld"))
        self.assertTrue(sealed)
        self.assertTrue(any(s.get("op") == "place" and s.get("reason") == "seal_shaft_entrance" for s in seal_steps))
        shaft = memo.get_mine_shaft(dimension="overworld")
        assert shaft is not None
        self.assertTrue(shaft["sealed"])
        self.assertTrue(len(shaft["seal_cells"]) >= 1)
        for cell in shaft["seal_cells"]:
            self.assertEqual(w.get_block(cell)["name"], "cobblestone")

        # 2) 从矿井内部返回地表：自动先拆封井口，再上到地表入口
        ret_steps, _ = _run_gen(return_to_surface_from_mine(ex, state))
        self.assertTrue(any(s.get("op") == "dig" and s.get("reason") == "unseal_shaft_entrance" for s in ret_steps))
        shaft_after_ret = memo.get_mine_shaft(dimension="overworld")
        assert shaft_after_ret is not None
        self.assertFalse(shaft_after_ret.get("sealed"))
        for cell in shaft["seal_cells"]:
            self.assertEqual(w.get_block(cell)["name"], "air")

        # 3) 再次封堵井口后，从地表侧调用 navigate_to_shaft 下井也会自动先拆封再下探
        w.st["pos"] = [3.5, 61.0, 0.5]
        _, sealed2 = _run_gen(seal_shaft_entrance(ex, state, dimension="overworld"))
        self.assertTrue(sealed2)
        w.st["pos"] = [0.5, 64.0, 0.5]
        nav_steps, _ = _run_gen(navigate_to_shaft(ex, state, target_y=61, dimension="overworld"))
        self.assertTrue(any(s.get("op") == "dig" and s.get("reason") == "unseal_shaft_entrance" for s in nav_steps))
        self.assertFalse((memo.get_mine_shaft(dimension="overworld") or {}).get("sealed", False))

        # 直接调用 unseal_shaft_entrance 对未封堵矿井幂等返回 False
        _, noop = _run_gen(unseal_shaft_entrance(ex, state, dimension="overworld"))
        self.assertFalse(noop)


class NightCheckMiningPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_all_world_memos()
        skill_base._COOLDOWN.clear()

    def test_night_priority_sleep_over_mining_over_emergency_shelter(self) -> None:
        planner = RulePlanner(min_verified="live")

        # 1) 有床且处于睡眠窗口 -> 仍优先睡觉
        st_bed = _make_night_state(items={"white_bed": 1, "iron_pickaxe": 1}, time_of_day=15000)
        dec_bed = planner.choose_survival_or_safe_mode(st_bed)
        assert dec_bed is not None and dec_bed.skill is not None
        self.assertEqual(dec_bed.skill.name, "sleep_in_bed")

        # 2) 夜间无床 + 铁镐 + 健康 -> 转入封口矿道采钻
        st_dia = _make_night_state(items={"iron_pickaxe": 1, "stone_sword": 1}, time_of_day=15000)
        dec_dia = planner.choose_survival_or_safe_mode(st_dia)
        assert dec_dia is not None and dec_dia.skill is not None
        self.assertEqual(dec_dia.skill.name, "mine_diamond")

        # 3) 夜间无床 + 石镐 + 熔炉 -> 转入封口矿道采铁冶炼
        st_iron = _make_night_state(items={"stone_pickaxe": 1, "furnace": 1, "coal": 4}, time_of_day=15000)
        dec_iron = planner.choose_survival_or_safe_mode(st_iron)
        assert dec_iron is not None and dec_iron.skill is not None
        self.assertEqual(dec_iron.skill.name, "smelt_iron")

        # 4) 夜间无床 + 木镐 -> 转入封口矿道采石
        st_wood = _make_night_state(items={"wooden_pickaxe": 1}, time_of_day=15000)
        dec_wood = planner.choose_survival_or_safe_mode(st_wood)
        assert dec_wood is not None and dec_wood.skill is not None
        self.assertEqual(dec_wood.skill.name, "mine_stone")

        # 5) 夜间无床 + 无镐 -> 退回应急挖洞避险
        st_empty = _make_night_state(items={"stone_sword": 1}, time_of_day=15000)
        dec_empty = planner.choose_survival_or_safe_mode(st_empty)
        assert dec_empty is not None and dec_empty.skill is not None
        self.assertEqual(dec_empty.skill.name, "night_shelter")

        # 6) 夜间无床 + 持镐但残血 (hp=9 <= 10, food=16 无法自然回血) -> 退回应急挖洞避险
        st_low_hp = _make_night_state(items={"stone_pickaxe": 1}, health=9.0, food=16.0, time_of_day=15000)
        dec_low = planner.choose_survival_or_safe_mode(st_low_hp)
        assert dec_low is not None and dec_low.skill is not None
        self.assertEqual(dec_low.skill.name, "night_shelter")

        # 7) 夜间无床 + 持镐但处于 safe_mode -> 退回应急挖洞避险
        st_safe = _make_night_state(items={"stone_pickaxe": 1}, safe_mode=True, time_of_day=15000)
        dec_safe = planner.choose_survival_or_safe_mode(st_safe)
        assert dec_safe is not None and dec_safe.skill is not None
        self.assertEqual(dec_safe.skill.name, "night_shelter")

        # 8) 夜间无床 + 持镐但敌怪已逼近至 7.5m -> 紧急直接挖洞封口而非开慢速阶梯
        st_mob = _make_night_state(
            items={"stone_pickaxe": 1},
            nearest_hostile={"name": "zombie", "dist": 7.5},
            time_of_day=15000,
        )
        dec_mob = planner.choose_survival_or_safe_mode(st_mob)
        assert dec_mob is not None and dec_mob.skill is not None
        self.assertEqual(dec_mob.skill.name, "night_shelter")


if __name__ == "__main__":
    unittest.main()
