"""考古与嗅探兽生态技能离线单测（verified: offline）。

使用 `skills.testing.drive()` 严格验证"一步一阻塞动作"契约，覆盖：
1. 合成刷子（备料与工作台合成）+ `blocked()` 提示；
2. 远程锚点导航刷陶片（含 `use_item` -> `interact` 降级与严禁挖掘可疑方块）；
3. 暖水海底废墟水下优先刷 `suspicious_sand` 获得嗅探兽蛋；
4. 水下氧气告警中断安全退出 & 上浮换气后继续刷取；
5. 放嗅探兽蛋孵化（优先 `moss_block` 加速孵化，普通方块兜底，记录锚点并抛 `Replan("waiting_sniffer_hatch")`）；
6. 锄地种火把花种子 & 复用耕地种瓶子草荚果；
7. 喂食幼年嗅探兽（含成对成年嗅探兽繁殖产幼体并喂食、以及仅服务端进度确认）；
8. `EnvBlocked` 边界、`skills.registry.load_all()` 自动发现与 `validate_ledger()` 契约核验。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import unittest
from typing import Any, Callable

from mcagent.client import ApiError
from mcagent.loop.draft_scores import ADVANCEMENT_SCORES_V2
from mcagent.loop.goals import GOAL_MAP, GOALS, validate_ledger
from mcagent.runtime import EnvBlocked, Replan, State
from mcagent.skills import REGISTRY, base as skill_base, bind_args, contract_errors, registry as skill_registry
from mcagent.skills.archaeology_sniffer import BrushArchaeology, HatchAndRaiseSniffer
from mcagent.skills.testing import GridWorld, drive
from mcagent.skills.voxel_cache import reset_all_voxel_caches
from mcagent.skills.world_memo import memo_for, reset_all_world_memos


def _flat(x0: int, x1: int, z0: int, z1: int, y: int = 63, block: str = "stone") -> dict[tuple[int, int, int], str]:
    return {(x, y, z): block for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


class ArchSnifferWorld(GridWorld):
    """支持考古刷取、水下氧气、合成、耕作与嗅探兽交互的离线世界替身。"""

    def __init__(
        self,
        *,
        pos: tuple[float, float, float] = (0.5, 64.0, 0.5),
        blocks: dict[tuple[int, int, int], str] | None = None,
        items: dict[str, int] | None = None,
        entities: list[dict[str, Any]] | None = None,
        oxygen: float | None = None,
        in_water: bool = False,
        submerged: bool = False,
        completed_advs: set[str] | None = None,
    ) -> None:
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.st.update(
            ready=True,
            alive=True,
            health=20.0,
            food=20.0,
            dimension="overworld",
            in_water=in_water,
            submerged=submerged,
        )
        if oxygen is not None:
            self.st["oxygen"] = float(oxygen)
        self.entities: list[dict[str, Any]] = [deepcopy(e) for e in (entities or ())]
        self.completed_advs: set[str] = set(completed_advs or ())
        self.on_move_to: Callable[..., dict | None] | None = None
        self.on_use_item: Callable[..., dict | None] | None = None
        self.on_interact: Callable[..., dict | None] | None = None
        self.on_craft: Callable[..., dict | None] | None = None

    def status(self, timeout: float = 10) -> dict:
        return deepcopy(self.st)

    def advancements(self, status: str = "completed", include_recipes: bool = False) -> dict:
        return {
            "ready": True,
            "advancements": [{"id": aid, "status": "completed"} for aid in sorted(self.completed_advs)],
        }

    def find_blocks(self, types=None, radius: int = 16, limit: int = 20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius, "limit": limit}))
        wanted = {types.removeprefix("minecraft:")} if isinstance(types, str) else {
            str(t).removeprefix("minecraft:") for t in (types or ())
        }
        px, py, pz = self.st["pos"]
        hits = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            bname = name.removeprefix("minecraft:")
            if wanted and bname not in wanted:
                continue
            d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
            if d <= radius + 0.5:
                hits.append({"name": bname, "pos": [bx, by, bz], "dist": round(d, 2)})
        hits.sort(key=lambda b: b["dist"])
        return {"blocks": hits[:limit]}

    def get_entities(self, radius: int = 32, types=None) -> dict:
        self.calls.append(("get_entities", {"radius": radius, "types": types}))
        wanted = {types.removeprefix("minecraft:")} if isinstance(types, str) else {
            str(t).removeprefix("minecraft:") for t in (types or ())
        }
        px, py, pz = self.st["pos"]
        out = []
        for ent in self.entities:
            ename = str(ent.get("name", "")).removeprefix("minecraft:")
            etype = str(ent.get("type", "")).removeprefix("minecraft:")
            if wanted and ename not in wanted and etype not in wanted:
                continue
            d = math.dist((px, py, pz), ent["pos"])
            if d <= radius + 0.5:
                row = deepcopy(ent)
                row.setdefault("dist", round(d, 2))
                out.append(row)
        return {"entities": out}

    def look(self, yaw=None, pitch=None, pos=None, entity_id=None) -> dict:
        self.calls.append(("look", {"pos": list(pos) if pos is not None else None, "entity_id": entity_id}))
        return {"ok": True}

    def move_to(self, x=None, z=None, y=None, entity_id=None, rng=1, pos=None, timeout=None, *, stop_on=None) -> dict:
        self.calls.append(("move_to", {"pos": list(pos) if pos is not None else None, "rng": rng}))
        if self.on_move_to is not None:
            res = self.on_move_to(self, pos=pos, rng=rng)
            if res is not None:
                return res
        dest = [float(v) for v in pos] if pos is not None else [
            float(x), float(self.st["pos"][1] if y is None else y), float(z)
        ]
        self.st["pos"] = dest
        # 自动拾取 1.6 格内掉落物实体
        kept = []
        for ent in self.entities:
            if ent.get("type") == "item" and math.dist(dest, ent["pos"]) <= 1.6:
                raw_item = str(ent.get("item", ""))
                iname = raw_item.rpartition("x")[0] if "x" in raw_item else raw_item
                if iname:
                    self.items[iname] = self.items.get(iname, 0) + 1
            else:
                kept.append(ent)
        self.entities = kept
        return {"pos": list(dest)}

    def craft(self, item: str, count: int = 1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if self.on_craft is not None:
            res = self.on_craft(self, item=item, count=count)
            if res is not None:
                return res
        if item == "brush":
            for ing in ("copper_ingot", "feather", "stick"):
                self._need(ing)
            for ing in ("copper_ingot", "feather", "stick"):
                self.items[ing] -= count
            self.items["brush"] = self.items.get("brush", 0) + count
            return {"crafted": "brush", "count": count}
        raise ApiError("missing_recipe", f"无配方: {item}")

    def use_item(self, item: str, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item, "target": deepcopy(target), "ms": ms}))
        self._need(item)
        if self.on_use_item is not None:
            res = self.on_use_item(self, item=item, target=target, ms=ms)
            if res is not None:
                return res
        return {"ok": True, "used": item}

    def interact(self, pos=None, entity_id=None, item=None, *, stop_on=None) -> dict:
        self.calls.append(("interact", {"pos": list(pos) if pos is not None else None, "entity_id": entity_id, "item": item}))
        if item is not None:
            self._need(item)
        if self.on_interact is not None:
            res = self.on_interact(self, pos=pos, entity_id=entity_id, item=item)
            if res is not None:
                return res
        return {"ok": True, "changed": True}


class _ArchSnifferTestBase(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()
        reset_all_voxel_caches()

    def tearDown(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_all_world_memos()
        reset_all_voxel_caches()


class BrushArchaeologyTests(_ArchSnifferTestBase):
    def test_blocked_prompt_and_auto_craft_brush_then_salvage_sherd(self) -> None:
        # 1) 无刷子且缺原料 -> blocked() 明确中文提示
        s_empty = State()
        s_empty.set_status({"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5]})
        s_empty.set_inventory({"items": [{"name": "copper_ingot", "count": 1}]})
        msg = BrushArchaeology.blocked(s_empty)
        self.assertIn("brush", msg)
        self.assertIn("copper_ingot", msg)

        # 2) 无刷子但备齐铜锭+羽毛+木棍 + 附近工作台 -> 自动合成刷子并刷出陶片
        blocks = _flat(-3, 5, -3, 3, y=63, block="stone")
        blocks[(2, 64, 0)] = "crafting_table"
        blocks[(4, 64, 0)] = "suspicious_gravel"
        world = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"copper_ingot": 1, "feather": 1, "stick": 1},
        )
        strokes = {"n": 0}

        def _on_use(w: ArchSnifferWorld, item: str, target=None, ms=None):
            if item == "brush" and target == {"pos": [4, 64, 0]}:
                strokes["n"] += 1
                if strokes["n"] >= 2:
                    w.blocks[(4, 64, 0)] = "gravel"
                    w.entities.append({"id": 701, "type": "item", "name": "item", "item": "arms_up_pottery_sherdx1", "pos": [4.5, 64.2, 0.5]})
                return {"ok": True, "used": "brush"}
            return None

        world.on_use_item = _on_use
        ex, state = world.session()
        skill = BrushArchaeology(target="sherd", radius=16)
        drive(skill, ex, state, max_steps=30)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("brush"), 1)
        self.assertEqual(state.count("arms_up_pottery_sherd"), 1)
        self.assertTrue(any(op == "craft" and req["item"] == "brush" for op, req in world.calls))
        self.assertFalse(any(op == "dig" for op, _ in world.calls), "严禁挖掘可疑方块")

    def test_remote_landmark_navigation_brushes_sherd_with_interact_fallback(self) -> None:
        blocks = _flat(-2, 106, -3, 3, y=63, block="sandstone")
        blocks[(102, 64, 0)] = "suspicious_sand"
        world = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"brush": 1},
        )
        # 模拟服务端 /use_item 对 brush 返回 cannot_use -> 自动切到 /interact 刷取
        world.on_use_item = lambda w, **_: (_ for _ in ()).throw(ApiError("cannot_use", "brush requires interact"))

        def _on_interact(w: ArchSnifferWorld, pos=None, entity_id=None, item=None):
            if item == "brush" and pos == [102, 64, 0]:
                w.blocks[(102, 64, 0)] = "sand"
                w.items["prize_pottery_sherd"] = w.items.get("prize_pottery_sherd", 0) + 1
                return {"ok": True, "changed": True}
            return None

        world.on_interact = _on_interact
        ex, state = world.session()
        memo_for(state).record_landmark("archaeology", (100, 64, 0), dimension="overworld")

        skill = BrushArchaeology(target="sherd", radius=24)
        steps = drive(skill, ex, state, max_steps=30)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("prize_pottery_sherd"), 1)
        self.assertTrue(any(op == "move_to" and (req["pos"] or [0])[0] >= 95.0 for op, req in world.calls))
        self.assertTrue(any(isinstance(s, dict) and s.get("op") == "use_item" and s.get("error_code") == "cannot_use" for s in steps))
        self.assertTrue(any(op == "interact" and req["item"] == "brush" for op, req in world.calls))
        self.assertFalse(any(op == "dig" for op, _ in world.calls))

    def test_underwater_warm_ocean_ruins_prioritizes_suspicious_sand_for_sniffer_egg(self) -> None:
        blocks = _flat(-2, 6, -3, 3, y=50, block="sand")
        for x in range(-2, 7):
            for y in range(51, 56):
                for z in range(-3, 4):
                    blocks[(x, y, z)] = "water"
        # suspicious_gravel 更近，但 target="sniffer_egg" 必须优先刷 suspicious_sand
        blocks[(2, 51, 0)] = "suspicious_gravel"
        blocks[(4, 51, 0)] = "suspicious_sand"
        world = ArchSnifferWorld(
            pos=(0.5, 51.0, 0.5),
            blocks=blocks,
            items={"brush": 1},
            oxygen=18.0,
            in_water=True,
            submerged=True,
        )
        brushed_targets: list[list[int]] = []

        def _on_use(w: ArchSnifferWorld, item: str, target=None, ms=None):
            if item == "brush" and isinstance(target, dict) and "pos" in target:
                brushed_targets.append(list(target["pos"]))
                if target["pos"] == [4, 51, 0]:
                    w.blocks[(4, 51, 0)] = "sand"
                    w.entities.append({"id": 702, "type": "item", "name": "item", "item": "sniffer_eggx1", "pos": [4.5, 51.2, 0.5]})
                return {"ok": True, "used": "brush"}
            return None

        world.on_use_item = _on_use
        ex, state = world.session()
        skill = BrushArchaeology(target="sniffer_egg", radius=24)
        drive(skill, ex, state, max_steps=30)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("sniffer_egg"), 1)
        self.assertEqual(brushed_targets[0], [4, 51, 0], "sniffer_egg 目标必须优先刷 suspicious_sand")
        self.assertFalse(any(op == "dig" for op, _ in world.calls))

    def test_oxygen_warning_surfaces_to_breathe_or_safely_exits(self) -> None:
        # Case A：水下氧气告急且上浮后仍无法恢复（如无气腔/顶部封顶）-> 安全中断退出
        blocks_trapped = _flat(-2, 4, -2, 2, y=50, block="stone")
        for y in range(51, 76):
            for x in range(-2, 5):
                for z in range(-2, 3):
                    blocks_trapped[(x, y, z)] = "water" if y < 56 else "stone"
        blocks_trapped[(2, 51, 0)] = "suspicious_sand"
        world_trap = ArchSnifferWorld(
            pos=(0.5, 51.0, 0.5),
            blocks=blocks_trapped,
            items={"brush": 1},
            oxygen=4.0,
            in_water=True,
            submerged=True,
        )
        ex_t, st_t = world_trap.session()
        with self.assertRaises(Replan) as cm:
            drive(BrushArchaeology(target="sniffer_egg", radius=16), ex_t, st_t, max_steps=20)
        self.assertIn("氧气", str(cm.exception))
        self.assertFalse(any(op in ("use_item", "interact", "dig") for op, _ in world_trap.calls))

        # Case B：水下氧气低但上方有水面 -> 先上浮换气恢复氧气，再下潜完成刷取
        blocks_open = _flat(-2, 4, -2, 2, y=50, block="sand")
        for x in range(-2, 5):
            for y in range(51, 55):
                for z in range(-2, 3):
                    blocks_open[(x, y, z)] = "water"
        blocks_open[(2, 51, 0)] = "suspicious_sand"
        world_open = ArchSnifferWorld(
            pos=(0.5, 51.0, 0.5),
            blocks=blocks_open,
            items={"brush": 1},
            oxygen=5.0,
            in_water=True,
            submerged=True,
        )

        def _on_move(w: ArchSnifferWorld, pos=None, rng=1):
            dest = [float(v) for v in pos]
            w.st["pos"] = dest
            if dest[1] >= 55.0:
                w.st["oxygen"] = 20.0
                w.st["submerged"] = False
                w.st["in_water"] = False
            else:
                w.st["submerged"] = True
                w.st["in_water"] = True
            return {"pos": list(dest)}

        def _on_use_open(w: ArchSnifferWorld, item: str, target=None, ms=None):
            if item == "brush" and target == {"pos": [2, 51, 0]}:
                w.blocks[(2, 51, 0)] = "sand"
                w.items["sniffer_egg"] = w.items.get("sniffer_egg", 0) + 1
                return {"ok": True, "used": "brush"}
            return None

        world_open.on_move_to = _on_move
        world_open.on_use_item = _on_use_open
        ex_o, st_o = world_open.session()
        skill_o = BrushArchaeology(target="sniffer_egg", radius=16)
        steps_o = drive(skill_o, ex_o, st_o, max_steps=30)

        self.assertEqual(skill_o.verify_done(st_o), "")
        self.assertEqual(st_o.count("sniffer_egg"), 1)
        self.assertTrue(any(isinstance(s, dict) and s.get("reason") == "surface_for_oxygen" for s in steps_o))

    def test_env_blocked_when_no_suspicious_blocks_or_no_table(self) -> None:
        # 1) 附近无任何可疑方块且无地标 -> EnvBlocked(wake_on="chunk_moved")
        w_none = ArchSnifferWorld(blocks=_flat(-3, 3, -3, 3, y=63), items={"brush": 1})
        ex1, st1 = w_none.session()
        with self.assertRaises(EnvBlocked) as cm1:
            drive(BrushArchaeology(target="sherd", radius=16), ex1, st1, max_steps=15)
        self.assertEqual(cm1.exception.wake_on, "chunk_moved")
        self.assertEqual(cm1.exception.missing_predicate, "visible_block:suspicious_sand")

        # 2) 无刷子有原料但缺少工作台 -> EnvBlocked(station_ready:crafting_table)
        w_notbl = ArchSnifferWorld(
            blocks=_flat(-3, 3, -3, 3, y=63),
            items={"copper_ingot": 1, "feather": 1, "stick": 1},
        )
        ex2, st2 = w_notbl.session()
        with self.assertRaises(EnvBlocked) as cm2:
            drive(BrushArchaeology(target="sherd", radius=16), ex2, st2, max_steps=15)
        self.assertEqual(cm2.exception.wake_on, "chunk_moved")
        self.assertEqual(cm2.exception.missing_predicate, "station_ready:crafting_table")


class HatchAndRaiseSnifferTests(_ArchSnifferTestBase):
    def test_place_sniffer_egg_prefers_moss_block_and_records_hatch_anchor(self) -> None:
        blocks = _flat(-3, 4, -3, 3, y=63, block="grass_block")
        blocks[(2, 63, 0)] = "moss_block"
        world = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"sniffer_egg": 1},
            entities=[],
        )
        ex, state = world.session()
        with self.assertRaises(Replan) as cm:
            drive(HatchAndRaiseSniffer(mode="plant_seed", radius=16), ex, state, max_steps=20)
        self.assertIn("waiting_sniffer_hatch", str(cm.exception))
        self.assertEqual(world.blocks.get((2, 64, 0)), "sniffer_egg")
        self.assertEqual(state.count("sniffer_egg"), 0)
        self.assertIn((2, 64, 0), memo_for(state).landmarks("sniffer_hatch", dimension="overworld"))

    def test_till_grass_and_plant_torchflower_seeds_and_pitcher_pod(self) -> None:
        # 1) 持石锄 + 火把花种子，在草方块上开垦耕地并种下
        blocks = _flat(-2, 4, -2, 2, y=63, block="stone")
        blocks[(2, 63, 0)] = "grass_block"
        world = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=blocks,
            items={"torchflower_seeds": 1, "stone_hoe": 1},
        )

        def _on_interact(w: ArchSnifferWorld, pos=None, entity_id=None, item=None):
            if pos == [2, 63, 0] and item == "stone_hoe":
                w.blocks[(2, 63, 0)] = "farmland"
                return {"ok": True, "changed": True}
            if pos == [2, 63, 0] and item == "torchflower_seeds":
                w.items["torchflower_seeds"] -= 1
                w.blocks[(2, 64, 0)] = "torchflower_crop"
                return {"ok": True, "changed": True}
            return None

        world.on_interact = _on_interact
        ex, state = world.session()
        skill = HatchAndRaiseSniffer(mode="plant_seed", radius=16)
        drive(skill, ex, state, max_steps=25)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(world.blocks.get((2, 63, 0)), "farmland")
        self.assertEqual(world.blocks.get((2, 64, 0)), "torchflower_crop")
        self.assertEqual(state.count("torchflower_seeds"), 0)

        # 2) 已有耕地 + 瓶子草荚果（无需锄头）
        blocks2 = _flat(-2, 4, -2, 2, y=63, block="stone")
        blocks2[(2, 63, 0)] = "farmland"
        world2 = ArchSnifferWorld(pos=(0.5, 64.0, 0.5), blocks=blocks2, items={"pitcher_pod": 1})

        def _on_interact2(w: ArchSnifferWorld, pos=None, entity_id=None, item=None):
            if pos == [2, 63, 0] and item == "pitcher_pod":
                w.items["pitcher_pod"] -= 1
                w.blocks[(2, 64, 0)] = "pitcher_crop"
                return {"ok": True, "changed": True}
            return None

        world2.on_interact = _on_interact2
        ex2, state2 = world2.session()
        skill2 = HatchAndRaiseSniffer(mode="plant_seed", radius=16)
        drive(skill2, ex2, state2, max_steps=20)
        self.assertEqual(skill2.verify_done(state2), "")
        self.assertEqual(world2.blocks.get((2, 64, 0)), "pitcher_crop")

    def test_feed_snifflet_direct_and_adult_pair_breeding(self) -> None:
        # 1) 直接喂食幼年嗅探兽
        world = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-3, 6, -3, 3, y=63, block="grass_block"),
            items={"torchflower_seeds": 2},
            entities=[{"id": 801, "name": "sniffer", "type": "animal", "baby": True, "pos": [4.5, 64.0, 0.5]}],
        )

        def _on_feed(w: ArchSnifferWorld, pos=None, entity_id=None, item=None):
            if entity_id == 801 and item == "torchflower_seeds":
                w.items["torchflower_seeds"] -= 1
                w.completed_advs.add("minecraft:husbandry/feed_snifflet")
                return {"ok": True, "changed": True}
            return None

        world.on_interact = _on_feed
        ex, state = world.session()
        skill = HatchAndRaiseSniffer(mode="feed_snifflet", radius=24)
        drive(skill, ex, state, max_steps=20)

        self.assertEqual(skill.verify_done(state), "")
        self.assertEqual(state.count("torchflower_seeds"), 1)
        self.assertTrue(any(op == "interact" and req["entity_id"] == 801 for op, req in world.calls))

        # 2) 成对成年嗅探兽繁殖出幼体并立刻喂食
        world2 = ArchSnifferWorld(
            pos=(0.5, 64.0, 0.5),
            blocks=_flat(-3, 6, -3, 3, y=63, block="grass_block"),
            items={"torchflower_seeds": 3},
            entities=[
                {"id": 901, "name": "sniffer", "type": "animal", "baby": False, "pos": [2.5, 64.0, 0.5]},
                {"id": 902, "name": "sniffer", "type": "animal", "baby": False, "pos": [3.5, 64.0, 0.5]},
            ],
        )
        fed_adults: set[int] = set()

        def _on_breed_and_feed(w: ArchSnifferWorld, pos=None, entity_id=None, item=None):
            if item != "torchflower_seeds":
                return None
            w.items["torchflower_seeds"] -= 1
            if entity_id in (901, 902):
                fed_adults.add(entity_id)
                if len(fed_adults) == 2 and not any(e["id"] == 903 for e in w.entities):
                    w.entities.append({"id": 903, "name": "sniffer", "type": "animal", "is_baby": True, "pos": [3.0, 64.0, 0.5]})
            return {"ok": True, "changed": True}

        world2.on_interact = _on_breed_and_feed
        ex2, state2 = world2.session()
        skill2 = HatchAndRaiseSniffer(mode="feed_snifflet", radius=24)
        drive(skill2, ex2, state2, max_steps=30)

        self.assertEqual(skill2.verify_done(state2), "")
        self.assertEqual(state2.count("torchflower_seeds"), 0)
        self.assertTrue(any(op == "interact" and req["entity_id"] == 903 for op, req in world2.calls))

    def test_sniffer_env_blocked_boundaries(self) -> None:
        # 1) 无耕地/泥土 -> EnvBlocked(reachable_planting_site:farmland)
        w1 = ArchSnifferWorld(blocks=_flat(-2, 2, -2, 2, y=63, block="stone"), items={"torchflower_seeds": 1})
        ex1, st1 = w1.session()
        with self.assertRaises(EnvBlocked) as cm1:
            drive(HatchAndRaiseSniffer(mode="plant_seed", radius=16), ex1, st1, max_steps=15)
        self.assertEqual(cm1.exception.wake_on, "chunk_moved")
        self.assertEqual(cm1.exception.missing_predicate, "reachable_planting_site:farmland")

        # 2) 仅有一只成年嗅探兽（无幼体、无法繁殖）-> EnvBlocked(visible_entity:snifflet)
        w2 = ArchSnifferWorld(
            blocks=_flat(-2, 4, -2, 2, y=63, block="grass_block"),
            items={"torchflower_seeds": 2},
            entities=[{"id": 910, "name": "sniffer", "type": "animal", "baby": False, "pos": [2.5, 64.0, 0.5]}],
        )
        ex2, st2 = w2.session()
        with self.assertRaises(EnvBlocked) as cm2:
            drive(HatchAndRaiseSniffer(mode="feed_snifflet", radius=16), ex2, st2, max_steps=15)
        self.assertEqual(cm2.exception.wake_on, "chunk_moved")
        self.assertEqual(cm2.exception.missing_predicate, "visible_entity:snifflet")


class RegistryAndLedgerContractTests(_ArchSnifferTestBase):
    def test_auto_discovery_contract_and_ledger_validation(self) -> None:
        loaded = skill_registry.load_all()
        self.assertIn("archaeology_sniffer", loaded)
        self.assertIn("brush_archaeology", REGISTRY)
        self.assertIn("hatch_and_raise_sniffer", REGISTRY)
        self.assertEqual(contract_errors(BrushArchaeology), [])
        self.assertEqual(contract_errors(HatchAndRaiseSniffer), [])

        # 默认台账静态核验必须返回空列表
        self.assertEqual(validate_ledger(GOALS), [])

        # 4 项新增目标的字段、分值（合计 135）与前置关系精确校验
        expected = {
            "salvage_sherd": ("T2", 15, "brush_archaeology", {"target": "sherd"}, (), ("archaeology|warm_ocean_ruins",), "minecraft:adventure/salvage_sherd"),
            "obtain_sniffer_egg": ("T3", 30, "brush_archaeology", {"target": "sniffer_egg"}, (), ("warm_ocean_ruins|warm_ocean|archaeology",), "minecraft:husbandry/obtain_sniffer_egg"),
            "plant_any_sniffer_seed": ("T3", 30, "hatch_and_raise_sniffer", {"mode": "plant_seed"}, ("obtain_sniffer_egg",), ("warm_ocean_ruins|warm_ocean|archaeology",), "minecraft:husbandry/plant_any_sniffer_seed"),
            "feed_snifflet": ("T4", 60, "hatch_and_raise_sniffer", {"mode": "feed_snifflet"}, ("obtain_sniffer_egg",), ("warm_ocean_ruins|warm_ocean|archaeology",), "minecraft:husbandry/feed_snifflet"),
        }
        self.assertEqual(sum(GOAL_MAP[gid].score for gid in expected), 135)
        for gid, (tier, score, skill_name, sargs, prereqs, landmarks, adv_id) in expected.items():
            spec = GOAL_MAP[gid]
            self.assertEqual(spec.tier, tier)
            self.assertEqual(spec.score, score)
            self.assertEqual(ADVANCEMENT_SCORES_V2[adv_id], score)
            self.assertEqual(spec.skill, skill_name)
            self.assertEqual(spec.args_dict, sargs)
            self.assertEqual(spec.prereq_goals, prereqs)
            self.assertEqual(spec.requires_landmarks, landmarks)
            self.assertEqual(spec.done_advancements, (adv_id,))
            self.assertEqual(spec.stage, "graduated")
            self.assertTrue(spec.stop_loss)
            self.assertEqual(
                spec.editable_files,
                ("code/mcagent/skills/archaeology_sniffer.py", "code/tests/test_archaeology_sniffer.py"),
            )
            _, arg_errs = bind_args(REGISTRY[skill_name], spec.args_dict)
            self.assertEqual(arg_errs, [], f"{gid} 参数绑定失败: {arg_errs}")
        self.assertEqual(GOAL_MAP["obtain_sniffer_egg"].done_items, ("sniffer_egg>=1",))

        # 模拟全量晋级为 graduated 时台账参数契约同样零报错
        promoted = tuple(replace(g, stage="graduated") if g.id in expected else g for g in GOALS)
        self.assertEqual(validate_ledger(promoted), [])

        # 检查新技能文件行数符合 AGENTS.md §9.3（250-350 行）且不含 ex.dig
        src = Path(__file__).resolve().parents[1] / "mcagent" / "skills" / "archaeology_sniffer.py"
        text = src.read_text(encoding="utf-8")
        line_count = len(text.splitlines())
        self.assertGreaterEqual(line_count, 250)
        self.assertLessEqual(line_count, 350)
        self.assertNotIn("ex.dig(", text)


if __name__ == "__main__":
    unittest.main()
