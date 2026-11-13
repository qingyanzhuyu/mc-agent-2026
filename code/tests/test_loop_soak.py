"""统一比赛主循环、死亡自愈、预期收敛核对与 HTTP 离线录制回放单测（verified: offline）。

全部用例断网可跑，覆盖：
1. 首批 5 个 L3 技能的契约检查与 `skills.testing.drive()` 单步单动作硬约束；
2. `MainLoop` 正常推进、中途死亡自愈重生、`keepInventory` 开/关双态阶段自动衔接、重生零多余 `/posture`；
3. `SoakObserver` 的技能后置判据核对、非死亡姿态泄漏检测、单次波动过滤与重复震荡升级、指纹去重、进度分曲线与基线对比；
4. `TapeClient` 脱敏录制（含后台 monitor 线程交错调用过滤）与 `replay_incident()` 离线确定性重现；
5. `focus` 定向模式、`--llm` 延迟统计与非法输出自动回退规则队列；
6. `code/mcagent` 与 `tools/test-island` 的零耦合隔离校验。
"""
from __future__ import annotations

import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path

from mcagent.client import ApiError, Config
from mcagent.loop import (
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    RulePlanner,
    SoakObserver,
    TapeClient,
    TapeRecorder,
    replay_incident,
    reset_world_memo,
)
from mcagent.planner.llm import LLMClient
from mcagent.runtime import Executor, Replan, State
from mcagent.skills import REGISTRY, Skill, contract_errors, make, rec, register
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor, GridWorld, drive


class RichGridWorld(GridWorld):
    """扩展离线夹具以支持 `find_blocks` / `craft` / `use_item` / `advancements` 控制流测试。"""

    def __init__(self, pos=(0.5, 64.0, 0.5), blocks=None, items=None, keep_inventory: bool = True):
        super().__init__(pos=pos, blocks=blocks, items=items)
        self.keep_inventory = keep_inventory
        self.completed_advancements: list[str] = []
        self.protected_blocks: set[tuple[int, int, int]] = set()
        self.die_on_next_dig = False
        self._dead_polls_left = 0

    def status(self, timeout: float = 10) -> dict:
        if self._dead_polls_left > 0:
            self._dead_polls_left -= 1
            return {
                **self.st,
                "alive": False,
                "ready": False,
                "sneaking": False,
                "blocking": False,
                "last_death": {"pos": list(self.st["pos"]), "t": 1791881333488, "cause": "Bot was slain by Zombie"},
            }
        self.st["alive"] = True
        self.st["ready"] = True
        return super().status(timeout=timeout)

    def wait_ready(self, timeout: float = 120) -> dict:
        while self._dead_polls_left > 0:
            self.status()
        return self.status()

    def connect(self, timeout: float = 20) -> dict:
        return {"ok": True}

    def disconnect(self) -> dict:
        return {"ok": True}

    def advancements(self, status="completed", include_recipes=False) -> dict:
        return {
            "ready": True,
            "advancements": [{"id": aid, "status": "completed"} for aid in self.completed_advancements],
        }

    def find_blocks(self, types=None, radius=16, limit=20, properties=None, mature=None) -> dict:
        self.calls.append(("find_blocks", {"types": types, "radius": radius}))
        cx, cy, cz = self.st["pos"]
        if not types:
            summary: dict[str, dict] = {}
            for (bx, by, bz), name in self.blocks.items():
                d = ((bx - cx) ** 2 + (by - cy) ** 2 + (bz - cz) ** 2) ** 0.5
                if d <= radius and name != "air":
                    entry = summary.setdefault(name, {"count": 0, "nearest": [bx, by, bz], "dist": round(d, 1)})
                    entry["count"] += 1
            return {"radius": radius, "scanned": len(self.blocks), "blocks": summary}

        allowed = set(types)
        out = []
        for (bx, by, bz), name in sorted(self.blocks.items()):
            if name not in allowed:
                continue
            d = ((bx - cx) ** 2 + (by - cy) ** 2 + (bz - cz) ** 2) ** 0.5
            if d <= radius:
                out.append({"name": name, "pos": [bx, by, bz], "dist": round(d, 2)})
        out.sort(key=lambda b: b["dist"])
        return {"total": len(out), "blocks": out[:limit]}

    def dig(self, pos, collect=True, *, stop_on=None) -> dict:
        tpos = (int(pos[0]), int(pos[1]), int(pos[2]))
        if self.die_on_next_dig:
            self.die_on_next_dig = False
            self._dead_polls_left = 2
            self.st["sneaking"] = False
            self.st["blocking"] = False
            if not self.keep_inventory:
                self.items.clear()
            self.st["pos"] = [0.5, 64.0, 0.5]
            raise ApiError("interrupted", "dig interrupted by death", {"interrupted_by": "death"})
        if tpos in self.protected_blocks:
            self.calls.append(("dig", {"pos": list(pos)}))
            raise ApiError("protected_region", "spawn protection rollback")
        raw = self.blocks.get(tpos, "air")
        res = super().dig(pos, collect=collect, stop_on=stop_on)
        if raw == "stone" and collect:
            # 原版挖 stone 掉落 cobblestone
            self.items["stone"] = max(0, self.items.get("stone", 0) - 1)
            self.items["cobblestone"] = self.items.get("cobblestone", 0) + 1
            if "minecraft:story/mine_stone" not in self.completed_advancements:
                self.completed_advancements.append("minecraft:story/mine_stone")
        return {**res, "gained": {"cobblestone": 1} if raw == "stone" else ({raw: 1} if raw != "air" else {})}

    def place(self, item, pos=None, *, stop_on=None) -> dict:
        if pos is None:
            x, y, z = int(self.st["pos"][0]), int(self.st["pos"][1]), int(self.st["pos"][2])
            pos = [x + 1, y, z]
        return super().place(item, pos=pos, stop_on=stop_on)

    def craft(self, item, count=1, *, stop_on=None) -> dict:
        self.calls.append(("craft", {"item": item, "count": count}))
        if item.endswith("_planks"):
            log_name = item.removesuffix("_planks") + "_log"
            self._need(log_name)
            self.items[log_name] -= 1
            self.items[item] = self.items.get(item, 0) + 4
            return {"crafted": item, "gained": 4}
        if item == "stick":
            plank = next((k for k, v in self.items.items() if k.endswith("_planks") and v >= 2), None)
            if not plank:
                raise ApiError("missing_ingredients", "need 2 planks")
            self.items[plank] -= 2
            self.items["stick"] = self.items.get("stick", 0) + 4
            return {"crafted": "stick", "gained": 4}
        if item == "crafting_table":
            plank = next((k for k, v in self.items.items() if k.endswith("_planks") and v >= 4), None)
            if not plank:
                raise ApiError("missing_ingredients", "need 4 planks")
            self.items[plank] -= 4
            self.items["crafting_table"] = self.items.get("crafting_table", 0) + 1
            return {"crafted": "crafting_table", "gained": 1}
        has_table = any(v == "crafting_table" for v in self.blocks.values())
        if not has_table:
            raise ApiError("need_crafting_table", "need nearby crafting_table")
        if item == "wooden_pickaxe":
            plank = next((k for k, v in self.items.items() if k.endswith("_planks") and v >= 3), None)
            if not plank or self.items.get("stick", 0) < 2:
                raise ApiError("missing_ingredients", "need 3 planks + 2 sticks")
            self.items[plank] -= 3
            self.items["stick"] -= 2
            self.items["wooden_pickaxe"] = self.items.get("wooden_pickaxe", 0) + 1
            return {"crafted": item, "gained": 1, "used_table": True}
        if item == "stone_pickaxe":
            if self.items.get("cobblestone", 0) < 3 or self.items.get("stick", 0) < 2:
                raise ApiError("missing_ingredients", "need 3 cobble + 2 sticks")
            self.items["cobblestone"] -= 3
            self.items["stick"] -= 2
            self.items["stone_pickaxe"] = self.items.get("stone_pickaxe", 0) + 1
            if "minecraft:story/upgrade_tools" not in self.completed_advancements:
                self.completed_advancements.append("minecraft:story/upgrade_tools")
            return {"crafted": item, "gained": 1, "used_table": True}
        if item == "stone_sword":
            if self.items.get("cobblestone", 0) < 2 or self.items.get("stick", 0) < 1:
                raise ApiError("missing_ingredients", "need 2 cobble + 1 stick")
            self.items["cobblestone"] -= 2
            self.items["stick"] -= 1
            self.items["stone_sword"] = self.items.get("stone_sword", 0) + 1
            return {"crafted": item, "gained": 1, "used_table": True}
        if item == "furnace":
            if self.items.get("cobblestone", 0) < 8:
                raise ApiError("missing_ingredients", "need 8 cobble")
            self.items["cobblestone"] -= 8
            self.items["furnace"] = self.items.get("furnace", 0) + 1
            return {"crafted": item, "gained": 1, "used_table": True}
        raise ApiError("no_recipe", item)

    def use_item(self, item, target=None, ms=None, timeout_ms=None, source=None, *, stop_on=None) -> dict:
        self.calls.append(("use_item", {"item": item}))
        self._need(item)
        self.items[item] -= 1
        self.st["food"] = min(20, int(self.st.get("food", 20)) + 6)
        return {"used": item, "food": self.st["food"]}


class StarterSkillsContractTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_all_starter_skills_pass_contract_and_one_action_per_step(self):
        expected = {"eat_food", "scout_island", "gather_wood", "craft_gear", "mine_stone"}
        self.assertTrue(expected.issubset(set(REGISTRY)))
        for name in expected:
            self.assertEqual(contract_errors(REGISTRY[name]), [], name)
            self.assertEqual(REGISTRY[name].verified, "offline")

        blocks = {
            (2, 64, 0): "oak_log", (2, 65, 0): "oak_log", (2, 66, 0): "oak_log",
            (3, 64, 0): "stone", (3, 65, 0): "stone", (4, 64, 0): "stone",
            (10, 63, 0): "grass_block",
        }
        world = RichGridWorld(blocks=blocks, items={"bread": 1})
        world.st["food"] = 12
        ex, state = world.session()

        drive(make("eat_food"), ex, state)
        self.assertEqual(world.st["food"], 18)

        drive(make("gather_wood", {"count": 3}), ex, state)
        state.refresh_inventory(world)
        self.assertEqual(state.count("oak_log"), 3)

        drive(make("craft_gear", {"item": "wooden_pickaxe"}), ex, state)
        state.refresh_inventory(world)
        self.assertEqual(state.count("wooden_pickaxe"), 1)

        drive(make("mine_stone", {"count": 3}), ex, state)
        state.refresh_inventory(world)
        self.assertEqual(state.count("cobblestone"), 3)

        drive(make("craft_gear", {"item": "stone_pickaxe"}), ex, state)
        state.refresh_inventory(world)
        self.assertEqual(state.count("stone_pickaxe"), 1)

        drive(make("scout_island", {"radius": 20}), ex, state)

    def test_protected_region_marks_blacklist_and_scout_moves_away(self):
        blocks = {(2, 64, 0): "oak_log", (14, 63, 0): "grass_block"}
        world = RichGridWorld(blocks=blocks)
        world.protected_blocks.add((2, 64, 0))
        ex, state = world.session()

        with self.assertRaisesRegex(Replan, "保护区"):
            drive(make("gather_wood", {"count": 1}), ex, state)

        # scout_island 会避开刚标记的保护区点位，走向远端安全陆地
        steps = drive(make("scout_island", {"radius": 24}), ex, state)
        self.assertEqual(steps[0]["pos"], [14.5, 64, 0.5])


class MainLoopDeathAndProgressionTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def _make_island_blocks(self) -> dict:
        blocks = {}
        for i in range(6):
            blocks[(2 + i, 64, 0)] = "oak_log"
        for i in range(12):
            blocks[(2 + i, 64, 2)] = "stone"
        blocks[(12, 63, 5)] = "grass_block"
        return blocks

    def test_campaign_recovers_from_death_keep_inventory_true_and_no_respawn_posture_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = RichGridWorld(blocks=self._make_island_blocks(), keep_inventory=True)
            state = State()
            ex = CountingExecutor(world, state)
            obs = SoakObserver(tmp, world_snapshot_id="void-snap-01", source="local-target")
            planner = CampaignPlanner(mode="campaign", min_verified="offline")
            cfg = LoopConfig(
                max_steps=25, idle_sleep_s=0.0, respawn_poll_s=0.001, enable_monitor=False
            )
            loop = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)

            # 在拿到木镐开始挖第一块石头时注入一次死亡
            orig_dig = world.dig

            def inject_once(pos, collect=True, *, stop_on=None):
                if world.items.get("wooden_pickaxe", 0) >= 1 and not getattr(inject_once, "fired", False):
                    inject_once.fired = True
                    world.die_on_next_dig = True
                    # 模拟死前处于潜行态：按规范 §4.11 死亡自动复位，不应误判姿态泄漏也不应调 /posture
                    world.st["sneaking"] = True
                return orig_dig(pos, collect=collect, stop_on=stop_on)

            world.dig = inject_once
            res = loop.run()

            self.assertEqual(res.deaths_recovered, 1)
            # 重生恢复不发多余的 POST /posture
            posture_calls = [c for c in world.calls if c[0] == "posture"]
            self.assertEqual(posture_calls, [])
            # keepInventory=True：保留木镐，重生后即使 mine_stone 刚进冷却也不会退回重造木镐，而是继续推进
            self.assertTrue(obs.keep_inventory_inferred)
            self.assertEqual(state.count("wooden_pickaxe"), 1)
            # 死前潜行不应误报 POSTURE_LEAK
            bugs = [inc for inc in obs._incidents.values() if inc["code"] == "POSTURE_LEAK"]
            self.assertEqual(bugs, [])
            # 进度曲线与 summary.md 已落盘
            self.assertTrue((Path(tmp) / "summary.md").exists())
            self.assertTrue((Path(tmp) / "progress_curve.jsonl").exists())

    def test_campaign_recovers_from_death_keep_inventory_false_restarts_from_wood(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = RichGridWorld(blocks=self._make_island_blocks(), keep_inventory=False)
            state = State()
            ex = CountingExecutor(world, state)
            obs = SoakObserver(tmp, world_snapshot_id="void-snap-drop", source="local-target")
            planner = CampaignPlanner(mode="campaign", min_verified="offline")
            cfg = LoopConfig(
                max_steps=22, idle_sleep_s=0.0, respawn_poll_s=0.001, enable_monitor=False
            )
            loop = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)

            orig_dig = world.dig

            def wipe_once(pos, collect=True, *, stop_on=None):
                if world.items.get("wooden_pickaxe", 0) >= 1 and not getattr(wipe_once, "fired", False):
                    wipe_once.fired = True
                    world.die_on_next_dig = True
                return orig_dig(pos, collect=collect, stop_on=stop_on)

            world.dig = wipe_once
            res = loop.run()

            self.assertEqual(res.deaths_recovered, 1)
            self.assertFalse(obs.keep_inventory_inferred)
            # keepInventory=False 装备清空后，规划层从真实空包重新推导回砍树 -> 重新造出木镐！
            self.assertGreaterEqual(state.count("wooden_pickaxe"), 1)


class AuditorNoiseAndTapeReplayTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_auditor_distinguishes_bug_strategy_and_variance_and_compares_baseline(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as run_dir:
            # 写一份上一版 baseline 进度曲线
            (Path(base_dir) / "progress_curve.jsonl").write_text(
                json.dumps({"elapsed_s": 30.0, "server_score_v2": 5, "inferred_score_v2": 5}) + "\n",
                encoding="utf-8",
            )
            obs = SoakObserver(
                run_dir,
                world_snapshot_id="snap-42",
                replan_oscillation_threshold=3,
                baseline_dir=base_dir,
            )
            world = RichGridWorld(items={"cobblestone": 4, "stone_pickaxe": 1})
            _, state = world.session()
            obs.on_run_start(state)

            # 1) 单次正常 Replan（如 path_not_found）只计入波动，不生成异常文件
            gw = make("gather_wood", {"count": 2})
            obs.on_skill_start(gw, "rule", "test", state)
            obs.on_skill_end(gw, "replan", state, error_code="path_not_found", error_msg="no path")
            self.assertEqual(len(list((Path(run_dir) / "incidents").glob("*.json"))), 0)
            self.assertEqual(obs.env_variance_counts["gather_wood:path_not_found"], 1)

            # 2) 同一目标连续失败达 3 次 -> 升级为 strategy 震荡单，第 4 次按指纹去重合并
            for _ in range(3):
                obs.on_skill_start(gw, "rule", "test", state)
                obs.on_skill_end(gw, "replan", state, error_code="path_not_found", error_msg="no path")
            inc_files = list((Path(run_dir) / "incidents").glob("*.json"))
            self.assertEqual(len(inc_files), 1)
            inc_data = json.loads(inc_files[0].read_text(encoding="utf-8"))
            self.assertEqual((inc_data["code"], inc_data["category"], inc_data["count"]),
                             ("REPLAN_OSCILLATION", "strategy", 2))

            # 3) 非死亡场景下技能结束残留 sneaking=True -> 抓出 POSTURE_LEAK (bug)
            sc = make("scout_island")
            obs.on_skill_start(sc, "rule", "test", state)
            state.st["sneaking"] = True
            obs.on_skill_end(sc, "replan", state, error_code="timeout", died_or_disconnected=False)
            summary = obs.write_summary()
            self.assertIn("POSTURE_LEAK", summary)
            self.assertIn("基线对比", summary)
            self.assertIn("snap-42", summary)

    def test_http_tape_records_without_secrets_and_replays_deterministically_offline(self):
        @register
        class BuggyPhantomWood(Skill):
            name = "buggy_phantom_wood"
            description = "测试用假成功技能（用于验证录制与离线回放）"
            params = {}
            goal = "test:phantom"

            def run(self, ex, state):
                state.refresh_status(ex.client, max_age_s=0)
                ex.client.find_blocks(types=["oak_log"], radius=16, limit=4)
                yield rec("move_to", ex.move_to(pos=[2, 64, 0], rng=2), pos=[2, 64, 0])
                # 正常结束但实际上根本没挖树 -> 触发 verify_done 失败

            def verify_done(self, state, before_counts) -> str:
                return "" if state.count("oak_log") > before_counts.get("oak_log", 0) else "未挖到原木却提前返回"

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tape_path = Path(tmp) / "http_tape.jsonl"
                recorder = TapeRecorder(tape_path=str(tape_path))
                cfg = Config(base="http://mock.local/api/v1", username="u1", password="TOP_SECRET_PASSWORD", token_file="")
                client = TapeClient(cfg, recorder, log=lambda *_: None)
                client.token = "SECRET_BEARER_TOKEN_123"

                # 模拟底层 _send：穿插一条来自 SafetyMonitor 后台线程的 status 轮询，验证回放线程隔离
                responses = [
                    {"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]},
                    {"total": 1, "blocks": [{"name": "oak_log", "pos": [2, 64, 0], "dist": 1.5}]},
                    {"pos": [1.5, 64.0, 0.0]},
                ]
                idx = {"i": 0}

                def fake_send(method, path, body=None, timeout=30, auth=True, token=None):
                    if threading.current_thread().name == "mcagent-safety-monitor":
                        return {"ok": True, "ms": 2, "result": {"ready": True, "alive": True, "pos": [0.5, 64.0, 0.5]}}
                    r = responses[idx["i"]]
                    idx["i"] += 1
                    return {"ok": True, "ms": 3, "result": r}

                client._send = fake_send
                state = State()
                state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
                state.set_inventory({"items": []})
                ex = Executor(client, state, log=lambda *_: None)
                obs = SoakObserver(tmp, tape=recorder, world_snapshot_id="snap-replay")

                skill = make("buggy_phantom_wood")
                obs.on_skill_start(skill, "focus", "repro", state)

                # 插入后台安全线程并发调用，验证不会弄乱主线程技能回放序列
                t = threading.Thread(target=lambda: client.status(), name="mcagent-safety-monitor")
                t.start()
                t.join()

                while not skill.step(ex, state):
                    obs.on_step(skill, skill.last, state)
                obs.on_skill_end(skill, "completed", state)

                raw_tape = tape_path.read_text(encoding="utf-8")
                self.assertNotIn("TOP_SECRET_PASSWORD", raw_tape)
                self.assertNotIn("SECRET_BEARER_TOKEN_123", raw_tape)

                inc_files = list((Path(tmp) / "incidents").glob("*.json"))
                self.assertEqual(len(inc_files), 1)

                # 完全离线重放该异常包：应精确复现 postcondition_failed 与未挖到原木结论
                replayed = replay_incident(inc_files[0])
                self.assertEqual(replayed["outcome"], "postcondition_failed")
                self.assertIn("未挖到原木", replayed["postcondition_error"])
                self.assertTrue(replayed["matched_original_outcome"])
                self.assertEqual(replayed["steps_executed"], 1)
                self.assertEqual(replayed["tape_consumed"], replayed["tape_total"])
        finally:
            REGISTRY.pop("buggy_phantom_wood", None)

    def test_llm_mode_records_latency_and_falls_back_to_rules_on_bad_output(self):
        world = RichGridWorld(blocks={(2, 64, 0): "oak_log"})
        _, state = world.session()
        seen_telemetry: list[dict] = []

        bad_reply = (200, json.dumps({"choices": [{"message": {"content": "乱七八糟不是JSON"}, "finish_reason": "stop"}]}))
        llm = LLMClient(key="dummy", poster=lambda *a: bad_reply)
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            on_llm_telemetry=seen_telemetry.append,
        )
        decision = planner.select(state, [])
        self.assertEqual(decision.source, "llm_fallback_rule")
        self.assertEqual(decision.skill.name, "gather_wood")
        self.assertEqual(len(seen_telemetry), 1)
        self.assertFalse(seen_telemetry[0]["ok"])

    def test_live_inc001_regressions_dense_scout_canopy_wood_and_table_memo(self):
        """回归真机首跑 INC-001 抓到的三处问题：密集草地 limit=40 饥饿、高树冠悬空原木寻路失败、无工作台时 8 木板误触发合木镐。"""
        blocks = {}
        # 近处 5 格内铺满 >50 块草方块（旧版 limit=40 会被近处草块截断导致找不到远端落脚点）
        for dx in range(-4, 5):
            for dz in range(-4, 5):
                blocks[(dx, 63, dz)] = "grass_block"
        blocks[(12, 63, 0)] = "grass_block"
        # 头顶 y=69 悬空树冠原木（dy=5 > 4）+ 远端可达第 2 棵树
        blocks[(1, 69, 0)] = "birch_log"
        blocks[(6, 64, 0)] = "birch_log"
        blocks[(6, 65, 0)] = "birch_log"

        world = RichGridWorld(blocks=blocks)
        ex, state = world.session()

        # 1) scout_island 在密集地形下仍能找到 >=5 格外的远端陆地
        steps = drive(make("scout_island", {"radius": 24}), ex, state)
        self.assertEqual(steps[0]["pos"], [12.5, 64, 0.5])

        # 重置回原点验证悬空树冠跳过与 2 原木不误触木镐合成
        world.st["pos"] = [0.5, 64.0, 0.5]
        state.set_status(world.status())
        drive(make("gather_wood", {"count": 3}), ex, state)
        state.refresh_inventory(world)
        # 悬空 (1,69,0) 被跳过，成功砍下 2 块可达白桦原木并正常完成（不抛 Replan）
        self.assertEqual(state.count("birch_log"), 2)

        # 2) 仅有 2 原木（8 木板）且尚未放置过工作台时，gather_wood 若在冷却，RulePlanner 应选 scout_island 而非误调 craft_gear
        make("gather_wood").mark_failed()
        planner = CampaignPlanner(mode="campaign", min_verified="offline")
        dec = planner.select(state, [])
        self.assertEqual(dec.skill.name, "scout_island")

    def test_physical_isolation_between_mcagent_and_tools(self):
        mcagent_root = Path(__file__).resolve().parent.parent / "mcagent"
        forbidden = {"tools", "rcon", "rcon_ctl"}
        for py in mcagent_root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = set(alias.name.lower().split("."))
                        self.assertFalse(parts & forbidden, f"{py}: {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    parts = set(node.module.lower().split("."))
                    self.assertFalse(parts & forbidden, f"{py}: {node.module}")


if __name__ == "__main__":
    unittest.main()
