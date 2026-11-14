"""双工位持续挂机与 AI 自动修单编排器离线契约与热接力单测（verified: offline）。

使用真实临时 Git 仓库与真实 `git worktree` 验证：
1. 严禁以 `main`/`master` 作为自动修复分支；
2. Gate 1 白名单拦截冻结底座（`client.py`/`runtime.py`/`skills/base.py`）与密钥泄露，并自动还原脏工作树；
3. `init -> episode 产出异常单 -> claim 优先级排序 -> verify-and-promote 入待审分支 -> runner 热切换新 SHA` 全链路；
4. 新自动提交在下一轮诱发 `P0 bug` 时自动 `git revert` 回滚，以及超最大重试次数转交人工（`escalated_human`）；
5. `teardown` 干净卸载双临时 worktree 并完整保留待审分支与 `REVIEW_REPORT.md`。
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_soak_ctl():
    repo_root = Path(__file__).resolve().parents[2]
    mod_path = repo_root / "tools" / "soak-autofix" / "soak_ctl.py"
    spec = importlib.util.spec_from_file_location("soak_ctl_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


soak_ctl = _load_soak_ctl()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


class SoakAutofixOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.session = self.root / "session"
        self.repo.mkdir()

        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Soak Tester")

        # 构造最小目录骨架
        (self.repo / "code" / "mcagent" / "loop").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "skills").mkdir(parents=True)
        (self.repo / "code" / "tests").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "client.py").write_text("# frozen client\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "runtime.py").write_text("# frozen runtime\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "base.py").write_text("# frozen base\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 40\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_dummy.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init baseline")
        _git(self.repo, "checkout", "-b", "feat/loop-soak-agy")

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_main_branch_and_enforces_file_whitelist_and_secret_scan(self):
        with self.assertRaisesRegex(ValueError, "不能是受保护主干"):
            soak_ctl.init_session(self.repo, self.session, target_branch="main")

        for forbidden in (
            "code/mcagent/client.py",
            "code/mcagent/runtime.py",
            "code/mcagent/skills/base.py",
            "code/mcagent/planner/decision.py",
            "AGENTS.md",
            "code/mcagent/.env",
            "tools/test-island/rcon_ctl.py",
        ):
            ok, _ = soak_ctl.is_allowed_edit_path(forbidden)
            self.assertFalse(ok, forbidden)

        for allowed in (
            "code/mcagent/loop/starter_skills.py",
            "code/mcagent/skills/wood.py",
            "code/tests/test_new_fix.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(allowed)
            self.assertTrue(ok, f"{allowed}: {why}")

        hits = soak_ctl.scan_diff_for_secrets("+API_KEY = 'sk-1234567890abcdefghij'\n+Authorization: Bearer abcdefghijklmnopqrstuv")
        self.assertEqual(len(hits), 2)

    def test_dual_worktree_lifecycle_promote_hot_handoff_revert_and_circuit_breaker(self):
        st = soak_ctl.init_session(
            self.repo, self.session, target_branch="feat/soak-autofix", max_attempts=2
        )
        fixer_wt = Path(st["fixer_wt"])
        runner_wt = Path(st["runner_wt"])
        self.assertTrue(fixer_wt.exists())
        self.assertTrue(runner_wt.exists())

        # 1) 模拟第 1 轮挂机同时产出 P2 strategy 与 P1 bug 两张单
        ep1 = self.session / "episodes" / "ep-0001"
        (ep1 / "incidents").mkdir(parents=True)
        (ep1 / "progress_curve.jsonl").write_text(json.dumps({"server_score_v2": 15, "inferred_score_v2": 20}) + "\n", encoding="utf-8")
        (ep1 / "timeline.jsonl").write_text(
            json.dumps({"event": "skill_end", "outcome": "completed"}) + "\n"
            + json.dumps({"event": "run_end", "elapsed_s": 60.0}) + "\n"
        , encoding="utf-8")
        inc_p2 = {
            "incident_id": "INC-001",
            "fingerprint": "REPLAN_OSCILLATION:scout_island",
            "severity": "P2",
            "category": "strategy",
            "code": "REPLAN_OSCILLATION",
            "skill": "scout_island",
            "outcome": "replan",
            "error_code": "replan",
            "error_msg": "limit=40 starved",
            "http_tape": [{"seq": 1, "path": "find_blocks"}],
        }
        inc_p1 = {
            "incident_id": "INC-002",
            "fingerprint": "POSTURE_LEAK:craft_gear",
            "severity": "P1",
            "category": "bug",
            "code": "POSTURE_LEAK",
            "skill": "craft_gear",
            "outcome": "completed",
            "postcondition_error": "sneaking=True leaked",
            "http_tape": [],
        }
        (ep1 / "incidents" / "INC-001.json").write_text(json.dumps(inc_p2), encoding="utf-8")
        (ep1 / "incidents" / "INC-002.json").write_text(json.dumps(inc_p1), encoding="utf-8")

        base_tip = st["current_tip"]
        soak_ctl.ingest_episode_and_check_rollback(self.session, "ep-0001", ep1, base_tip, "agy_soak_r001")

        # 2) claim 必须优先认领 P1 bug（INC-002），而非 P2 strategy
        claimed = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["fingerprint"], "POSTURE_LEAK:craft_gear")
        self.assertTrue(Path(claimed["prompt_file"]).exists())

        # 3) 模拟 AI 违规修改冻结文件 client.py -> Gate 1 拒绝并自动清空脏改动（第 1 次失败）
        (fixer_wt / "code" / "mcagent" / "client.py").write_text("# hacked!\n", encoding="utf-8")
        bad_res = soak_ctl.verify_and_promote(
            self.session, "POSTURE_LEAK:craft_gear", live_canary=False
        )
        self.assertFalse(bad_res["ok"])
        self.assertEqual(bad_res["failed_gate"], "gate1_whitelist")
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")

        # 第 2 次再违规（空改动）-> 达到 max_attempts=2，自动熔断升级为 escalated_human
        reclaimed = soak_ctl.claim_next_incident(self.session)
        self.assertEqual(reclaimed["fingerprint"], "POSTURE_LEAK:craft_gear")
        bad_res2 = soak_ctl.verify_and_promote(
            self.session, "POSTURE_LEAK:craft_gear", live_canary=False
        )
        self.assertFalse(bad_res2["ok"])
        st_after_escalate = soak_ctl.read_state(self.session)
        self.assertEqual(
            st_after_escalate["incidents"]["POSTURE_LEAK:craft_gear"]["status"],
            "escalated_human",
        )

        # 4) 再次 claim 自动跳过已转人工的 INC-002，拿到 INC-001 并提交合法修复
        claimed_p2 = soak_ctl.claim_next_incident(self.session)
        self.assertEqual(claimed_p2["fingerprint"], "REPLAN_OSCILLATION:scout_island")
        (fixer_wt / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 250\n", encoding="utf-8")
        (fixer_wt / "code" / "tests" / "test_dummy.py").write_text("x = 2\n", encoding="utf-8")

        # 只拦截 `uv run ...` 子进程，保留真实 git 命令运行
        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            good_res = soak_ctl.verify_and_promote(
                self.session, "REPLAN_OSCILLATION:scout_island", live_canary=False
            )
        self.assertTrue(good_res["ok"], good_res)
        new_sha = good_res["commit"]
        self.assertNotEqual(new_sha, base_tip)

        # Runner 轮次边界原子热切换到新 commit，无需改主工作区
        _git(runner_wt, "checkout", "--detach", _git(self.repo, "rev-parse", "refs/heads/feat/soak-autofix"))
        self.assertIn("250", (runner_wt / "code" / "mcagent" / "loop" / "starter_skills.py").read_text(encoding="utf-8"))

        # 5) 模拟第 2 轮挂机在该新提交上触发了新 P0 bug -> 自动在 feat/soak-autofix 上 git revert 回滚！
        ep2 = self.session / "episodes" / "ep-0002"
        (ep2 / "incidents").mkdir(parents=True)
        (ep2 / "incidents" / "INC-001-P0.json").write_text(
            json.dumps({
                "incident_id": "INC-P0",
                "fingerprint": "FATAL_ENV_DEATH:scout_island:void",
                "severity": "P0",
                "category": "bug",
                "code": "FATAL_ENV_DEATH",
                "skill": "scout_island",
            }),
            encoding="utf-8",
        )
        ep2_meta = soak_ctl.ingest_episode_and_check_rollback(
            self.session, "ep-0002", ep2, new_sha, "agy_soak_r002"
        )
        self.assertEqual(ep2_meta["rolled_back"], new_sha)
        reverted_tip = _git(self.repo, "rev-parse", "refs/heads/feat/soak-autofix")
        self.assertNotEqual(reverted_tip, new_sha)
        _git(runner_wt, "checkout", "--detach", reverted_tip)
        self.assertIn("40", (runner_wt / "code" / "mcagent" / "loop" / "starter_skills.py").read_text(encoding="utf-8"))

        # 6) teardown 卸载临时双 worktree，但保留 feat/soak-autofix 分支与 REVIEW_REPORT.md
        report_path = soak_ctl.teardown_session(self.session)
        self.assertFalse(fixer_wt.exists())
        self.assertFalse(runner_wt.exists())
        self.assertTrue(report_path.exists())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn("reverted", report_text)
        self.assertIn("输入/输出 token", report_text)
        self.assertIn("价值", report_text)
        # main 分支全程纹丝未动
        self.assertNotEqual(
            _git(self.repo, "rev-parse", "refs/heads/main"),
            _git(self.repo, "rev-parse", "refs/heads/feat/soak-autofix"),
        )

    def test_run_subcommand_passes_llm_flag_and_review_report_renders_token_value(self):
        # 1) 验证 `run` 默认开启 LLM，且支持显式 `--llm` / `--no-llm` 透传给 runner-loop -> run_runner_loop(use_llm=...)
        parsed_default = soak_ctl.build_parser().parse_args(["run", "--session-dir", str(self.session), "--episodes", "2"])
        self.assertTrue(parsed_default.llm)
        parsed = soak_ctl.build_parser().parse_args(["run", "--session-dir", str(self.session), "--llm", "--episodes", "2"])
        self.assertTrue(parsed.llm)
        parsed_off = soak_ctl.build_parser().parse_args(["run", "--session-dir", str(self.session), "--no-llm", "--episodes", "2"])
        self.assertFalse(parsed_off.llm)

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-llm")
        spawned_cmds: list[list[str]] = []

        class DummyProc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "init_session", return_value={}), \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=lambda cmd, **kw: (spawned_cmds.append(list(cmd)), DummyProc())[1]):
            rc = soak_ctl.main(["run", "--session-dir", str(self.session), "--branch", "feat/soak-llm", "--episodes", "1", "--llm"])
        self.assertEqual(rc, 0)
        runner_invocations = [c for c in spawned_cmds if "runner-loop" in c]
        self.assertEqual(len(runner_invocations), 1)
        self.assertIn("--llm", runner_invocations[0])

        # 验证 runner-loop 解析 --llm 后真正以 use_llm=True 调用 run_runner_loop
        with patch.object(soak_ctl, "run_runner_loop", return_value=[]) as mock_rrl:
            rc2 = soak_ctl.main(["runner-loop", "--session-dir", str(self.session), "--episodes", "1", "--llm"])
        self.assertEqual(rc2, 0)
        self.assertTrue(mock_rrl.call_args.kwargs["use_llm"])

        # 2) 验证 ingest_episode_and_check_rollback 读取 token 用量并写入 REVIEW_REPORT.md 的两列
        st = soak_ctl.read_state(self.session)
        ep_dir = self.session / "episodes" / "ep-0001"
        (ep_dir / "incidents").mkdir(parents=True)
        (ep_dir / "progress_curve.jsonl").write_text(
            json.dumps({
                "elapsed_s": 288.0,
                "server_score_v2": 5,
                "inferred_score_v2": 10,
                "prompt_tokens": 80_000,
                "completion_tokens": 15_000,
                "total_tokens": 95_000,
            }) + "\n",
            encoding="utf-8",
        )
        (ep_dir / "timeline.jsonl").write_text(
            json.dumps({"event": "skill_end", "outcome": "completed", "elapsed_s": 120.0}) + "\n"
            + json.dumps({"event": "run_end", "elapsed_s": 288.0, "prompt_tokens": 80_000, "completion_tokens": 15_000}) + "\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"LLM_PRICE_IN_PER_M": "0", "LLM_PRICE_OUT_PER_M": "0"}):
            meta = soak_ctl.ingest_episode_and_check_rollback(
                self.session, "ep-0001", ep_dir, st["current_tip"], "agy_soak_r001"
            )
            report_default = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")

        self.assertEqual((meta["prompt_tokens"], meta["completion_tokens"]), (80_000, 15_000))
        self.assertIn("`80,000 / 15,000`", report_default)
        self.assertIn("额度 入1.00%/出1.00%（8h≈入100.0%/出100.0%）", report_default)
        self.assertNotIn("¥", report_default)  # 默认单价为 0，不显示金额

        with patch.dict("os.environ", {"LLM_PRICE_IN_PER_M": "1.0", "LLM_PRICE_OUT_PER_M": "2.0"}):
            soak_ctl.write_review_report(self.session)
            report_priced = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("¥0.1100（8h≈¥11.00）", report_priced)

        soak_ctl.teardown_session(self.session)

    def test_narrow_file_whitelist_enforces_editable_files_without_weakening_frozen_guard(self):
        narrow = ["code/mcagent/skills/iron.py", "code/tests/test_iron.py"]

        for allowed in (
            "code/mcagent/skills/iron.py",
            "./code/mcagent/skills/iron.py",
            "code/tests/test_iron.py",
            "code/tests/test_iron_extra.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(allowed, narrow_files=narrow)
            self.assertTrue(ok, f"{allowed}: {why}")

        for out_of_scope in (
            "code/mcagent/loop/policy.py",
            "code/mcagent/loop/starter_skills.py",
            "code/mcagent/skills/wood.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(out_of_scope, narrow_files=narrow)
            self.assertFalse(ok, out_of_scope)
            self.assertIn("超出目标单收窄白名单", why)

        # 即使恶意/错误把冻结底座或工程外脚本塞入 narrow_files，也绝不能绕过底层硬防线
        malicious_narrow = [
            "code/mcagent/client.py",
            "code/mcagent/runtime.py",
            "code/mcagent/skills/base.py",
            "tools/test-island/rcon_ctl.py",
            "code/mcagent/skills/iron.py",
        ]
        for frozen in (
            "code/mcagent/client.py",
            "code/mcagent/runtime.py",
            "code/mcagent/skills/base.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(frozen, narrow_files=malicious_narrow)
            self.assertFalse(ok, frozen)
            self.assertIn("命中冻结保护文件", why)

        ok_ext, why_ext = soak_ctl.is_allowed_edit_path("tools/test-island/rcon_ctl.py", narrow_files=malicious_narrow)
        self.assertFalse(ok_ext)
        self.assertIn("不在自动修复允许白名单内", why_ext)

    def test_goal_miss_lifecycle_narrow_gate1_campaign_canary_and_same_sha_recurrence(self):
        # 准备铁器技能初始文件
        (self.repo / "code" / "mcagent" / "skills" / "iron.py").write_text("IRON_SCAN = 16\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_iron.py").write_text("assert True\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "add iron skill stub")

        # 常规 bug max_attempts=1，但 GOAL_MISS 使用独立的 max_goal_attempts=3（验证两者解耦 + reopen + escalate）
        st = soak_ctl.init_session(
            self.repo,
            self.session,
            target_branch="feat/goal-smelt-iron",
            max_attempts=1,
            target_goal="smelt_iron",
            max_goal_attempts=3,
        )
        self.assertEqual(st["target_goal"], "smelt_iron")
        self.assertEqual(st["max_goal_attempts"], 3)
        fixer_wt = Path(st["fixer_wt"])

        def _make_goal_miss_inc(ep_dir: Path, summary_txt: str = "缺少铁矿暴露面") -> None:
            (ep_dir / "incidents").mkdir(parents=True, exist_ok=True)
            (ep_dir / "timeline.jsonl").write_text(
                json.dumps({"event": "run_start", "source": "local-target"}) + "\n"
                + json.dumps({
                    "event": "run_end",
                    "elapsed_s": 120.0,
                    "stop_reason": "max_seconds",
                    "source": "local-target",
                    "deaths": 0,
                    "target_goal": "smelt_iron",
                    "target_goal_reached": False,
                    "goals_reached": {"prep_wood": 9.0, "upgrade_tools": 40.0},
                    "total_tokens": 2200,
                }) + "\n",
                encoding="utf-8",
            )
            inc_payload = {
                "id": "INC-001",
                "fingerprint": "GOAL_MISS:smelt_iron",
                "severity": "P1",
                "category": "strategy",
                "code": "GOAL_MISS",
                "skill_context": {"skill": "smelt_iron", "args": {"count": 3}, "outcome": "replan"},
                "detail": {
                    "goal_id": "smelt_iron",
                    "goal_title": "来硬的（冶炼出铁锭）",
                    "tier": "T1",
                    "score": 5,
                    "budget_s": 210.0,
                    "elapsed_s": 120.0,
                    "stop_reason": "max_seconds",
                    "completed_prereqs": ["prep_wood", "upgrade_tools", "prep_furnace"],
                    "missing_prereqs": [],
                    "bottleneck_goal_id": "smelt_iron",
                    "bottleneck_skill": "smelt_iron",
                    "missing_items": ["iron_ingot (0/1)"],
                    "summary": summary_txt,
                    "target_skill_started": True,
                    "last_replan": {"skill": "smelt_iron", "reason": "no_iron_ore_visible"},
                    "skill_stats": {"smelt_iron": {"runs": 2, "replans": 2}},
                    "editable_files": ["code/mcagent/skills/iron.py", "code/tests/test_iron.py"],
                },
            }
            (ep_dir / "incidents" / "INC-001-P1-goal_miss-smelt_iron.json").write_text(
                json.dumps(inc_payload, ensure_ascii=False), encoding="utf-8"
            )

        # 1) ep-0001 产出 GOAL_MISS:smelt_iron（验证 fallback 提取 id / skill_context.skill / detail.goal_id & editable_files）
        ep1 = self.session / "episodes" / "ep-0001"
        _make_goal_miss_inc(ep1)
        soak_ctl.ingest_episode_and_check_rollback(
            self.session, "ep-0001", ep1, st["current_tip"], "agy_soak_r001"
        )

        # 2) claim 生成 prompt.md，必须包含 1B 缺口诊断与收窄白名单警告
        claimed = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["fingerprint"], "GOAL_MISS:smelt_iron")
        self.assertEqual(claimed["goal_id"], "smelt_iron")
        self.assertEqual(claimed["skill"], "smelt_iron")
        prompt_text = Path(claimed["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("目标未达成诊断（GOAL_MISS）", prompt_text)
        self.assertIn("iron_ingot (0/1)", prompt_text)
        self.assertIn("no_iron_ore_visible", prompt_text)
        self.assertIn("code/mcagent/skills/iron.py", prompt_text)
        self.assertIn("policy.py", prompt_text)

        # 3) 第 1 次尝试：Fixer 越界修改 starter_skills.py（不在 editable_files 中）-> Gate 1 拒绝！
        # 且因 max_goal_attempts=3 (> max_attempts=1)，attempts=1 后状态仍为 pending 而非误熔断
        (fixer_wt / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 999\n", encoding="utf-8")
        bad_res = soak_ctl.verify_and_promote(self.session, "GOAL_MISS:smelt_iron", live_canary=False)
        self.assertFalse(bad_res["ok"])
        self.assertEqual(bad_res["failed_gate"], "gate1_whitelist")
        self.assertIn("超出目标单收窄白名单", bad_res["detail"])
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")
        st_after_1 = soak_ctl.read_state(self.session)
        self.assertEqual(st_after_1["incidents"]["GOAL_MISS:smelt_iron"]["status"], "pending")
        self.assertEqual(st_after_1["incidents"]["GOAL_MISS:smelt_iron"]["attempts"], 1)

        # 4) 第 2 次尝试：重领并在收窄白名单内修改 iron.py + test_iron.py，且开启 live_canary 验证 Gate 3 走 --mode campaign --goal smelt_iron
        reclaimed = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(reclaimed)
        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text("IRON_SCAN = 32\n", encoding="utf-8")
        (fixer_wt / "code" / "tests" / "test_iron.py").write_text("assert 32 > 16\n", encoding="utf-8")

        real_run = subprocess.run
        uv_cmds: list[list[str]] = []

        def mock_uv_and_canary(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                uv_cmds.append(list(cmd))
                if "--run-dir" in cmd:
                    c_dir = Path(cmd[cmd.index("--run-dir") + 1])
                    (c_dir / "incidents").mkdir(parents=True, exist_ok=True)
                    (c_dir / "timeline.jsonl").write_text(
                        json.dumps({"event": "run_end", "elapsed_s": 30.0}) + "\n", encoding="utf-8"
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_and_canary):
            good_res = soak_ctl.verify_and_promote(
                self.session, "GOAL_MISS:smelt_iron", live_canary=True
            )
        self.assertTrue(good_res["ok"], good_res)
        sha_1 = good_res["commit"]
        canary_invocations = [c for c in uv_cmds if "--live" in c]
        self.assertEqual(len(canary_invocations), 1)
        self.assertIn("campaign", canary_invocations[0])
        self.assertIn("--goal", canary_invocations[0])
        self.assertEqual(canary_invocations[0][canary_invocations[0].index("--goal") + 1], "smelt_iron")
        self.assertNotIn("focus", canary_invocations[0])

        # 5) ep-0002 正好跑在 commit_sha == sha_1 上，但仍复发 GOAL_MISS:smelt_iron！
        # 验证修复后的 merge-base --is-ancestor 检查能准确识别同 SHA 复发并重新打开（attempts=2 < 3 -> pending）
        ep2 = self.session / "episodes" / "ep-0002"
        _make_goal_miss_inc(ep2, summary_txt="32 格仍未搜到铁矿")
        soak_ctl.ingest_episode_and_check_rollback(
            self.session, "ep-0002", ep2, sha_1, "agy_soak_r002"
        )
        st_after_ep2 = soak_ctl.read_state(self.session)
        inc_after_ep2 = st_after_ep2["incidents"]["GOAL_MISS:smelt_iron"]
        self.assertEqual(inc_after_ep2["status"], "pending")
        self.assertEqual(inc_after_ep2["last_failed_fix_commit"], sha_1)
        self.assertIsNone(inc_after_ep2["fix_commit"])

        # 6) 第 3 次尝试提交 sha_2（attempts=3），随后 ep-0003 在 sha_2 上再次复发 -> 达到上限 3 自动升级 escalated_human！
        reclaimed_3 = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(reclaimed_3)
        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text("IRON_SCAN = 48\n", encoding="utf-8")
        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_and_canary):
            good_res_2 = soak_ctl.verify_and_promote(
                self.session, "GOAL_MISS:smelt_iron", live_canary=False
            )
        self.assertTrue(good_res_2["ok"], good_res_2)
        sha_2 = good_res_2["commit"]

        ep3 = self.session / "episodes" / "ep-0003"
        _make_goal_miss_inc(ep3, summary_txt="48 格仍失败")
        soak_ctl.ingest_episode_and_check_rollback(
            self.session, "ep-0003", ep3, sha_2, "agy_soak_r003"
        )
        st_after_ep3 = soak_ctl.read_state(self.session)
        self.assertEqual(st_after_ep3["incidents"]["GOAL_MISS:smelt_iron"]["status"], "escalated_human")

        # 7) 持续模式（episodes=0）下 Runner 检测到聚焦目标已 escalated_human，自动写入 STOP 止损退出，不空耗下一轮
        loop_res = soak_ctl.run_runner_loop(self.session, episodes=0)
        self.assertEqual(loop_res, [])
        self.assertTrue((self.session / "STOP").exists())

        soak_ctl.teardown_session(self.session)

    def test_prereq_score_regression_revert_goal_telemetry_isolation_and_cli_forwarding(self):
        (self.repo / "code" / "mcagent" / "skills" / "iron.py").write_text("MODE = 'safe'\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_iron.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init iron")

        st = soak_ctl.init_session(
            self.repo,
            self.session,
            target_branch="feat/goal-telemetry",
            target_goal="smelt_iron",
            max_goal_attempts=2,
        )
        fixer_wt = Path(st["fixer_wt"])
        base_sha = st["current_tip"]

        # 1) ep-0001 (local-target)：前置木石链达成（得分 10），但目标 smelt_iron 未达成
        ep1 = self.session / "episodes" / "ep-0001"
        (ep1 / "incidents").mkdir(parents=True)
        (ep1 / "progress_curve.jsonl").write_text(
            json.dumps({"server_score_v2": 10, "inferred_score_v2": 10}) + "\n", encoding="utf-8"
        )
        (ep1 / "timeline.jsonl").write_text(
            json.dumps({"event": "run_start", "source": "local-target"}) + "\n"
            + json.dumps({
                "event": "run_end",
                "elapsed_s": 95.0,
                "stop_reason": "max_seconds",
                "source": "local-target",
                "deaths": 1,
                "target_goal": "smelt_iron",
                "target_goal_reached": False,
                "goals_reached": {"prep_wood": 12.0, "mine_stone": 35.0, "upgrade_tools": 50.0},
                "prompt_tokens": 2000,
                "completion_tokens": 400,
                "total_tokens": 2400,
            }) + "\n",
            encoding="utf-8",
        )
        (ep1 / "incidents" / "INC-001-goal_miss.json").write_text(
            json.dumps({
                "incident_id": "INC-001",
                "fingerprint": "GOAL_MISS:smelt_iron",
                "severity": "P1",
                "category": "strategy",
                "code": "GOAL_MISS",
                "skill": "smelt_iron",
                "goal_id": "smelt_iron",
                "editable_files": ["code/mcagent/skills/iron.py", "code/tests/test_iron.py"],
            }),
            encoding="utf-8",
        )
        soak_ctl.ingest_episode_and_check_rollback(self.session, "ep-0001", ep1, base_sha, "agy_soak_r001")

        # 2) Fixer 提交一次修复 bad_sha
        soak_ctl.claim_next_incident(self.session)
        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text("MODE = 'broken_prereqs'\n", encoding="utf-8")
        real_run = subprocess.run

        def mock_uv_ok(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_ok):
            prom = soak_ctl.verify_and_promote(self.session, "GOAL_MISS:smelt_iron", live_canary=False)
        self.assertTrue(prom["ok"], prom)
        bad_sha = prom["commit"]

        # 3) ep-0002 跑在 bad_sha 上：虽无 P0 bug，但原有前置目标全丢（goals_reached={}）且最高分退化为 0 (< 10)
        # 必须触发 prereq_score_regression 自动 git revert！
        ep2 = self.session / "episodes" / "ep-0002"
        (ep2 / "incidents").mkdir(parents=True)
        (ep2 / "progress_curve.jsonl").write_text(
            json.dumps({"server_score_v2": 0, "inferred_score_v2": 0}) + "\n", encoding="utf-8"
        )
        (ep2 / "timeline.jsonl").write_text(
            json.dumps({"event": "run_start", "source": "local-target"}) + "\n"
            + json.dumps({
                "event": "run_end",
                "elapsed_s": 60.0,
                "stop_reason": "max_seconds",
                "source": "local-target",
                "deaths": 0,
                "target_goal": "smelt_iron",
                "target_goal_reached": False,
                "goals_reached": {},
                "total_tokens": 900,
            }) + "\n",
            encoding="utf-8",
        )
        ep2_meta = soak_ctl.ingest_episode_and_check_rollback(self.session, "ep-0002", ep2, bad_sha, "agy_soak_r002")
        self.assertEqual(ep2_meta["rolled_back"], bad_sha)
        self.assertEqual(ep2_meta["revert_reason"], "prereq_score_regression")
        st_after_rev = soak_ctl.read_state(self.session)
        self.assertEqual(st_after_rev["commits"][-1]["status"], "reverted")
        self.assertEqual(st_after_rev["commits"][-1]["revert_reason"], "prereq_score_regression")
        self.assertEqual(st_after_rev["incidents"]["GOAL_MISS:smelt_iron"]["status"], "rolled_back")

        # 4) ep-0003 (local-target) 成功达成 smelt_iron，且同时验证 pending GOAL_MISS 被实跑达成时转为 resolved_by_run
        with soak_ctl.locked_state(self.session) as st_mut:
            st_mut["incidents"]["GOAL_MISS:iron_tools"] = {
                "fingerprint": "GOAL_MISS:iron_tools",
                "incident_id": "INC-009",
                "code": "GOAL_MISS",
                "severity": "P1",
                "category": "strategy",
                "skill": "craft_iron_gear",
                "goal_id": "iron_tools",
                "editable_files": ["code/mcagent/skills/iron.py"],
                "status": "pending",
                "attempts": 0,
                "incident_path": str(ep1 / "incidents" / "INC-001-goal_miss.json"),
                "gate_history": [],
            }
        cur_tip = soak_ctl.read_state(self.session)["current_tip"]
        ep3 = self.session / "episodes" / "ep-0003"
        (ep3 / "incidents").mkdir(parents=True)
        (ep3 / "progress_curve.jsonl").write_text(
            json.dumps({"server_score_v2": 20, "inferred_score_v2": 20}) + "\n", encoding="utf-8"
        )
        (ep3 / "timeline.jsonl").write_text(
            json.dumps({"event": "run_start", "source": "local-target"}) + "\n"
            + json.dumps({
                "event": "run_end",
                "elapsed_s": 110.0,
                "stop_reason": "goal_reached",
                "source": "local-target",
                "deaths": 0,
                "target_goal": "iron_tools",
                "target_goal_reached": True,
                "target_goal_elapsed_s": 102.5,
                "goals_reached": {
                    "prep_wood": 10.0,
                    "mine_stone": 29.0,
                    "upgrade_tools": 44.0,
                    "smelt_iron": 88.4,
                    "iron_tools": 102.5,
                },
                "prompt_tokens": 2600,
                "completion_tokens": 400,
                "total_tokens": 3000,
            }) + "\n",
            encoding="utf-8",
        )
        soak_ctl.ingest_episode_and_check_rollback(self.session, "ep-0003", ep3, cur_tip, "agy_soak_r003")
        st_after_ep3 = soak_ctl.read_state(self.session)
        self.assertEqual(st_after_ep3["incidents"]["GOAL_MISS:iron_tools"]["status"], "resolved_by_run")
        self.assertEqual(st_after_ep3["incidents"]["GOAL_MISS:iron_tools"]["goal_achieved_ep"], "ep-0003")

        # 5) ep-0004 (official) 独立落盘到 goal_telemetry.official.json，严禁污染 local-target 统计
        ep4 = self.session / "episodes" / "ep-0004"
        (ep4 / "incidents").mkdir(parents=True)
        (ep4 / "timeline.jsonl").write_text(
            json.dumps({"event": "run_start", "source": "official"}) + "\n"
            + json.dumps({
                "event": "run_end",
                "elapsed_s": 50.0,
                "source": "official",
                "deaths": 2,
                "goals_reached": {"prep_wood": 15.0},
                "total_tokens": 1200,
            }) + "\n",
            encoding="utf-8",
        )
        soak_ctl.ingest_episode_and_check_rollback(self.session, "ep-0004", ep4, cur_tip, "official_bot")

        local_tel_path = self.session / "goal_telemetry.local-target.json"
        official_tel_path = self.session / "goal_telemetry.official.json"
        self.assertTrue(local_tel_path.exists())
        self.assertTrue(official_tel_path.exists())

        local_tel = json.loads(local_tel_path.read_text(encoding="utf-8"))
        self.assertEqual(local_tel["source"], "local-target")
        # ep-0002 已回滚排除，非回滚 local-target 轮次仅 ep-0001 与 ep-0003 共 2 轮
        self.assertEqual(local_tel["episodes_total"], 2)
        self.assertIn("AGENTS.md §4", local_tel["note"])
        upg = local_tel["goals"]["upgrade_tools"]
        self.assertEqual(upg["episodes"], 2)
        self.assertEqual(upg["hits"], 2)
        self.assertEqual(upg["hit_rate"], 1.0)
        self.assertEqual(upg["p50_elapsed_s"], 47.0)   # median([50.0, 44.0])
        self.assertEqual(upg["p50_tokens"], 2700)      # median([2400, 3000])
        self.assertEqual(upg["total_deaths"], 1)

        smelt = local_tel["goals"]["smelt_iron"]
        self.assertEqual(smelt["episodes"], 2)
        self.assertEqual(smelt["hits"], 1)
        self.assertEqual(smelt["hit_rate"], 0.5)
        self.assertEqual(smelt["p50_elapsed_s"], 88.4)
        self.assertEqual(smelt["p50_tokens"], 3000)

        # 6) 验证 REVIEW_REPORT.md 渲染聚焦目标、退化原因、达成耗时与第五节实测成本表
        report_txt = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("本轮聚焦晋级目标", report_txt)
        self.assertIn("`smelt_iron`", report_txt)
        self.assertIn("prereq_score_regression", report_txt)
        self.assertIn("✅ `iron_tools` (102.5s)", report_txt)
        self.assertIn("目标台账实测成本汇总", report_txt)
        self.assertIn("输入/输出 token", report_txt)
        self.assertIn("价值", report_txt)

        # 7) 验证 run / runner-loop CLI --goal 透传及 --goal-telemetry 自动挂载
        loop_invocations: list[list[str]] = []

        def mock_runner_subproc(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                loop_invocations.append(list(cmd))
                rdir = Path(cmd[cmd.index("--run-dir") + 1])
                (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                (rdir / "timeline.jsonl").write_text(
                    json.dumps({"event": "run_end", "elapsed_s": 10.0, "source": "local-target"}) + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_runner_subproc):
            rc_rl = soak_ctl.main([
                "runner-loop",
                "--session-dir", str(self.session),
                "--episodes", "1",
                "--goal", "iron_tools",
                "--no-llm",
            ])
        self.assertEqual(rc_rl, 0)
        self.assertEqual(len(loop_invocations), 1)
        inv = loop_invocations[0]
        self.assertIn("--goal", inv)
        self.assertEqual(inv[inv.index("--goal") + 1], "iron_tools")
        self.assertIn("--goal-telemetry", inv)
        self.assertEqual(inv[inv.index("--goal-telemetry") + 1], str(local_tel_path))

        spawned_dual: list[list[str]] = []

        class DummyProc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "init_session", return_value={}) as mock_init, \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=lambda c, **kw: (spawned_dual.append(list(c)), DummyProc())[1]):
            rc_dual = soak_ctl.main([
                "run",
                "--session-dir", str(self.session),
                "--branch", "feat/goal-telemetry",
                "--goal", "smelt_iron",
                "--max-goal-attempts", "5",
                "--episodes", "1",
            ])
        self.assertEqual(rc_dual, 0)
        self.assertEqual(mock_init.call_args.kwargs["target_goal"], "smelt_iron")
        self.assertEqual(mock_init.call_args.kwargs["max_goal_attempts"], 5)
        dual_runner = [c for c in spawned_dual if "runner-loop" in c][0]
        self.assertIn("--goal", dual_runner)
        self.assertEqual(dual_runner[dual_runner.index("--goal") + 1], "smelt_iron")

        soak_ctl.teardown_session(self.session)

    def test_set_goal_hot_inject_gate_rollback_and_live_runner_goal_switch(self):
        st = soak_ctl.init_session(
            self.repo,
            self.session,
            target_branch="feat/hot-inject",
            target_goal="smelt_iron",
            max_goal_attempts=3,
        )
        base_tip = st["current_tip"]

        # 在主工作区单独分支 feat/staging 上新增初版技能与台账变更（完全不打断 runner/fixer worktrees）
        _git(self.repo, "checkout", "-b", "feat/staging")
        (self.repo / "code" / "mcagent" / "skills" / "shield.py").write_text("SHIELD_V = 1\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "feat: add initial shield skill and candidate goal")

        real_run = subprocess.run

        # 1) 模拟台账静态核验失败（如忘记把上一个 candidate 改成 graduated 导致 WIP>1）：必须自动回退待审分支且不改 target_goal
        def mock_gate_fail_dry(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                if "--dry" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="validate_ledger: WIP>1 candidate")
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_gate_fail_dry):
            rc_fail = soak_ctl.main([
                "set-goal",
                "--session-dir", str(self.session),
                "--merge-ref", "feat/staging",
                "--goal", "craft_shield",
            ])
        self.assertEqual(rc_fail, 1)
        self.assertEqual(soak_ctl._branch_tip(self.repo, "feat/hot-inject"), base_tip)
        self.assertEqual(soak_ctl.read_state(self.session)["target_goal"], "smelt_iron")

        # 2) 修复后再次热合入（同时验证可清除遗留 STOP 文件）
        (self.session / "STOP").write_text("paused", encoding="utf-8")

        def mock_gate_ok(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                if "--run-dir" in cmd:
                    rdir = Path(cmd[cmd.index("--run-dir") + 1])
                    (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                    (rdir / "timeline.jsonl").write_text(
                        json.dumps({"event": "run_end", "elapsed_s": 12.0, "source": "local-target"}) + "\n",
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_gate_ok):
            rc_ok = soak_ctl.main([
                "set-goal",
                "--session-dir", str(self.session),
                "--merge-ref", "feat/staging",
                "--goal", "craft_shield",
                "--max-goal-attempts", "6",
            ])
        self.assertEqual(rc_ok, 0)
        self.assertFalse((self.session / "STOP").exists())
        st_injected = soak_ctl.read_state(self.session)
        self.assertNotEqual(st_injected["current_tip"], base_tip)
        self.assertEqual(st_injected["target_goal"], "craft_shield")
        self.assertEqual(st_injected["max_goal_attempts"], 6)
        self.assertTrue((Path(st["fixer_wt"]) / "code" / "mcagent" / "skills" / "shield.py").exists())

        # 3) 验证持续运行的 run_runner_loop 即使启动时带了 target_goal="smelt_iron"，在轮间 set-goal 后第 2 轮立即热切换到新目标与新 commit
        seen_goals: list[Optional[str]] = []

        def mock_two_ep_hot_switch(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv" and "--run-dir" in cmd:
                g = cmd[cmd.index("--goal") + 1] if "--goal" in cmd else None
                seen_goals.append(g)
                rdir = Path(cmd[cmd.index("--run-dir") + 1])
                (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                (rdir / "timeline.jsonl").write_text(
                    json.dumps({"event": "run_end", "elapsed_s": 10.0, "source": "local-target"}) + "\n",
                    encoding="utf-8",
                )
                if len(seen_goals) == 1:
                    # 在第 1 轮运行期间，外部调用 set-goal 热切换至 craft_shield，第 2 轮立即生效
                    soak_ctl.inject_and_set_goal(self.session, target_goal="craft_shield")
                elif len(seen_goals) == 2:
                    # 第 2 轮运行期间切回全量已毕业目标模式（--clear-goal）
                    soak_ctl.inject_and_set_goal(self.session, target_goal="")
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_two_ep_hot_switch):
            soak_ctl.run_runner_loop(
                self.session,
                episodes=3,
                target_goal="smelt_iron",
                use_llm=False,
            )
        self.assertEqual(seen_goals, ["smelt_iron", "craft_shield", None])

        soak_ctl.teardown_session(self.session)

    def test_fixer_presets_codex_61sol_xhigh_and_agy_hydrogen_high(self):
        codex_tmpl = soak_ctl.FIXER_PRESETS["codex"]
        self.assertIn("-m gpt-6.1-sol", codex_tmpl)
        self.assertIn('model_reasoning_effort="xhigh"', codex_tmpl)

        agy_tmpl = soak_ctl.FIXER_PRESETS["agy"]
        self.assertIn("agy", agy_tmpl)
        self.assertIn("--model gemini-4-hydrogen-high", agy_tmpl)
        self.assertIn("--effort high", agy_tmpl)
        self.assertIn('-p="$(cat {prompt_file})"', agy_tmpl)

        # 验证 run --fixer-preset agy 正确透传预设与 --fixer-timeout，且 $(cat ...) 不被 bash 展开 $ 符号
        spawned: list[list[str]] = []

        class DummyProc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        self.session.mkdir(parents=True, exist_ok=True)
        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "init_session", return_value={}), \
             patch.object(soak_ctl, "write_review_report", return_value=self.session / "REVIEW_REPORT.md"), \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=lambda c, **kw: (spawned.append(list(c)), DummyProc())[1]):
            rc = soak_ctl.main([
                "run",
                "--session-dir", str(self.session),
                "--branch", "feat/agy-preset",
                "--fixer-preset", "agy",
                "--fixer-timeout", "720",
                "--episodes", "1",
            ])
        self.assertEqual(rc, 0)
        fixer_argv = [c for c in spawned if "fixer-loop" in c][0]
        self.assertIn("--fixer-cmd", fixer_argv)
        self.assertEqual(fixer_argv[fixer_argv.index("--fixer-cmd") + 1], agy_tmpl)
        self.assertIn("--fixer-timeout", fixer_argv)
        self.assertEqual(fixer_argv[fixer_argv.index("--fixer-timeout") + 1], "720")

        # 验证 $(cat {prompt_file}) 对含 $VAR 与反引号的工单文本零意外 shell 展开
        prompt_p = self.root / "prompt_with_special.md"
        prompt_p.write_text("line1 $HOME `uname`\nline2 /api/v1/act\n", encoding="utf-8")
        rendered = 'printf "%s" "$(cat ' + shlex.quote(str(prompt_p)) + ')"'
        out = subprocess.run(["bash", "-lc", rendered], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "line1 $HOME `uname`\nline2 /api/v1/act")

    def test_default_player_model_deepseek_and_live_env_injection(self):
        self.assertEqual(soak_ctl.DEFAULT_PLAYER_MODEL, "DeepSeek-V4.1-Flash")
        st = soak_ctl.init_session(
            self.repo,
            self.session,
            target_branch="feat/ds-player",
        )
        self.assertEqual(st["player_model"], "DeepSeek-V4.1-Flash")

        seen_models: list[Optional[str]] = []
        real_run = subprocess.run

        def mock_runner_env_capture(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv" and "--run-dir" in cmd:
                env = kwargs.get("env") or {}
                seen_models.append(env.get("LLM_CUSTOM_MODEL"))
                rdir = Path(cmd[cmd.index("--run-dir") + 1])
                (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                (rdir / "timeline.jsonl").write_text(
                    json.dumps({"event": "run_end", "elapsed_s": 8.0, "source": "local-target"}) + "\n",
                    encoding="utf-8",
                )
                if len(seen_models) == 1:
                    soak_ctl.inject_and_set_goal(self.session, player_model="DeepSeek-V4-Pro-0813")
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_runner_env_capture):
            soak_ctl.run_runner_loop(self.session, episodes=2, use_llm=True)
        self.assertEqual(seen_models, ["DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro-0813"])
        report_txt = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("`DeepSeek-V4-Pro-0813`", report_txt)

        # 验证主干新增提交后重新 init 同一会话，已有待审分支与双 worktree 自动同步最新主干，且回收孤儿 claimed 单
        (self.repo / "code" / "mcagent" / "loop" / "hotfix.py").write_text("FIXED = True\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "fix: main hotfix after session created")
        new_main_sha = _git(self.repo, "rev-parse", "HEAD")

        with soak_ctl.locked_state(self.session) as st_mut:
            st_mut["incidents"]["orphan_fp"] = {
                "fingerprint": "orphan_fp",
                "incident_id": "INC-ORPHAN",
                "code": "LLM_INVALID_OR_TIMEOUT",
                "severity": "P3",
                "category": "strategy",
                "status": "claimed",
                "attempts": 0,
            }

        st_reinit = soak_ctl.init_session(self.repo, self.session, target_branch="feat/ds-player")
        self.assertEqual(st_reinit["current_tip"], new_main_sha)
        self.assertTrue((Path(st_reinit["runner_wt"]) / "code" / "mcagent" / "loop" / "hotfix.py").exists())
        self.assertEqual(st_reinit["incidents"]["orphan_fp"]["status"], "pending")
        soak_ctl.teardown_session(self.session)

    def test_set_goal_hot_episode_budget_override_bounds_and_clear(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/ep-budget")

        # 1) 越界与互斥参数在合入前直接拒绝，不写 state.json
        for kwargs in (
            {"episode_seconds": 10.0},
            {"episode_seconds": 99999.0},
            {"episode_steps": 0},
            {"episode_steps": 5000},
            {"clear_episode_budget": True, "episode_steps": 60},
        ):
            res = soak_ctl.inject_and_set_goal(self.session, **kwargs)
            self.assertFalse(res["ok"], kwargs)
            self.assertEqual(res["failed_step"], "invalid_episode_budget")
        st0 = soak_ctl.read_state(self.session)
        self.assertNotIn("episode_seconds", st0)
        self.assertNotIn("episode_steps", st0)

        # 2) 持续运行的 runner：第 1 轮沿用启动参数，轮间热改后第 2 轮生效，清除后第 3 轮回到启动参数
        seen: list[tuple[str, str]] = []
        real_run = subprocess.run

        def mock_runner(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv" and "--run-dir" in cmd:
                seen.append((cmd[cmd.index("--max-seconds") + 1], cmd[cmd.index("--max-steps") + 1]))
                rdir = Path(cmd[cmd.index("--run-dir") + 1])
                (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                (rdir / "timeline.jsonl").write_text(
                    json.dumps({"event": "run_end", "elapsed_s": 5.0, "source": "local-target"}) + "\n",
                    encoding="utf-8",
                )
                if len(seen) == 1:
                    ok = soak_ctl.inject_and_set_goal(self.session, episode_seconds=420, episode_steps=80)
                    self.assertTrue(ok["ok"])
                    self.assertEqual(
                        ok["episode_budget_override"], {"episode_seconds": 420.0, "episode_steps": 80}
                    )
                elif len(seen) == 2:
                    self.assertTrue(soak_ctl.inject_and_set_goal(self.session, clear_episode_budget=True)["ok"])
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_runner):
            soak_ctl.run_runner_loop(
                self.session, episodes=3, episode_seconds=240.0, episode_steps=45, use_llm=False
            )
        self.assertEqual(seen, [("240.0", "45"), ("420.0", "80"), ("240.0", "45")])

        # 3) 只改一项时另一项沿用启动参数；报告与 CLI 透传
        self.assertTrue(soak_ctl.inject_and_set_goal(self.session, episode_steps=120)["ok"])
        self.assertEqual(
            soak_ctl.effective_episode_budget(soak_ctl.read_state(self.session), 240.0, 45), (240.0, 120)
        )
        report_txt = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("每轮时长/步数热覆盖", report_txt)
        self.assertIn("120 步", report_txt)

        # 4) 手改 state.json 写入非法值时 runner 不崩，回落启动参数
        with soak_ctl.locked_state(self.session) as st_mut:
            st_mut["episode_seconds"] = "abc"
        self.assertEqual(
            soak_ctl.effective_episode_budget(soak_ctl.read_state(self.session), 240.0, 45), (240.0, 45)
        )

        with patch.object(soak_ctl, "inject_and_set_goal", return_value={"ok": True}) as mock_inj:
            rc = soak_ctl.main([
                "set-goal", "--session-dir", str(self.session),
                "--goal", "smelt_iron", "--episode-seconds", "420", "--episode-steps", "80",
            ])
        self.assertEqual(rc, 0)
        kw = mock_inj.call_args.kwargs
        self.assertEqual((kw["episode_seconds"], kw["episode_steps"], kw["clear_episode_budget"]), (420.0, 80, False))
        soak_ctl.teardown_session(self.session)


    def test_run_keeps_same_bot_name_each_episode_unless_rotate(self):
        class DummyProc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def runner_cmd_for(extra: list[str]) -> list[str]:
            spawned: list[list[str]] = []
            with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
                 patch.object(soak_ctl, "init_session", return_value={}), \
                 patch.object(soak_ctl.subprocess, "Popen",
                              side_effect=lambda cmd, **kw: (spawned.append(list(cmd)), DummyProc())[1]):
                rc = soak_ctl.main(["run", "--session-dir", str(self.session), "--branch", "feat/fixed-bot",
                                    "--episodes", "1", *extra])
            self.assertEqual(rc, 0)
            return next(c for c in spawned if "runner-loop" in c)

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/fixed-bot")
        default_cmd = runner_cmd_for([])
        self.assertEqual(default_cmd[default_cmd.index("--user-prefix") + 1], soak_ctl.DEFAULT_BOT_USER)
        self.assertIn("--fixed-user", default_cmd)
        rotate_cmd = runner_cmd_for(["--bot-user", "team_bot", "--rotate-user"])
        self.assertEqual(rotate_cmd[rotate_cmd.index("--user-prefix") + 1], "team_bot")
        self.assertNotIn("--fixed-user", rotate_cmd)
        self.assertEqual(soak_ctl.main(["run", "--session-dir", str(self.session), "--bot-user", "bad name!"]), 2)
        with self.assertRaises(ValueError):
            soak_ctl.validate_bot_user("x" * 14)
        with patch.object(soak_ctl.getpass, "getuser", return_value="Alice.Wang-PC"):
            self.assertEqual(soak_ctl.default_bot_user(), "AliceWan_soak")
        with patch.object(soak_ctl.getpass, "getuser", side_effect=OSError("no user")):
            self.assertEqual(soak_ctl.default_bot_user(), "agy_soak_bot")
        soak_ctl.validate_bot_user(soak_ctl.DEFAULT_BOT_USER)

        # 同名挂机：每轮进服都是同一个角色名，轮次号只出现在 ep-NNNN 记录里
        (self.session / "STOP").unlink(missing_ok=True)
        users: list[str] = []
        real_run = subprocess.run

        def mock_runner(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv" and "--run-dir" in cmd:
                users.append(cmd[cmd.index("--user") + 1])
                rdir = Path(cmd[cmd.index("--run-dir") + 1])
                (rdir / "incidents").mkdir(parents=True, exist_ok=True)
                (rdir / "timeline.jsonl").write_text(
                    json.dumps({"event": "run_end", "elapsed_s": 5.0, "source": "local-target"}) + "\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_runner):
            soak_ctl.run_runner_loop(self.session, episodes=2, use_llm=False,
                                     user_prefix="agy_soak_bot", rotate_user=False)
        self.assertEqual(users, ["agy_soak_bot", "agy_soak_bot"])
        st = soak_ctl.read_state(self.session)
        self.assertEqual([e["ep_id"] for e in st["episodes"]], ["ep-0001", "ep-0002"])
        self.assertEqual({e["bot_user"] for e in st["episodes"]}, {"agy_soak_bot"})
        soak_ctl.teardown_session(self.session)

    def test_fixer_model_catalog_hot_switch_and_per_machine_fixer_user(self):
        cat = soak_ctl.FIXER_PRESETS
        for eff in ("low", "medium", "high", "xhigh", "max"):
            for model in ("gpt-6.1-sol", "gpt-6-astra"):
                tmpl = cat[f"codex:{model}:{eff}"]
                self.assertIn(f"-m {model} ", tmpl)
                self.assertIn(f'model_reasoning_effort="{eff}"', tmpl)
        for eff in ("low", "medium", "high", "xhigh"):
            self.assertIn(f"--model gemini-4-hydrogen-{eff} --effort {eff} ", cat[f"agy:gemini-4-hydrogen-{eff}"])
        for eff in ("low", "medium", "high"):
            self.assertIn(f"--model gemini-4-carbon-{eff} --effort {eff} ", cat[f"agy:gemini-4-carbon-{eff}"])
        self.assertNotIn("agy:gemini-4-carbon-xhigh", cat)
        self.assertEqual(cat["codex"], cat["codex:gpt-6.1-sol:xhigh"])
        self.assertEqual(cat["agy"], cat["agy:gemini-4-hydrogen-high"])
        self.assertEqual(set(cat), set(soak_ctl.FIXER_PRESET_LABELS))
        for tmpl in cat.values():  # 模板只留 {fixer_wt} {prompt_file} 两个占位
            tmpl.format(fixer_wt="W", prompt_file="P", incident_file="I", skill="S")

        with patch.object(soak_ctl.getpass, "getuser", return_value="Alice.Wang-PC"):
            self.assertEqual(soak_ctl.default_fixer_user(), "AliceWan_fixer")

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/fixer-hot")
        # 没有热切换时沿用启动参数
        self.assertEqual(soak_ctl.resolve_fixer_template(self.session, "codex", "T0"), ("codex", "T0"))
        bad = soak_ctl.inject_and_set_goal(self.session, fixer_preset="codex:gpt-9:max")
        self.assertEqual((bad["ok"], bad["failed_step"]), (False, "invalid_fixer_preset"))
        ok = soak_ctl.inject_and_set_goal(self.session, fixer_preset="agy:gemini-4-carbon-medium")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["fixer_preset"], "agy:gemini-4-carbon-medium")
        name, tmpl = soak_ctl.resolve_fixer_template(self.session, "codex", "T0")
        self.assertEqual(name, "agy:gemini-4-carbon-medium")
        self.assertIn("gemini-4-carbon-medium", tmpl)
        report = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("Gemini 4 Carbon · medium", report)
        # 手改出的未知名字不生效，回落启动参数
        with soak_ctl.locked_state(self.session) as st_mut:
            st_mut["fixer_preset"] = "nope"
        self.assertEqual(soak_ctl.resolve_fixer_template(self.session, "codex", "T0"), ("codex", "T0"))

        # fixer-loop 每接一单前重新解析，并把用到的模型记到异常单上
        with soak_ctl.locked_state(self.session) as st_mut:
            st_mut["fixer_preset"] = "codex:gpt-6-astra:high"
            st_mut["incidents"]["fp1"] = {
                "fingerprint": "fp1", "incident_id": "INC-001", "code": "X", "severity": "P1",
                "category": "bug", "status": "pending", "attempts": 0,
            }
        seen: list[str] = []

        def fake_once(session_dir, **kw):
            seen.append(kw["fixer_cmd_template"])
            self.assertEqual(kw["fixer_preset_name"], "codex:gpt-6-astra:high")
            return None

        with patch.object(soak_ctl, "run_fixer_once", side_effect=fake_once):
            rc = soak_ctl.main(["fixer-loop", "--session-dir", str(self.session), "--fixer-preset", "codex", "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("-m gpt-6-astra", seen[0])

        claimed = {"fingerprint": "fp1", "incident_id": "INC-001", "prompt_file": "/p", "incident_path": "/i",
                   "active_attempt_dir": str(self.root / "att")}
        (self.root / "att").mkdir()
        with patch.object(soak_ctl, "claim_next_incident", return_value=claimed), \
             patch.object(soak_ctl, "_clean_fixer_worktree"), patch.object(soak_ctl, "_git"), \
             patch.object(soak_ctl.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), \
             patch.object(soak_ctl, "verify_and_promote", return_value={"ok": True}):
            soak_ctl.run_fixer_once(self.session, fixer_cmd_template="echo {prompt_file}",
                                    fixer_preset_name="codex:gpt-6-astra:high")
        self.assertEqual(soak_ctl.read_state(self.session)["incidents"]["fp1"]["last_fixer_preset"],
                         "codex:gpt-6-astra:high")
        soak_ctl.teardown_session(self.session)

if __name__ == "__main__":
    unittest.main()
