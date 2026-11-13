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
        (ep1 / "progress_curve.jsonl").write_text(json.dumps({"server_score_v2": 15, "inferred_score_v2": 20}) + "\n")
        (ep1 / "timeline.jsonl").write_text(
            json.dumps({"event": "skill_end", "outcome": "completed"}) + "\n"
            + json.dumps({"event": "run_end", "elapsed_s": 60.0}) + "\n"
        )
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
        self.assertIn("250", (runner_wt / "code" / "mcagent" / "loop" / "starter_skills.py").read_text())

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
        self.assertIn("40", (runner_wt / "code" / "mcagent" / "loop" / "starter_skills.py").read_text())

        # 6) teardown 卸载临时双 worktree，但保留 feat/soak-autofix 分支与 REVIEW_REPORT.md
        report_path = soak_ctl.teardown_session(self.session)
        self.assertFalse(fixer_wt.exists())
        self.assertFalse(runner_wt.exists())
        self.assertTrue(report_path.exists())
        self.assertIn("reverted", report_path.read_text(encoding="utf-8"))
        # main 分支全程纹丝未动
        self.assertNotEqual(
            _git(self.repo, "rev-parse", "refs/heads/main"),
            _git(self.repo, "rev-parse", "refs/heads/feat/soak-autofix"),
        )


if __name__ == "__main__":
    unittest.main()
