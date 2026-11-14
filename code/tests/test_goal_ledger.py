"""目标台账（Goal Ledger）、链路性价比排序、单轮止损与单目标闭环离线单测（verified: offline）。

全部用例完全断网可跑，覆盖：
1. `validate_ledger` 台账静态契约检查：默认 `GOALS` 通过，捕获重复 ID、前置环路、多 `candidate`（WIP=1 违规）、
   未注册技能与非法参数；
2. `rank_actionable_goals` 与 `RulePlanner` / `CampaignPlanner`：
   - 0 分准备项（`prep_wood` / `prep_wooden_pickaxe` / `prep_furnace`）继承下游链路分值，高价值链（如 `shiny_gear` 60 分）
     自动压过低分旁支；
   - `must_hold_for_children=True` 在 `keepInventory=False` 重生丢包后自动重补前置工具；
   - 挂机实测遥测（`p50_elapsed_s`、`hit_rate`、连败惩罚）动态重排优先级；
   - 单轮止损（`max_fails_per_ep` 与 `stop_loss=True` + `budget_s`）跳过卡死支线并切往下一目标；
   - `--llm` 模式自动将 `当前目标指引：...` 注入模型提示词，且 `live` 模式自动隔离 `candidate` 阶段目标；
3. `MainLoop` + `SoakObserver` 单目标生命周期：
   - `target_goal="upgrade_tools", stop_on_goal=True` 从空包自举砍树->木镐->采石->石镐，达成即停（`goal_reached`），
     `timeline.jsonl` 记录 `goals_reached` 且零 `GOAL_MISS` 误报；
   - 仅靠服务端 `done_advancements` 判定的目标在主技能完成当拍即刻强制复核达成，无需苦等 25s 周期；
   - 静默假完成技能（`completed` 零报错但未产出目标物/进度）准确产出 `INC-001-P1-goal_miss-*.json`，含精确缺口、
     `editable_files` 与可由 `replay_incident()` 确定性离线重放的 `http_tape`；
4. CLI 参数核验：`--goal nonexistent_goal --local-target` 返回码 2，`--dry` 离线校验台账返回码 0。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mcagent.client import Client, Config
from mcagent.loop import (
    GOAL_MAP,
    GOALS,
    CampaignPlanner,
    GoalSpec,
    LoopConfig,
    MainLoop,
    RulePlanner,
    SoakObserver,
    TapeClient,
    TapeRecorder,
    diagnose_goal_gap,
    is_goal_achieved,
    load_goal_telemetry,
    replay_incident,
    reset_world_memo,
    validate_ledger,
)
from mcagent.loop import __main__ as loop_main
from mcagent.loop.goals import (
    DEFAULT_CAMPAIGN_STAGES,
    OFFICIAL_CAMPAIGN_STAGES,
    effective_goal_cost,
    rank_actionable_goals,
    resolve_active_goals,
)
from mcagent.planner.llm import LLMClient
from mcagent.runtime import Executor, State
from mcagent.skills import REGISTRY, Skill, rec, register
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


class LedgerValidationTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_default_goals_ledger_passes_validation(self):
        self.assertEqual(validate_ledger(GOALS), [])
        self.assertEqual(len(GOALS), len(GOAL_MAP))

    def test_validate_ledger_catches_duplicate_id(self):
        dup = (GOALS[0], replace(GOALS[0], title="重复项"))
        errs = validate_ledger(dup)
        self.assertTrue(any("重复的目标 id: prep_wood" in e for e in errs), errs)

    def test_validate_ledger_catches_dependency_cycle(self):
        cyc = (
            GoalSpec(id="g_a", title="A", tier="T1", score=5, skill="gather_wood", prereq_goals=("g_b",), stage="draft"),
            GoalSpec(id="g_b", title="B", tier="T1", score=5, skill="mine_stone", prereq_goals=("g_a",), stage="draft"),
        )
        errs = validate_ledger(cyc)
        self.assertTrue(any("环路" in e for e in errs), errs)

    def test_validate_ledger_enforces_single_candidate_wip_limit(self):
        two_cands = (
            replace(GOALS[0], stage="candidate"),
            replace(GOALS[1], stage="candidate"),
        )
        errs = validate_ledger(two_cands)
        self.assertTrue(any("最多允许 1 个 candidate" in e for e in errs), errs)

    def test_validate_ledger_catches_unregistered_skill_on_active_stages(self):
        bad_cand = (
            GoalSpec(
                id="missing_skill_goal",
                title="未注册技能目标",
                tier="T1",
                score=10,
                skill="nonexistent_magic_skill",
                stage="candidate",
            ),
        )
        errs = validate_ledger(bad_cand)
        self.assertTrue(any("nonexistent_magic_skill" in e and "未在 skills.REGISTRY 注册" in e for e in errs), errs)

        bad_grad_fb = (
            GoalSpec(
                id="missing_fb_goal",
                title="补料技能未注册",
                tier="T1",
                score=5,
                skill="gather_wood",
                gather_fallbacks=(("oak_log>=1", "unregistered_fb_skill", ()),),
                stage="graduated",
            ),
        )
        errs_fb = validate_ledger(bad_grad_fb)
        self.assertTrue(any("unregistered_fb_skill" in e for e in errs_fb), errs_fb)

    def test_validate_ledger_catches_bad_skill_args(self):
        bad_args_ledger = (
            GoalSpec(
                id="bad_args_goal",
                title="越界参数",
                tier="T0_PREP",
                score=0,
                skill="gather_wood",
                skill_args=(("count", 999), ("unknown_param", 1)),
                stage="graduated",
            ),
        )
        errs = validate_ledger(bad_args_ledger)
        self.assertTrue(any("主技能参数" in e and "不符合契约" in e for e in errs), errs)


class RankingTelemetryStopLossAndLlmHintTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def _empty_state(self) -> State:
        st = State()
        st.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]})
        st.set_inventory({"items": []})
        return st

    def test_zero_point_prereqs_inherit_downstream_chain_and_shiny_gear_beats_side_goals(self):
        state = self._empty_state()
        # 激活全部台账目标（模拟全技能就绪后的全局性价比竞争）
        all_active = tuple(replace(g, stage="graduated") for g in GOALS)
        ranked = rank_actionable_goals(state, all_active)
        self.assertGreater(len(ranked), 0)

        # 空包首步必定是 0 分前置 prep_wood，且其驱动根目标继承高价值下游链（分值远超 0）
        top = ranked[0]
        self.assertEqual(top.step_goal.id, "prep_wood")
        self.assertEqual(top.step_goal.score, 0)
        self.assertGreaterEqual(top.path_score, 60)
        self.assertEqual(top.root_goal.id, "shiny_gear")

        # 当已持有石镐与石剑时：通往 shiny_gear(60分) 的 0 分前置 prep_furnace 排在低分旁支 breed_an_animal(5分) 之前
        mid_state = self._empty_state()
        mid_state.set_inventory({
            "items": [
                {"name": "stone_pickaxe", "count": 1, "slot": 0},
                {"name": "stone_sword", "count": 1, "slot": 1},
                {"name": "oak_log", "count": 4, "slot": 2},
                {"name": "cobblestone", "count": 8, "slot": 3},
            ]
        })
        ranked_mid = rank_actionable_goals(
            mid_state,
            all_active,
            completed_advs={"minecraft:story/mine_stone", "minecraft:story/upgrade_tools"},
        )
        step_ids = [s.step_goal.id for s in ranked_mid]
        self.assertEqual(ranked_mid[0].step_goal.id, "prep_furnace")
        self.assertEqual(ranked_mid[0].root_goal.id, "shiny_gear")
        self.assertLess(step_ids.index("prep_furnace"), step_ids.index("breed_an_animal"))

    def test_must_hold_for_children_rebuilds_tools_when_keep_inventory_false(self):
        state = self._empty_state()
        # 模拟本局早已拿过 mine_stone 与 upgrade_tools 服务端进度，但死亡掉落后当前空包
        planner = RulePlanner(target_goal="prep_furnace")
        planner.completed_advancements.update({
            "minecraft:story/mine_stone",
            "minecraft:story/upgrade_tools",
        })
        dec = planner.choose(state)
        self.assertIsNotNone(dec.skill)
        self.assertEqual(dec.skill.name, "gather_wood")
        gap = diagnose_goal_gap(GOAL_MAP["prep_furnace"], state, planner.completed_advancements)
        self.assertIn("prep_wooden_pickaxe", gap["missing_prereqs"])
        self.assertIn("upgrade_tools", gap["missing_prereqs"])

    def test_empirical_telemetry_dynamically_reranks_goals_and_loads_json(self):
        custom_ledger = (
            GoalSpec(
                id="goal_fast_static",
                title="静态看似快实测慢",
                tier="T1",
                score=10,
                skill="gather_wood",
                skill_args=(("count", 2),),
                est_cost_s=20.0,
                stage="graduated",
            ),
            GoalSpec(
                id="goal_slow_static",
                title="静态看似慢实测极快",
                tier="T1",
                score=10,
                skill="scout_island",
                skill_args=(("radius", 24),),
                est_cost_s=80.0,
                stage="graduated",
            ),
        )
        state = self._empty_state()

        # 无遥测时：按静态 est_cost_s 排序，goal_fast_static (10/20=0.5) > goal_slow_static (10/80=0.125)
        r_static = rank_actionable_goals(state, custom_ledger)
        self.assertEqual([x.step_goal.id for x in r_static], ["goal_fast_static", "goal_slow_static"])

        # 写入实测遥测文件：goal_fast_static 命中率低且慢，goal_slow_static 100% 命中且仅 12s
        with tempfile.TemporaryDirectory() as tmp:
            tel_path = Path(tmp) / "goal_cost_table.json"
            tel_path.write_text(
                json.dumps({
                    "goals": {
                        "goal_fast_static": {"episodes": 6, "hits": 2, "hit_rate": 0.25, "p50_elapsed_s": 50.0},
                        "goal_slow_static": {"episodes": 6, "hits": 6, "hit_rate": 1.0, "p50_elapsed_s": 12.0},
                    }
                }),
                encoding="utf-8",
            )
            telemetry = load_goal_telemetry(tel_path)
            self.assertEqual(load_goal_telemetry(Path(tmp) / "missing.json"), {})

        self.assertAlmostEqual(effective_goal_cost(custom_ledger[0], telemetry), 200.0)
        self.assertAlmostEqual(effective_goal_cost(custom_ledger[1], telemetry), 12.0)

        r_emp = rank_actionable_goals(state, custom_ledger, telemetry=telemetry)
        self.assertEqual([x.step_goal.id for x in r_emp], ["goal_slow_static", "goal_fast_static"])

    def test_per_episode_stop_loss_max_fails_and_budget_skips_to_next_goal(self):
        custom_ledger = (
            GoalSpec(
                id="opt_high_roi",
                title="高分但本局卡死支线",
                tier="T2",
                score=30,
                skill="gather_wood",
                skill_args=(("count", 4),),
                budget_s=60.0,
                max_fails_per_ep=2,
                stop_loss=True,
                est_cost_s=25.0,
                stage="graduated",
            ),
            GoalSpec(
                id="opt_steady_backup",
                title="稳健次选目标",
                tier="T1",
                score=10,
                skill="scout_island",
                skill_args=(("radius", 24),),
                budget_s=120.0,
                max_fails_per_ep=3,
                stop_loss=True,
                est_cost_s=25.0,
                stage="graduated",
            ),
        )
        state = self._empty_state()
        planner = CampaignPlanner(mode="campaign", min_verified="offline", ledger=custom_ledger)

        # 初始选择高 ROI 支线 opt_high_roi
        d1 = planner.select(state, [])
        self.assertEqual(d1.skill.name, "gather_wood")
        planner.record_skill_outcome("gather_wood", ok=False, elapsed_s=15.0)
        skill_base._COOLDOWN.clear()

        # 第 2 次失败触及 max_fails_per_ep=2 -> 自动止损跳过 opt_high_roi，切到 opt_steady_backup
        d2 = planner.select(state, [])
        self.assertEqual(d2.skill.name, "gather_wood")
        planner.record_skill_outcome("gather_wood", ok=False, elapsed_s=15.0)
        skill_base._COOLDOWN.clear()

        d3 = planner.select(state, [])
        self.assertEqual(d3.skill.name, "scout_island")
        self.assertIn("opt_steady_backup", d3.reason)

        # 验证时间预算硬止损（stop_loss=True 且累计耗时 >= budget_s=120s）
        planner.record_skill_outcome("scout_island", ok=True, elapsed_s=125.0)
        skill_base._COOLDOWN.clear()
        ranked_after = rank_actionable_goals(
            state,
            custom_ledger,
            ep_goal_fails=planner.rules.ep_goal_fails,
            ep_goal_elapsed=planner.rules.ep_goal_elapsed,
        )
        self.assertEqual(ranked_after, [])

    def test_campaign_planner_llm_injects_goal_hint_and_filters_stages_by_verified(self):
        cand_ledger = (
            replace(GOAL_MAP["prep_wood"], stage="graduated"),
            GoalSpec(
                id="cand_trial_goal",
                title="单候选试验目标",
                tier="T1",
                score=50,
                skill="scout_island",
                stage="candidate",
            ),
        )
        # live 模式只收 OFFICIAL_CAMPAIGN_STAGES，自动排除 candidate
        p_live = CampaignPlanner(mode="campaign", min_verified="live", ledger=cand_ledger)
        self.assertEqual([g.id for g in p_live.rules.active_goals], ["prep_wood"])
        self.assertEqual(
            [g.id for g in resolve_active_goals(cand_ledger, include_stages=OFFICIAL_CAMPAIGN_STAGES)],
            ["prep_wood"],
        )
        self.assertEqual(
            [g.id for g in resolve_active_goals(cand_ledger, include_stages=DEFAULT_CAMPAIGN_STAGES)],
            ["prep_wood", "cand_trial_goal"],
        )

        # LLM 模式收到包含「当前目标指引：...」的用户提示词
        captured_prompts: list[str] = []

        def fake_poster(url, headers, data, timeout, proxy):
            body = json.loads(data)
            user_msgs = [m["content"] for m in body.get("messages", []) if m.get("role") == "user"]
            captured_prompts.extend(user_msgs)
            return 200, json.dumps({
                "choices": [{
                    "message": {"content": '{"skill": "gather_wood", "args": {"count": 3}, "why": "按指引砍树"}'},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            })

        world = RichGridWorld(blocks={(2, 64, 0): "oak_log"})
        _, state = world.session()
        llm = LLMClient(key="dummy", fast_proxy="", poster=fake_poster, log=None)
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            target_goal="upgrade_tools",
        )
        dec = planner.select(state, [])
        self.assertEqual(dec.source, "llm")
        self.assertEqual(dec.skill.name, "gather_wood")
        self.assertGreaterEqual(len(captured_prompts), 1)
        self.assertIn("当前目标指引：", captured_prompts[0])
        self.assertIn("prep_wood", captured_prompts[0])
        self.assertIn("upgrade_tools", captured_prompts[0])


class SingleGoalLifecycleAndIncidentTests(unittest.TestCase):
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

    def test_mainloop_target_goal_upgrade_tools_bootstraps_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = RichGridWorld(blocks=self._make_island_blocks(), keep_inventory=True)
            state = State()
            ex = CountingExecutor(world, state)
            obs = SoakObserver(tmp, world_snapshot_id="snap-goal-ok", source="local-target", target_goal="upgrade_tools")
            planner = CampaignPlanner(mode="campaign", min_verified="offline", target_goal="upgrade_tools")
            cfg = LoopConfig(
                max_steps=80,
                idle_sleep_s=0.0,
                respawn_poll_s=0.001,
                enable_monitor=False,
                target_goal="upgrade_tools",
                stop_on_goal=True,
            )
            loop = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            res = loop.run()

            self.assertEqual(res.stop_reason, "goal_reached")
            self.assertGreaterEqual(state.count("stone_pickaxe"), 1)
            # 达成石镐后立即停止，不继续合成后续的 stone_sword / furnace
            self.assertEqual(state.count("stone_sword"), 0)
            self.assertEqual(state.count("furnace"), 0)
            self.assertIn("upgrade_tools", obs.goals_reached)
            self.assertTrue(is_goal_achieved(GOAL_MAP["upgrade_tools"], state, obs.server_advancements))

            # 零 GOAL_MISS 误报
            inc_files = list((Path(tmp) / "incidents").glob("*.json"))
            self.assertEqual(inc_files, [])

            # 核查 timeline.jsonl 的 run_end 字段与 summary.md
            tl_rows = [json.loads(line) for line in (Path(tmp) / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if line]
            run_end = [r for r in tl_rows if r.get("event") == "run_end"][-1]
            self.assertEqual(run_end["stop_reason"], "goal_reached")
            self.assertEqual(run_end["target_goal"], "upgrade_tools")
            self.assertTrue(run_end["target_goal_reached"])
            self.assertIsNotNone(run_end["target_goal_elapsed_s"])
            self.assertIn("upgrade_tools", run_end["goals_reached"])

            summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
            self.assertIn("`upgrade_tools` -> ✅ 已达成", summary)

    def test_advancement_only_target_goal_detected_immediately_without_25s_throttle(self):
        @register
        class MockSleepSkill(Skill):
            name = "mock_sleep_bed"
            description = "测试用躺床触发服务端进度技能"
            params = {}
            goal = "test:sleep"
            verified = "offline"

            def run(self, ex, state):
                ex.client.completed_advancements.append("minecraft:adventure/sleep_in_bed")
                yield rec("use_item", ex.use_item("red_bed"), item="red_bed")

        custom_ledger = (
            GoalSpec(
                id="sleep_in_bed_test",
                title="躺床仅服务端进度验收",
                tier="T1",
                score=5,
                skill="mock_sleep_bed",
                done_advancements=("minecraft:adventure/sleep_in_bed",),
                stage="candidate",
                editable_files=("code/mcagent/skills/survival.py",),
            ),
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                world = RichGridWorld(items={"red_bed": 1})
                state = State()
                ex = CountingExecutor(world, state)
                obs = SoakObserver(tmp, target_goal="sleep_in_bed_test", ledger=custom_ledger)
                planner = CampaignPlanner(
                    mode="campaign",
                    min_verified="offline",
                    target_goal="sleep_in_bed_test",
                    ledger=custom_ledger,
                )
                cfg = LoopConfig(
                    max_steps=10,
                    idle_sleep_s=0.0,
                    enable_monitor=False,
                    target_goal="sleep_in_bed_test",
                    stop_on_goal=True,
                )
                loop = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
                res = loop.run()

                self.assertEqual(res.stop_reason, "goal_reached")
                self.assertEqual(res.skills_completed, 1)
                self.assertIn("sleep_in_bed_test", obs.goals_reached)
                self.assertEqual(list((Path(tmp) / "incidents").glob("*.json")), [])
        finally:
            REGISTRY.pop("mock_sleep_bed", None)

    def test_quiet_noop_skill_without_goal_item_emits_replayable_goal_miss_incident(self):
        ran_count = {"n": 0}

        @register
        class QuietNoopDiamondSkill(Skill):
            name = "quiet_noop_diamond"
            description = "正常结束零报错但未产出钻石的假技能"
            params = {}
            goal = "test:quiet_diamond"
            verified = "offline"

            def can_start(self, state):
                return ran_count["n"] < 1

            def run(self, ex, state):
                state.refresh_status(ex.client, max_age_s=0)
                ex.client.find_blocks(types=["diamond_ore"], radius=16, limit=4)
                yield rec("move_to", ex.move_to(pos=[2, 64, 0], rng=2), pos=[2, 64, 0])
                ran_count["n"] += 1

        custom_ledger = (
            GoalSpec(
                id="quiet_diamond_goal",
                title="采集至少 2 颗钻石",
                tier="T1",
                score=15,
                skill="quiet_noop_diamond",
                done_advancements=("minecraft:story/mine_diamond",),
                done_items=("diamond>=2",),
                budget_s=60.0,
                max_fails_per_ep=1,
                est_cost_s=30.0,
                stage="candidate",
                editable_files=("code/mcagent/skills/diamond.py", "code/tests/test_diamond.py"),
            ),
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tape_path = Path(tmp) / "http_tape.jsonl"
                recorder = TapeRecorder(tape_path=str(tape_path))
                client_cfg = Config(base="http://mock.local/api/v1", username="u1", password="pw", token_file="")
                client = TapeClient(client_cfg, recorder, log=lambda *_: None)
                client.token = "tok"

                def fake_send(method, path, body=None, timeout=30, auth=True, token=None):
                    clean = path.lstrip("/")
                    if clean == "status":
                        return {"ok": True, "ms": 1, "result": {"ready": True, "alive": True, "health": 20, "food": 20, "pos": [0.5, 64.0, 0.5]}}
                    if clean == "inventory":
                        return {"ok": True, "ms": 1, "result": {"items": []}}
                    if clean.startswith("events"):
                        return {"ok": True, "ms": 1, "result": {"events": [], "next": 0}}
                    if clean.startswith("advancements"):
                        return {"ok": True, "ms": 1, "result": {"ready": True, "advancements": []}}
                    if clean == "find_blocks":
                        return {"ok": True, "ms": 2, "result": {"total": 1, "blocks": [{"name": "diamond_ore", "pos": [2, 64, 0], "dist": 1.5}]}}
                    if clean == "move_to":
                        return {"ok": True, "ms": 3, "result": {"pos": [1.5, 64.0, 0.5]}}
                    raise AssertionError(f"未预期的请求路径: {clean}")

                client._send = fake_send
                state = State()
                ex = Executor(client, state, log=lambda *_: None)
                obs = SoakObserver(
                    tmp,
                    tape=recorder,
                    world_snapshot_id="snap-goal-miss",
                    source="local-target",
                    target_goal="quiet_diamond_goal",
                    ledger=custom_ledger,
                )
                planner = CampaignPlanner(
                    mode="campaign",
                    min_verified="offline",
                    registry={"quiet_noop_diamond": QuietNoopDiamondSkill},
                    target_goal="quiet_diamond_goal",
                    ledger=custom_ledger,
                )
                loop_cfg = LoopConfig(
                    max_steps=5,
                    idle_sleep_s=0.0,
                    enable_monitor=False,
                    target_goal="quiet_diamond_goal",
                    stop_on_goal=True,
                )
                loop = MainLoop(client, state, ex, planner, config=loop_cfg, observer=obs, log=lambda *_: None)
                res = loop.run()

                self.assertEqual(res.skills_completed, 1)
                self.assertEqual(res.skills_replan, 0)
                self.assertTrue(res.stop_reason.startswith("idle:"), res.stop_reason)

                inc_files = sorted((Path(tmp) / "incidents").glob("INC-001-P1-goal_miss-*.json"))
                self.assertEqual(len(inc_files), 1)
                inc = json.loads(inc_files[0].read_text(encoding="utf-8"))

                self.assertEqual(inc["id"], "INC-001")
                self.assertEqual(inc["incident_id"], "INC-001")
                self.assertEqual(inc["code"], "GOAL_MISS")
                self.assertEqual(inc["severity"], "P1")
                self.assertEqual(inc["category"], "strategy")
                self.assertEqual(inc["fingerprint"], "GOAL_MISS:quiet_diamond_goal")
                self.assertEqual(inc["skill"], "quiet_noop_diamond")
                self.assertEqual(inc["goal_id"], "quiet_diamond_goal")
                self.assertEqual(
                    inc["editable_files"],
                    ["code/mcagent/skills/diamond.py", "code/tests/test_diamond.py"],
                )
                self.assertTrue(inc["detail"]["target_skill_started"])
                self.assertIn("diamond (0/2)", inc["detail"]["missing_items"])
                self.assertGreater(len(inc["http_tape"]), 0)

                # 重置测试计数后验证异常包可由 replay_incident() 100% 离线确定性重放且磁带精确对齐
                ran_count["n"] = 0
                replayed = replay_incident(inc_files[0])
                self.assertEqual(replayed["skill"], "quiet_noop_diamond")
                self.assertEqual(replayed["outcome"], "completed")
                self.assertEqual(replayed["steps_executed"], 1)
                self.assertEqual(replayed["tape_consumed"], replayed["tape_total"])

                summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
                self.assertIn("`quiet_diamond_goal` -> ❌ 未达成", summary)
        finally:
            REGISTRY.pop("quiet_noop_diamond", None)

    def test_goal_miss_when_blocked_at_prereq_reports_bottleneck_and_last_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 空岛无任何树木与陆地，目标 upgrade_tools 卡在首个前置 prep_wood
            world = RichGridWorld(blocks={}, items={})
            state = State()
            ex = CountingExecutor(world, state)
            obs = SoakObserver(tmp, target_goal="upgrade_tools")
            planner = CampaignPlanner(mode="campaign", min_verified="offline", target_goal="upgrade_tools")
            cfg = LoopConfig(
                max_steps=5,
                idle_sleep_s=0.0,
                enable_monitor=False,
                target_goal="upgrade_tools",
                stop_on_goal=True,
            )
            loop = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None)
            res = loop.run()

            self.assertTrue(res.stop_reason.startswith("idle:"), res.stop_reason)
            inc_files = sorted((Path(tmp) / "incidents").glob("INC-001-P1-goal_miss-*.json"))
            self.assertEqual(len(inc_files), 1)
            inc = json.loads(inc_files[0].read_text(encoding="utf-8"))
            self.assertEqual(inc["fingerprint"], "GOAL_MISS:upgrade_tools")
            self.assertFalse(inc["detail"]["target_skill_started"])
            self.assertEqual(inc["detail"]["bottleneck_goal_id"], "prep_wood")
            self.assertEqual(inc["detail"]["bottleneck_skill"], "gather_wood")
            self.assertIn("prep_wood", inc["detail"]["missing_prereqs"])
            self.assertIsNotNone(inc["detail"]["last_replan"])


class CliGoalAndDryTests(unittest.TestCase):
    def test_cli_nonexistent_goal_exits_with_code_2(self):
        with io.StringIO() as err_buf, redirect_stderr(err_buf):
            rc = loop_main.main(["--goal", "nonexistent_goal", "--local-target"])
            err_text = err_buf.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("nonexistent_goal", err_text)

    def test_cli_dry_validates_ledger_and_returns_zero(self):
        with patch.object(Client, "probe", return_value="unauthorized"):
            with io.StringIO() as out_buf, redirect_stdout(out_buf):
                rc = loop_main.main(["--dry"])
                out_text = out_buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("[dry] 目标台账核验通过", out_text)

    def test_cli_dry_returns_1_when_ledger_invalid(self):
        with patch.object(loop_main, "validate_ledger", return_value=["模拟台账错误"]):
            with io.StringIO() as out_buf, redirect_stdout(out_buf):
                rc = loop_main.main(["--dry"])
                out_text = out_buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("[dry] 目标台账契约不合格", out_text)


if __name__ == "__main__":
    unittest.main()
