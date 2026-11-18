"""Offline unit tests for proactive fixer upgrades in tools/soak-autofix/soak_ctl.py (Task #40).

Covers:
1. `_effective_goal_narrow_files`, `is_allowed_edit_path`, `_incident_edit_scope`,
   `claim_parallel_incidents` conflict matrix on recurrence, and end-to-end Gate 1
   expansion via `ingest_episode_and_check_rollback` + `verify_and_promote`.
2. Gate 1 anti-test-only guard in `_check_gate1_in_worktree`, `verify_and_promote`,
   and `run_fixer_batch`.
3. `_extract_prompt_episode_context` and `_render_fixer_prompt` (direct `episode_profile`
   rendering, `ep_dir` disk backfill with corrupt lines, `## 1C` / `## 3B` placement,
   `allow_subagents=False`, recurrence unlocking, and `PROACTIVE_WIDE_INCIDENT_CODES`).
4. Gate 3 live canary command routing, `canary_summary` recording in `gate_history`,
   and `gate3_live_canary_stopped_out` enforcement.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_soak_ctl():
    repo_root = Path(__file__).resolve().parents[2]
    mod_path = repo_root / "tools" / "soak-autofix" / "soak_ctl.py"
    spec = importlib.util.spec_from_file_location("soak_ctl_proactive_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


soak_ctl = _load_soak_ctl()

ANTI_TEST_ONLY_DETAIL = (
    "仅改动测试文件而未修改任何生产代码（code/mcagent/**/*.py）；"
    "禁止只写测试敷衍交差，请真正修复根因"
)
WIDE_SCOPE_GUIDANCE = (
    "你有权跨 `loop/*.py` 与 `skills/*.py` 协同根治调度饥饿、无效重复刷技能与频繁探测瓶颈"
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _create_base_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Soak Tester")

    (repo / "code" / "mcagent" / "loop").mkdir(parents=True)
    (repo / "code" / "mcagent" / "skills").mkdir(parents=True)
    (repo / "code" / "mcagent" / "planner").mkdir(parents=True)
    (repo / "code" / "tests").mkdir(parents=True)

    (repo / "code" / "mcagent" / "client.py").write_text("# frozen client\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "runtime.py").write_text("# frozen runtime\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "base.py").write_text("# frozen base\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "registry.py").write_text("# frozen registry\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "testing.py").write_text("# frozen testing\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "planner" / "decision.py").write_text("# frozen decision\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "loop" / "policy.py").write_text("POLICY = 1\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 40\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "loop" / "goals.py").write_text("GOALS = {}\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "combat.py").write_text("# combat\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "diamond.py").write_text("# diamond\n", encoding="utf-8")
    (repo / "code" / "mcagent" / "skills" / "iron.py").write_text("# iron\n", encoding="utf-8")
    (repo / "code" / "tests" / "test_dummy.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init baseline")
    return repo


def _uv_mock_dispatcher(uv_commands: list[list[str]], canary_events: list[dict] | None = None):
    real_run = subprocess.run

    def _mock(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "uv":
            uv_commands.append(list(cmd))
            if "--run-dir" in cmd and canary_events is not None:
                run_dir = Path(cmd[cmd.index("--run-dir") + 1])
                run_dir.mkdir(parents=True, exist_ok=True)
                lines = "\n".join(json.dumps(ev, ensure_ascii=False) for ev in canary_events) + "\n"
                (run_dir / "timeline.jsonl").write_text(lines, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
        return real_run(cmd, *args, **kwargs)

    return _mock


class TestEffectiveGoalNarrowFilesAndParallelScope(unittest.TestCase):
    """验证 _effective_goal_narrow_files、冻结路径拦截、并发冲突与 Gate 1 复发扩权闭环。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _create_base_repo(self.root)
        self.session = self.root / "session"

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_goal_miss_or_empty_editable_files_returns_none_and_wildcard_scope(self):
        cases = [
            None,
            {"code": "POSTURE_LEAK", "editable_files": ["code/mcagent/skills/combat.py"]},
            {
                "code": "DISPATCH_STARVATION",
                "editable_files": ["code/mcagent/skills/combat.py"],
                "last_failed_fix_commit": "deadbeef12345678",
            },
            {"code": "GOAL_MISS", "editable_files": []},
            {"code": "GOAL_MISS", "editable_files": None},
            {"code": "GOAL_MISS", "editable_files": ["", "   ", "\t\n"]},
        ]
        for inc in cases:
            self.assertIsNone(soak_ctl._effective_goal_narrow_files(inc))
            if isinstance(inc, dict):
                self.assertEqual(soak_ctl._incident_edit_scope(inc), frozenset({"*"}))

    def test_first_time_goal_miss_normalizes_and_dedups_without_extra_loop_files(self):
        inc = {
            "code": "GOAL_MISS",
            "editable_files": [
                " ./code/mcagent/skills/combat.py ",
                "code\\mcagent\\skills\\combat.py",
                "code/mcagent/skills/iron.py",
                "   ",
            ],
            "last_failed_fix_commit": "   ",
        }
        narrow = soak_ctl._effective_goal_narrow_files(inc)
        self.assertEqual(
            narrow,
            ["code/mcagent/skills/combat.py", "code/mcagent/skills/iron.py"],
        )
        for extra in (
            "code/mcagent/loop/policy.py",
            "code/mcagent/loop/starter_skills.py",
            "code/mcagent/loop/goals.py",
        ):
            self.assertNotIn(extra, narrow)

    def test_recurrence_goal_miss_appends_loop_files_and_frozen_paths_still_rejected(self):
        inc = {
            "code": "GOAL_MISS",
            "editable_files": [
                "code/mcagent/skills/combat.py",
                "code/mcagent/loop/policy.py",  # already present -> must stay deduplicated & preserve order
                "code/mcagent/client.py",  # even if injected into editable_files
            ],
            "last_failed_fix_commit": "deadbeef12345678",
        }
        narrow = soak_ctl._effective_goal_narrow_files(inc)
        self.assertEqual(
            narrow,
            [
                "code/mcagent/skills/combat.py",
                "code/mcagent/loop/policy.py",
                "code/mcagent/client.py",
                "code/mcagent/loop/starter_skills.py",
                "code/mcagent/loop/goals.py",
            ],
        )

        # Frozen paths must STILL be rejected by is_allowed_edit_path even if injected
        injected_narrow = list(narrow) + [
            "code/mcagent/runtime.py",
            "code/mcagent/skills/base.py",
        ]
        for frozen in (
            "code/mcagent/client.py",
            "code/mcagent/runtime.py",
            "code/mcagent/skills/base.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(frozen, narrow_files=injected_narrow)
            self.assertFalse(ok, f"expected frozen path {frozen} to be rejected")
            self.assertIn("命中冻结保护文件", why)

        for allowed in (
            "code/mcagent/loop/policy.py",
            "code/mcagent/loop/starter_skills.py",
            "code/mcagent/loop/goals.py",
        ):
            ok, why = soak_ctl.is_allowed_edit_path(allowed, narrow_files=narrow)
            self.assertTrue(ok, f"expected {allowed} allowed, got: {why}")

    def test_parallel_claim_disjoint_initially_then_conflicts_after_recurrence(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        base_incidents = {
            "GOAL_MISS:combat": {
                "incident_id": "INC-C1",
                "fingerprint": "GOAL_MISS:combat",
                "code": "GOAL_MISS",
                "goal_id": "kill_mob",
                "skill": "combat",
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "editable_files": ["code/mcagent/skills/combat.py"],
                "attempts": 0,
            },
            "GOAL_MISS:diamond": {
                "incident_id": "INC-D1",
                "fingerprint": "GOAL_MISS:diamond",
                "code": "GOAL_MISS",
                "goal_id": "mine_diamond",
                "skill": "diamond",
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "editable_files": ["code/mcagent/skills/diamond.py"],
                "attempts": 0,
            },
        }
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = json.loads(json.dumps(base_incidents))

        claimed_first = soak_ctl.claim_parallel_incidents(self.session, max_workers=2)
        self.assertEqual(
            [c["fingerprint"] for c in claimed_first],
            ["GOAL_MISS:combat", "GOAL_MISS:diamond"],
        )

        # Reset to pending with last_failed_fix_commit set on BOTH -> share loop/{policy,starter_skills,goals}.py
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = json.loads(json.dumps(base_incidents))
            for rec in st["incidents"].values():
                rec["last_failed_fix_commit"] = "deadbeef12345678"

        scope_c = soak_ctl._incident_edit_scope(soak_ctl.read_state(self.session)["incidents"]["GOAL_MISS:combat"])
        scope_d = soak_ctl._incident_edit_scope(soak_ctl.read_state(self.session)["incidents"]["GOAL_MISS:diamond"])
        self.assertIn("code/mcagent/loop/policy.py", scope_c)
        self.assertIn("code/mcagent/loop/policy.py", scope_d)
        self.assertTrue(soak_ctl._scopes_conflict(scope_c, scope_d))

        claimed_recur = soak_ctl.claim_parallel_incidents(self.session, max_workers=2)
        self.assertEqual(len(claimed_recur), 1)
        st_after = soak_ctl.read_state(self.session)
        statuses = {fp: rec["status"] for fp, rec in st_after["incidents"].items()}
        self.assertEqual(sorted(statuses.values()), ["claimed", "pending"])

    def test_end_to_end_gate1_expansion_via_ingest_and_verify_and_promote(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        ep1_inc_dir = self.session / "episodes" / "ep-0001" / "incidents"
        ep1_inc_dir.mkdir(parents=True)
        ep1_inc_path = ep1_inc_dir / "INC-001.json"
        inc_payload = {
            "incident_id": "INC-001",
            "fingerprint": "GOAL_MISS:smelt_iron",
            "code": "GOAL_MISS",
            "severity": "P1",
            "category": "strategy",
            "skill": "iron",
            "goal_id": "smelt_iron",
            "editable_files": ["code/mcagent/skills/iron.py"],
        }
        ep1_inc_path.write_text(json.dumps(inc_payload), encoding="utf-8")

        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:smelt_iron": {
                    **inc_payload,
                    "status": "pending",
                    "attempts": 0,
                    "incident_path": str(ep1_inc_path),
                    "fix_commit": None,
                    "gate_history": [],
                }
            }

        st0 = soak_ctl.read_state(self.session)
        fixer_wt = Path(st0["fixer_wt"])
        uv_cmds: list[list[str]] = []

        # Attempt 1: editing policy.py before recurrence must be rejected by Gate 1
        soak_ctl.claim_next_incident(self.session)
        (fixer_wt / "code" / "mcagent" / "loop" / "policy.py").write_text("POLICY = 2\n", encoding="utf-8")
        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds)):
            res_reject = soak_ctl.verify_and_promote(
                self.session, "GOAL_MISS:smelt_iron", live_canary=False
            )
        self.assertFalse(res_reject["ok"])
        self.assertEqual(res_reject["failed_gate"], "gate1_whitelist")
        self.assertIn("超出目标单收窄白名单", res_reject["detail"])
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")

        # Attempt 2: valid narrow edit on iron.py passes and commits fix_commit
        soak_ctl.claim_next_incident(self.session)
        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text("# iron fix v1\n", encoding="utf-8")
        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds)):
            res_fix1 = soak_ctl.verify_and_promote(
                self.session, "GOAL_MISS:smelt_iron", live_canary=False
            )
        self.assertTrue(res_fix1["ok"])
        fix1_sha = res_fix1["commit"]

        # Next episode on fix1_sha still fails with GOAL_MISS:smelt_iron -> populates last_failed_fix_commit
        ep2_dir = self.session / "episodes" / "ep-0002"
        (ep2_dir / "incidents").mkdir(parents=True)
        (ep2_dir / "incidents" / "INC-002.json").write_text(
            json.dumps({**inc_payload, "incident_id": "INC-002"}),
            encoding="utf-8",
        )
        (ep2_dir / "timeline.jsonl").write_text(
            json.dumps({"event": "run_end", "target_goal": "smelt_iron", "goals_reached": {}}) + "\n",
            encoding="utf-8",
        )
        soak_ctl.ingest_episode_and_check_rollback(
            self.session, "ep-0002", ep2_dir, fix1_sha, "soak_r_002"
        )

        st_recur = soak_ctl.read_state(self.session)
        recur_rec = st_recur["incidents"]["GOAL_MISS:smelt_iron"]
        self.assertEqual(recur_rec["status"], "pending")
        self.assertEqual(recur_rec["last_failed_fix_commit"], fix1_sha)

        # Attempt 3: editing policy.py, starter_skills.py, goals.py now passes Gate 1
        soak_ctl.claim_next_incident(self.session)
        (fixer_wt / "code" / "mcagent" / "loop" / "policy.py").write_text("POLICY = 3\n", encoding="utf-8")
        (fixer_wt / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 50\n", encoding="utf-8")
        (fixer_wt / "code" / "mcagent" / "loop" / "goals.py").write_text("GOALS = {'smelt_iron': 1}\n", encoding="utf-8")
        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds)):
            res_fix2 = soak_ctl.verify_and_promote(
                self.session, "GOAL_MISS:smelt_iron", live_canary=False
            )
        self.assertTrue(res_fix2["ok"], f"expected expanded narrow files to pass Gate 1: {res_fix2}")
        self.assertIn("code/mcagent/loop/policy.py", res_fix2["changed_files"])
        self.assertIn("code/mcagent/loop/starter_skills.py", res_fix2["changed_files"])
        self.assertIn("code/mcagent/loop/goals.py", res_fix2["changed_files"])


class TestGate1AntiTestOnlyGuard(unittest.TestCase):
    """验证 Gate 1 防纯测试敷衍机制（空改动、纯单测改动、生产+单测混合、并发批次上报）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _create_base_repo(self.root)
        self.session = self.root / "session"
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_incident(self, fp: str = "BUG:iron", code: str = "POSTURE_LEAK", skill: str = "iron", editable_files=None):
        rec = {
            "incident_id": "INC-T1",
            "fingerprint": fp,
            "code": code,
            "skill": skill,
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        }
        if editable_files is not None:
            rec["editable_files"] = editable_files
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"][fp] = rec

    def test_empty_worktree_change_still_reports_empty_message(self):
        self._seed_incident()
        fixer_wt = Path(soak_ctl.read_state(self.session)["fixer_wt"])
        inc_rec = soak_ctl.read_state(self.session)["incidents"]["BUG:iron"]

        changed, err = soak_ctl._check_gate1_in_worktree(fixer_wt, inc_rec)
        self.assertEqual(changed, [])
        self.assertEqual(err, ("gate1_whitelist", "fixer 工位没有任何文件改动"))

        res = soak_ctl.verify_and_promote(self.session, "BUG:iron", live_canary=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_gate"], "gate1_whitelist")
        self.assertEqual(res["detail"], "fixer 工位没有任何文件改动")

    def test_test_only_add_or_modify_rejected_by_gate1(self):
        self._seed_incident()
        fixer_wt = Path(soak_ctl.read_state(self.session)["fixer_wt"])
        inc_rec = soak_ctl.read_state(self.session)["incidents"]["BUG:iron"]

        # Case A: only adding a new test file
        (fixer_wt / "code" / "tests" / "test_soak_dummy.py").write_text("assert True\n", encoding="utf-8")
        changed, err = soak_ctl._check_gate1_in_worktree(fixer_wt, inc_rec)
        self.assertEqual(changed, ["code/tests/test_soak_dummy.py"])
        self.assertEqual(err, ("gate1_whitelist", ANTI_TEST_ONLY_DETAIL))

        res = soak_ctl.verify_and_promote(self.session, "BUG:iron", live_canary=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_gate"], "gate1_whitelist")
        self.assertEqual(res["detail"], ANTI_TEST_ONLY_DETAIL)
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")

        # Case B: only modifying an existing tracked test file
        (fixer_wt / "code" / "tests" / "test_dummy.py").write_text("x = 2\n", encoding="utf-8")
        res2 = soak_ctl.verify_and_promote(self.session, "BUG:iron", live_canary=False)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["failed_gate"], "gate1_whitelist")
        self.assertEqual(res2["detail"], ANTI_TEST_ONLY_DETAIL)

    def test_prod_plus_test_change_passes_gate1_and_promotes(self):
        self._seed_incident()
        fixer_wt = Path(soak_ctl.read_state(self.session)["fixer_wt"])
        inc_rec = soak_ctl.read_state(self.session)["incidents"]["BUG:iron"]

        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text("# real fix\n", encoding="utf-8")
        (fixer_wt / "code" / "tests" / "test_soak_dummy.py").write_text("def test_ok(): pass\n", encoding="utf-8")

        changed, err = soak_ctl._check_gate1_in_worktree(fixer_wt, inc_rec)
        self.assertIsNone(err)
        self.assertIn("code/mcagent/skills/iron.py", changed)
        self.assertIn("code/tests/test_soak_dummy.py", changed)

        uv_cmds: list[list[str]] = []
        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds)):
            res = soak_ctl.verify_and_promote(self.session, "BUG:iron", live_canary=False)
        self.assertTrue(res["ok"], f"expected prod+test to pass: {res}")
        self.assertIn("code/mcagent/skills/iron.py", res["changed_files"])
        self.assertIn("code/tests/test_soak_dummy.py", res["changed_files"])

    def test_run_fixer_batch_preserves_test_only_gate1_detail(self):
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:combat": {
                    "incident_id": "INC-C1",
                    "fingerprint": "GOAL_MISS:combat",
                    "code": "GOAL_MISS",
                    "skill": "combat",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/combat.py"],
                    "attempts": 0,
                    "gate_history": [],
                },
                "GOAL_MISS:diamond": {
                    "incident_id": "INC-D1",
                    "fingerprint": "GOAL_MISS:diamond",
                    "code": "GOAL_MISS",
                    "skill": "diamond",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                    "gate_history": [],
                },
            }

        worker_script = self.root / "test_only_worker.py"
        worker_script.write_text(
            """import sys
from pathlib import Path

wt = Path(sys.argv[1])
skill = sys.argv[2]

if skill == "combat":
    (wt / "code" / "tests" / "test_soak_dummy.py").write_text("# test only\\n", encoding="utf-8")
else:
    (wt / "code" / "mcagent" / "skills" / "diamond.py").write_text("# diamond fixed\\n", encoding="utf-8")
    (wt / "code" / "tests" / "test_soak_diamond.py").write_text("OK = True\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"
        uv_cmds: list[list[str]] = []

        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds)):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        by_fp = {r["fingerprint"]: r for r in results}
        bad = by_fp["GOAL_MISS:combat"]
        good = by_fp["GOAL_MISS:diamond"]

        self.assertFalse(bad["ok"])
        self.assertEqual(bad["failed_gate"], "gate1_whitelist")
        self.assertEqual(bad["detail"], ANTI_TEST_ONLY_DETAIL)
        self.assertNotEqual(bad["detail"], "fixer 工位没有任何文件改动")

        self.assertTrue(good["ok"])

        st_after = soak_ctl.read_state(self.session)
        combat_rec = st_after["incidents"]["GOAL_MISS:combat"]
        self.assertEqual(combat_rec["attempts"], 1)
        self.assertEqual(combat_rec["gate_history"][-1]["failed_gate"], "gate1_whitelist")
        self.assertEqual(combat_rec["gate_history"][-1]["detail"], ANTI_TEST_ONLY_DETAIL)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:diamond"]["status"], "fixed")


class TestEpisodeContextAndPromptRendering(unittest.TestCase):
    """验证全局运行剖面提取、磁盘兜底回填与修单 Prompt 各区块/红线渲染。"""

    def test_direct_episode_profile_rendering_truncates_http_top5_and_last12_decisions(self):
        decisions = [
            {
                "event": "skill_start",
                "elapsed_s": float(i),
                "skill": f"skill_{i:02d}",
                "args": {"step": i},
                "source": "llm" if i % 2 == 0 else "rule",
                "reason": f"reason_{i:02d}",
            }
            for i in range(1, 17)
        ]
        inc_rec = {
            "incident_id": "INC-201",
            "fingerprint": "GOAL_MISS:smelt_iron",
            "code": "GOAL_MISS",
            "goal_id": "smelt_iron",
            "skill": "smelt_iron",
            "severity": "P1",
            "category": "strategy",
            "editable_files": ["code/mcagent/skills/iron.py"],
            "incident_path": "/tmp/nonexistent/ep-0001/incidents/INC-201.json",
        }
        inc_json = {
            "detail": {
                "goal_id": "smelt_iron",
                "goal_title": "Smelt Iron",
                "tier": "tier_2",
                "score": 20,
                "summary": "never smelted iron",
                "elapsed_s": 100.0,
                "budget_s": 180.0,
                "stop_reason": "timeout",
                "bottleneck_goal_id": "smelt_iron",
                "bottleneck_skill": "smelt_iron",
                "target_skill_started": False,
            },
            "episode_profile": {
                "elapsed_s": 100.0,
                "llm_calls": 18,
                "llm_fallbacks": 3,
                "decision_sources": {"llm": 12, "rule": 5, "llm_fallback_rule": 3},
                "http_top_paths": [
                    {"path": "state", "count": 40, "total_s": 25.0, "pct": 25.0},
                    {"path": "nearby", "count": 30, "total_s": 20.0, "pct": 20.0},
                    {"path": "dig", "count": 15, "total_s": 15.0, "pct": 15.0},
                    {"path": "move", "count": 10, "total_s": 10.0, "pct": 10.0},
                    {"path": "craft", "count": 5, "total_s": 5.0, "pct": 5.0},
                    {"path": "chat", "count": 2, "total_s": 1.0, "pct": 1.0},
                ],
                "repeated_skill_args": [
                    {"skill": "gather_wood", "args": {"count": 4}, "count": 7},
                ],
                "recent_decisions": decisions,
                "summary_path": "/tmp/ep-0001/summary.md",
                "timeline_path": "/tmp/ep-0001/timeline.jsonl",
            },
        }

        ctx = soak_ctl._extract_prompt_episode_context(inc_json, inc_rec["incident_path"])
        self.assertEqual(len(ctx["http_top_paths"]), 5)
        self.assertEqual([p["path"] for p in ctx["http_top_paths"]], ["state", "nearby", "dig", "move", "craft"])
        self.assertEqual(len(ctx["recent_decisions"]), 12)
        self.assertEqual(ctx["recent_decisions"][0]["skill"], "skill_05")
        self.assertEqual(ctx["recent_decisions"][-1]["skill"], "skill_16")

        prompt = soak_ctl._render_fixer_prompt(inc_rec, inc_json, Path("/tmp/fixer_wt"))
        idx_1b = prompt.index("## 1B. 目标未达成诊断（GOAL_MISS）")
        idx_1c = prompt.index("## 1C. 全局运行剖面与瓶颈线索（勿只盯末尾 10 条磁带）")
        idx_2 = prompt.index("## 2. 最近 HTTP 交互磁带切片（末尾 10 条）")
        self.assertLess(idx_1b, idx_1c)
        self.assertLess(idx_1c, idx_2)

        self.assertIn("`/tmp/ep-0001/summary.md`", prompt)
        self.assertIn("`/tmp/ep-0001/timeline.jsonl`", prompt)
        self.assertIn("整局耗时 `100.0s` · LLM 调用 `18` 次 · 回退规则 `3` 次", prompt)
        self.assertIn("`llm`=12 (60.0%)", prompt)
        self.assertIn("`rule`=5 (25.0%)", prompt)
        self.assertIn("`llm_fallback_rule`=3 (15.0%)", prompt)
        self.assertIn("`state`: 40 次, 累计 25.00s / 25.0%", prompt)
        self.assertIn("`craft`: 5 次, 累计 5.00s / 5.0%", prompt)
        self.assertNotIn("`chat`: 2 次", prompt)
        self.assertIn('`gather_wood`({"count": 4}) ×7', prompt)

        for i in range(1, 5):
            self.assertNotIn(f"`skill_{i:02d}`", prompt)
        for i in range(5, 17):
            self.assertIn(f"`skill_{i:02d}`", prompt)

    def test_observer_episode_profile_schema_compatibility(self):
        inc_rec = {
            "incident_id": "INC-399",
            "fingerprint": "API_BOTTLENECK_SLOW:find_blocks",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "session",
            "severity": "P2",
            "category": "strategy",
        }
        inc_json = {
            "episode_profile": {
                "elapsed_s": 80.0,
                "decision_sources": {"llm": 6, "rule": 4},
                "llm_calls": 6,
                "llm_fallbacks": 0,
                "http_stats": [
                    {
                        "path": "find_blocks",
                        "count": 120,
                        "ok": 120,
                        "errors": 0,
                        "total_ms": 36_000.0,
                        "avg_ms": 300.0,
                        "max_ms": 950.0,
                        "pct_of_elapsed": 45.0,
                    }
                ],
                "repeated_skill_args": [
                    {
                        "skill": "craft_gear",
                        "args": {"target": "wooden_pickaxe"},
                        "started": 5,
                        "ok": 1,
                        "replan": 4,
                        "env_blocked": 0,
                        "total_elapsed_s": 12.5,
                    }
                ],
                "recent_decisions": [
                    {
                        "elapsed_s": 14.2,
                        "skill": "craft_gear",
                        "args": {"target": "wooden_pickaxe"},
                        "source": "llm",
                        "reason": "need tool",
                        "outcome": "replan",
                        "error_code": "no_planks",
                        "error_msg": "no planks",
                        "steps": 1,
                    }
                ],
            }
        }
        prompt = soak_ctl._render_fixer_prompt(inc_rec, inc_json, Path("/tmp/fixer_wt"))
        self.assertIn("整局耗时 `80.0s` · LLM 调用 `6` 次 · 回退规则 `0` 次", prompt)
        self.assertIn("`find_blocks`: 120 次, 累计 36.00s / 45.0%", prompt)
        self.assertIn('`craft_gear`({"target": "wooden_pickaxe"}) ×5', prompt)
        self.assertIn("`craft_gear` args={\"target\": \"wooden_pickaxe\"} source=`llm` outcome=`replan` err=`no_planks`", prompt)

    def test_disk_backfill_from_inc_path_handles_corrupt_lines_and_renders(self):
        with tempfile.TemporaryDirectory() as td:
            ep_dir = Path(td) / "ep-0007"
            inc_dir = ep_dir / "incidents"
            inc_dir.mkdir(parents=True)
            inc_path = inc_dir / "INC-007.json"
            inc_path.write_text("{}", encoding="utf-8")
            (ep_dir / "summary.md").write_text("# Episode Summary\n", encoding="utf-8")

            tl_lines = [
                "",
                "   ",
                "{corrupt json line",
                "[1, 2, 3]",
            ]
            # 3 repeated calls to mine_ore + 12 additional skill_start/skill_end events
            for i in range(3):
                tl_lines.append(json.dumps({
                    "event": "skill_start",
                    "elapsed_s": float(i + 1),
                    "skill": "mine_ore",
                    "args": {"target": "iron"},
                    "source": "llm",
                }))
                tl_lines.append(json.dumps({
                    "event": "llm_call",
                    "elapsed_s": float(i + 1),
                    "ok": i != 1,  # 1 fallback out of 3 calls
                }))
            for i in range(10):
                tl_lines.append(json.dumps({
                    "event": "skill_start",
                    "elapsed_s": float(10 + i),
                    "skill": f"probe_{i:02d}",
                    "args": {"idx": i},
                    "source": "rule" if i < 4 else "llm",
                }))
                tl_lines.append(json.dumps({
                    "event": "skill_end",
                    "elapsed_s": float(10 + i + 0.5),
                    "skill": f"probe_{i:02d}",
                    "outcome": "completed",
                }))
            tl_lines.append(json.dumps({
                "event": "run_end",
                "elapsed_s": 42.5,
                "stop_reason": "max_steps",
                "target_goal_reached": False,
                "goals_reached": {"gather_wood": 5.0},
            }))
            (ep_dir / "timeline.jsonl").write_text("\n".join(tl_lines) + "\n", encoding="utf-8")

            tape_lines = [
                "{bad tape json",
                json.dumps({"path": "/inventory", "ms": 1200}),
                json.dumps({"path": "/inventory", "ms": 800}),
                json.dumps({"path": "/nearby", "ms": 500}),
            ]
            (ep_dir / "http_tape.jsonl").write_text("\n".join(tape_lines) + "\n", encoding="utf-8")

            inc_json = {"http_tape": []}  # historical incident with NO episode_profile
            ctx = soak_ctl._extract_prompt_episode_context(inc_json, str(inc_path))

            self.assertEqual(ctx["summary_path"], str(ep_dir / "summary.md"))
            self.assertEqual(ctx["timeline_path"], str(ep_dir / "timeline.jsonl"))
            self.assertEqual(ctx["elapsed_s"], 42.5)
            self.assertEqual(ctx["llm_calls"], 3)
            self.assertEqual(ctx["llm_fallbacks"], 1)
            self.assertEqual(ctx["decision_sources"], {"llm": 9, "rule": 4})
            self.assertEqual(len(ctx["recent_decisions"]), 12)
            self.assertEqual(ctx["recent_decisions"][-1]["event"], "run_end")
            self.assertEqual(len(ctx["repeated_skill_args"]), 1)
            self.assertEqual(ctx["repeated_skill_args"][0]["skill"], "mine_ore")
            self.assertEqual(ctx["repeated_skill_args"][0]["count"], 3)
            self.assertEqual(ctx["http_top_paths"][0]["path"], "inventory")
            self.assertEqual(ctx["http_top_paths"][0]["count"], 2)

            inc_rec = {
                "incident_id": "INC-007",
                "fingerprint": "POSTURE_LEAK:probe",
                "code": "POSTURE_LEAK",
                "skill": "probe",
                "severity": "P1",
                "category": "bug",
                "incident_path": str(inc_path),
            }
            prompt = soak_ctl._render_fixer_prompt(inc_rec, inc_json, Path("/tmp/fixer_wt"))
            idx_1 = prompt.index("## 1. 异常摘要")
            idx_1c = prompt.index("## 1C. 全局运行剖面与瓶颈线索（勿只盯末尾 10 条磁带）")
            idx_2 = prompt.index("## 2. 最近 HTTP 交互磁带切片（末尾 10 条）")
            self.assertLess(idx_1, idx_1c)
            self.assertLess(idx_1c, idx_2)
            self.assertIn(str(ep_dir / "summary.md"), prompt)
            self.assertIn(str(ep_dir / "timeline.jsonl"), prompt)
            self.assertIn("LLM 调用 `3` 次 · 回退规则 `1` 次", prompt)
            self.assertIn('`mine_ore`({"target": "iron"}) ×3', prompt)
            self.assertIn("`inventory`: 2 次, 累计 2.00s", prompt)
            self.assertIn("`run_end` stop_reason=`max_steps`", prompt)

    def test_section_3b_and_allow_subagents_false(self):
        inc_rec = {
            "incident_id": "INC-301",
            "fingerprint": "POSTURE_LEAK:combat",
            "code": "POSTURE_LEAK",
            "skill": "combat",
            "severity": "P1",
            "category": "bug",
        }
        prompt = soak_ctl._render_fixer_prompt(inc_rec, {}, Path("/tmp/fixer_wt"), allow_subagents=False)
        self.assertIn("## 3B. 主动排雷与防敷衍硬要求（机器与人工协同审阅）", prompt)
        self.assertIn("严禁“只改/只加测试文件、不改任何生产代码”的空转补丁", prompt)
        self.assertNotIn("spawn_agent", prompt)
        self.assertNotIn("2B. **子代理协作权限", prompt)

    def test_recurrence_goal_miss_prompt_unlocks_loop_files_and_retains_keywords(self):
        inc_rec = {
            "incident_id": "INC-401",
            "fingerprint": "GOAL_MISS:smelt_iron",
            "code": "GOAL_MISS",
            "goal_id": "smelt_iron",
            "skill": "iron",
            "severity": "P1",
            "category": "strategy",
            "editable_files": ["code/mcagent/skills/iron.py"],
            "last_failed_fix_commit": "deadbeef1234",
        }
        inc_json = {
            "detail": {
                "goal_id": "smelt_iron",
                "target_skill_started": False,
                "bottleneck_skill": "iron",
            }
        }
        prompt = soak_ctl._render_fixer_prompt(inc_rec, inc_json, Path("/tmp/fixer_wt"))
        self.assertIn("复发跨模块扩权已解锁", prompt)
        self.assertIn("deadbeef", prompt)
        self.assertIn("code/mcagent/loop/policy.py", prompt)
        self.assertIn("code/mcagent/loop/starter_skills.py", prompt)
        self.assertIn("code/mcagent/loop/goals.py", prompt)

        # Required narrow-GOAL_MISS keywords must remain present
        for kw in (
            "目标单收窄文件白名单",
            "target_skill_started=",
            "load_all()",
            "REGISTRY",
            "新建不同文件名的技能文件",
        ):
            self.assertIn(kw, prompt)

    def test_proactive_wide_incident_codes_guidance(self):
        fixer_wt = Path("/tmp/fixer_wt")
        for code in ("DISPATCH_STARVATION", "SKILL_SPAM_LOOP", "API_BOTTLENECK_SLOW"):
            inc_rec = {
                "incident_id": f"INC-{code}",
                "fingerprint": f"{code}:session",
                "code": code,
                "skill": "session",
                "severity": "P1",
                "category": "strategy",
            }
            prompt = soak_ctl._render_fixer_prompt(inc_rec, {}, fixer_wt)
            self.assertIn(WIDE_SCOPE_GUIDANCE, prompt, f"missing wide guidance for {code}")

        normal_rec = {
            "incident_id": "INC-REG",
            "fingerprint": "POSTURE_LEAK:combat",
            "code": "POSTURE_LEAK",
            "skill": "combat",
            "severity": "P1",
            "category": "bug",
        }
        normal_prompt = soak_ctl._render_fixer_prompt(normal_rec, {}, fixer_wt)
        self.assertNotIn(WIDE_SCOPE_GUIDANCE, normal_prompt)


class TestGate3CanaryRoutingAndSummary(unittest.TestCase):
    """验证 Gate 3 金丝雀命令路由、canary_summary 落盘及 goal_stopped_out 门禁拦截。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = _create_base_repo(self.root)
        self.session = self.root / "session"
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

    def tearDown(self):
        self.tmp.cleanup()

    def _prepare_dirty_iron_fix(self, inc_rec: dict) -> str:
        fp = inc_rec["fingerprint"]
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"][fp] = inc_rec
        soak_ctl.claim_next_incident(self.session)
        fixer_wt = Path(soak_ctl.read_state(self.session)["fixer_wt"])
        (fixer_wt / "code" / "mcagent" / "skills" / "iron.py").write_text(f"# fixed {fp}\n", encoding="utf-8")
        return fp

    def _run_verify(self, fp: str, canary_events: list[dict]):
        uv_cmds: list[list[str]] = []
        with patch.object(soak_ctl.subprocess, "run", side_effect=_uv_mock_dispatcher(uv_cmds, canary_events)):
            res = soak_ctl.verify_and_promote(self.session, fp, live_canary=True)
        return res, uv_cmds

    def test_dispatch_starvation_with_goal_routes_to_campaign_goal_and_records_canary_summary(self):
        for skill in ("session", "gather_wood"):
            fp = f"DISPATCH_STARVATION:{skill}"
            self._prepare_dirty_iron_fix({
                "incident_id": f"INC-DS-{skill}",
                "fingerprint": fp,
                "code": "DISPATCH_STARVATION",
                "goal_id": "smelt_iron",
                "skill": skill,
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "attempts": 0,
                "gate_history": [],
            })
            canary_events = [
                {"event": "step", "elapsed_s": 1.0},
                {"event": "step", "elapsed_s": 2.0},
                {"event": "skill_end", "skill": "smelt_iron", "outcome": "completed", "elapsed_s": 3.0},
                {
                    "event": "run_end",
                    "elapsed_s": 4.0,
                    "stop_reason": "goal_reached",
                    "target_goal": "smelt_iron",
                    "target_goal_reached": True,
                    "goals_reached": {"smelt_iron": 4.0},
                },
            ]
            res, uv_cmds = self._run_verify(fp, canary_events)
            self.assertTrue(res["ok"], f"skill={skill} res={res}")
            canary_cmd = uv_cmds[-1]
            self.assertIn("--mode", canary_cmd)
            self.assertEqual(canary_cmd[canary_cmd.index("--mode") + 1], "campaign")
            self.assertIn("--goal", canary_cmd)
            self.assertEqual(canary_cmd[canary_cmd.index("--goal") + 1], "smelt_iron")
            self.assertNotIn("focus", canary_cmd)

            st_after = soak_ctl.read_state(self.session)
            hist_last = st_after["incidents"][fp]["gate_history"][-1]
            self.assertEqual(
                hist_last["canary_summary"],
                {
                    "steps": 2,
                    "skills_completed": 1,
                    "target_goal_reached": True,
                    "stop_reason": "goal_reached",
                },
            )

    def test_session_skill_without_goal_routes_to_campaign_llm_and_records_canary_summary(self):
        fp = "API_BOTTLENECK_SLOW:session"
        self._prepare_dirty_iron_fix({
            "incident_id": "INC-API-1",
            "fingerprint": fp,
            "code": "API_BOTTLENECK_SLOW",
            "goal_id": "",
            "skill": "session",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        })
        canary_events = [
            {"event": "step", "elapsed_s": 1.0},
            {"event": "skill_end", "skill": "gather_wood", "outcome": "completed", "elapsed_s": 2.0},
            {"event": "skill_end", "skill": "craft_planks", "outcome": "completed", "elapsed_s": 3.0},
            {"event": "run_end", "elapsed_s": 5.0, "stop_reason": "max_steps", "target_goal_reached": False},
        ]
        res, uv_cmds = self._run_verify(fp, canary_events)
        self.assertTrue(res["ok"])
        canary_cmd = uv_cmds[-1]
        self.assertEqual(canary_cmd[canary_cmd.index("--mode") + 1], "campaign")
        self.assertIn("--llm", canary_cmd)
        self.assertNotIn("focus", canary_cmd)
        self.assertNotIn("--skill", canary_cmd)

        hist_last = soak_ctl.read_state(self.session)["incidents"][fp]["gate_history"][-1]
        self.assertEqual(
            hist_last["canary_summary"],
            {
                "steps": 1,
                "skills_completed": 2,
                "target_goal_reached": False,
                "stop_reason": "max_steps",
            },
        )

    def test_regular_skill_bug_routes_to_focus_mode(self):
        fp = "POSTURE_LEAK:craft_gear"
        self._prepare_dirty_iron_fix({
            "incident_id": "INC-FOCUS-1",
            "fingerprint": fp,
            "code": "POSTURE_LEAK",
            "goal_id": "",
            "skill": "craft_gear",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        })
        canary_events = [
            {"event": "step", "elapsed_s": 1.0},
            {"event": "skill_end", "skill": "craft_gear", "outcome": "completed", "elapsed_s": 2.0},
            {"event": "run_end", "elapsed_s": 2.5, "stop_reason": "completed"},
        ]
        res, uv_cmds = self._run_verify(fp, canary_events)
        self.assertTrue(res["ok"])
        canary_cmd = uv_cmds[-1]
        self.assertEqual(canary_cmd[canary_cmd.index("--mode") + 1], "focus")
        self.assertIn("--skill", canary_cmd)
        self.assertEqual(canary_cmd[canary_cmd.index("--skill") + 1], "craft_gear")

        hist_last = soak_ctl.read_state(self.session)["incidents"][fp]["gate_history"][-1]
        self.assertEqual(hist_last["canary_summary"]["stop_reason"], "completed")

    def test_goal_stopped_out_without_reaching_goal_fails_gate3(self):
        fp = "GOAL_MISS:smelt_iron"
        self._prepare_dirty_iron_fix({
            "incident_id": "INC-STOP-OUT",
            "fingerprint": fp,
            "code": "GOAL_MISS",
            "goal_id": "smelt_iron",
            "skill": "iron",
            "severity": "P1",
            "category": "strategy",
            "editable_files": ["code/mcagent/skills/iron.py"],
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        })
        canary_events = [
            {"event": "step", "elapsed_s": 1.0},
            {"event": "step", "elapsed_s": 2.0},
            {
                "event": "run_end",
                "elapsed_s": 3.0,
                "stop_reason": "goal_stopped_out",
                "target_goal": "smelt_iron",
                "target_goal_reached": False,
                "goals_reached": {},
            },
        ]
        res, _ = self._run_verify(fp, canary_events)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_gate"], "gate3_live_canary_stopped_out")
        self.assertIn("goal_stopped_out", res["detail"])
        self.assertIn("smelt_iron", res["detail"])

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"][fp]["attempts"], 1)
        self.assertEqual(st_after["incidents"][fp]["gate_history"][-1]["failed_gate"], "gate3_live_canary_stopped_out")
        self.assertEqual(_git(Path(st_after["fixer_wt"]), "status", "--porcelain"), "")

    def test_goal_stopped_out_with_target_reached_or_empty_stop_reason_passes_gate3(self):
        fp1 = "GOAL_MISS:smelt_iron_reached"
        self._prepare_dirty_iron_fix({
            "incident_id": "INC-PASS-1",
            "fingerprint": fp1,
            "code": "GOAL_MISS",
            "goal_id": "smelt_iron",
            "skill": "iron",
            "severity": "P1",
            "category": "strategy",
            "editable_files": ["code/mcagent/skills/iron.py"],
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        })
        res1, _ = self._run_verify(fp1, [
            {"event": "step", "elapsed_s": 1.0},
            {
                "event": "run_end",
                "elapsed_s": 2.0,
                "stop_reason": "goal_stopped_out",
                "target_goal": "smelt_iron",
                "target_goal_reached": True,
                "goals_reached": {"smelt_iron": 1.5},
            },
        ])
        self.assertTrue(res1["ok"], f"expected pass when target_goal_reached=True: {res1}")
        hist1 = soak_ctl.read_state(self.session)["incidents"][fp1]["gate_history"][-1]
        self.assertEqual(hist1["canary_summary"]["stop_reason"], "goal_stopped_out")
        self.assertTrue(hist1["canary_summary"]["target_goal_reached"])

        fp2 = "DISPATCH_STARVATION:empty_sr"
        self._prepare_dirty_iron_fix({
            "incident_id": "INC-PASS-2",
            "fingerprint": fp2,
            "code": "DISPATCH_STARVATION",
            "goal_id": "smelt_iron",
            "skill": "session",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
            "gate_history": [],
        })
        res2, _ = self._run_verify(fp2, [
            {"event": "step", "elapsed_s": 1.0},
            {
                "event": "run_end",
                "elapsed_s": 2.0,
                "stop_reason": "",
                "target_goal": "smelt_iron",
                "target_goal_reached": False,
                "goals_reached": {},
            },
        ])
        self.assertTrue(res2["ok"], f"expected pass when stop_reason is empty: {res2}")
        hist2 = soak_ctl.read_state(self.session)["incidents"][fp2]["gate_history"][-1]
        self.assertEqual(hist2["canary_summary"]["stop_reason"], "")
        self.assertFalse(hist2["canary_summary"]["target_goal_reached"])


if __name__ == "__main__":
    unittest.main()
