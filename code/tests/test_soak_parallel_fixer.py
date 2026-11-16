"""Unit and Git integration tests for soak autofix parallel fixer and prompt enhancements.

Tests cover:
1. `_render_fixer_prompt`: GOAL_MISS narrowing, target_skill_started=False, @register L3 Skill,
   REGISTRY mentions, unique suggested test file paths, non-narrowed incident prompts,
   and allow_subagents toggle.
2. Scope & `claim_parallel_incidents`: path normalization, conflict matrix, disjoint-file parallelism,
   wildcard exclusion rules, sequential attempt/worktree allocation, and leftover cleanup.
3. `run_fixer_batch`: true concurrency across isolated worktrees, coordinator serial cherry-pick,
   Gate 1 failure isolation, conflict requeue without burning attempts, worker timeout handling,
   and teardown cleanup.
4. Worker bounds & CLI: `FIXER_WORKERS_BOUNDS == (1, 6)`, `_validate_fixer_workers`,
   `resolve_fixer_workers`, `inject_and_set_goal` / `set-goal --fixer-workers`, `fixer-loop --once`,
   `run` CLI propagation, `status` JSON, and `REVIEW_REPORT.md`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_soak_ctl():
    repo_root = Path(__file__).resolve().parents[2]
    mod_path = repo_root / "tools" / "soak-autofix" / "soak_ctl.py"
    spec = importlib.util.spec_from_file_location("soak_ctl_parallel_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


soak_ctl = _load_soak_ctl()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


class TestFixerPromptRendering(unittest.TestCase):
    """验证 _render_fixer_prompt 各种单型、权限说明与白名单收窄渲染。"""

    def test_goal_miss_with_narrow_files_and_target_skill_not_started(self):
        inc_rec = {
            "incident_id": "INC-042",
            "fingerprint": "GOAL_MISS:craft_diamond_pickaxe",
            "code": "GOAL_MISS",
            "goal_id": "craft_diamond_pickaxe",
            "skill": "mine_diamond",
            "severity": "P1",
            "category": "goal",
            "editable_files": [
                "code/mcagent/skills/diamond.py",
                "code/mcagent/skills/crafting.py",
            ],
        }
        inc_json = {
            "detail": {
                "goal_id": "craft_diamond_pickaxe",
                "goal_title": "Diamond Pickaxe",
                "tier": "tier_3",
                "score": 50,
                "summary": "need diamond to craft pickaxe",
                "elapsed_s": 90.0,
                "budget_s": 180.0,
                "stop_reason": "timeout",
                "bottleneck_goal_id": "gather_diamond",
                "bottleneck_skill": "mine_diamond",
                "target_skill_started": False,
                "completed_prereqs": ["iron_pickaxe"],
                "missing_prereqs": ["diamond"],
                "missing_items": {"diamond": 3},
                "last_replan": None,
                "skill_stats": {},
            }
        }
        fixer_wt = Path("/tmp/fake/fixer_wt")
        prompt = soak_ctl._render_fixer_prompt(inc_rec, inc_json, fixer_wt)

        self.assertIn("target_skill_started=False", prompt)
        self.assertIn("code/mcagent/skills/diamond.py", prompt)
        self.assertIn("code/mcagent/skills/crafting.py", prompt)
        self.assertIn("允许从零创建新技能文件与 `@register` 新 L3 技能", prompt)
        self.assertIn("code/mcagent/skills/__init__.py", prompt)
        self.assertIn("skills/README.md", prompt)
        self.assertIn("load_all()", prompt)
        self.assertIn("code/mcagent/skills/REGISTRY", prompt)
        self.assertIn("严禁", prompt)
        self.assertIn("新建不同文件名的技能文件", prompt)
        expected_test_path = "code/tests/test_soak_craft_diamond_pickaxe_inc_042.py"
        self.assertIn(expected_test_path, prompt)

    def test_non_narrowed_incident_prompt(self):
        inc_rec_bug = {
            "incident_id": "INC-099",
            "fingerprint": "POSTURE_LEAK:combat",
            "code": "POSTURE_LEAK",
            "skill": "combat",
            "severity": "P1",
            "category": "bug",
        }
        inc_json_bug = {
            "error_msg": "sneaking flag leaked",
            "postcondition_error": "sneaking=True",
        }
        fixer_wt = Path("/tmp/fake/fixer_wt")
        prompt_bug = soak_ctl._render_fixer_prompt(inc_rec_bug, inc_json_bug, fixer_wt)

        self.assertIn("只允许修改/新增", prompt_bug)
        self.assertIn("code/mcagent/loop/*.py", prompt_bug)
        self.assertIn("code/mcagent/skills/*.py", prompt_bug)
        self.assertIn("允许修改已有技能或从零新建技能模块", prompt_bug)
        self.assertIn("code/mcagent/skills/<module>.py", prompt_bug)
        self.assertIn("@register", prompt_bug)
        self.assertIn("load_all()", prompt_bug)
        self.assertIn("REGISTRY", prompt_bug)
        self.assertIn("code/mcagent/client.py", prompt_bug)
        self.assertIn("code/mcagent/runtime.py", prompt_bug)
        self.assertIn("code/tests/test_soak_combat_inc_099.py", prompt_bug)

        inc_rec_goal_no_narrow = {
            "incident_id": "INC-100",
            "fingerprint": "GOAL_MISS:explore",
            "code": "GOAL_MISS",
            "goal_id": "explore",
            "skill": "explore",
            "severity": "P1",
            "category": "goal",
            "editable_files": [],
        }
        prompt_goal_no_narrow = soak_ctl._render_fixer_prompt(inc_rec_goal_no_narrow, {}, fixer_wt)
        self.assertIn("只允许修改/新增", prompt_goal_no_narrow)
        self.assertIn("code/mcagent/skills/<module>.py", prompt_goal_no_narrow)

    def test_allow_subagents_flag(self):
        inc_rec = {
            "incident_id": "INC-001",
            "fingerprint": "BUG:foo",
            "code": "BUG",
            "skill": "foo",
            "severity": "P1",
            "category": "bug",
        }
        fixer_wt = Path("/tmp/test/fixer_wt")

        prompt_default = soak_ctl._render_fixer_prompt(inc_rec, {}, fixer_wt)
        self.assertIn("2B. **子代理协作权限与模型配置（可选）**", prompt_default)
        self.assertIn("spawn_agent", prompt_default)
        self.assertIn('model="gpt-6.1-terra"', prompt_default)
        self.assertIn('reasoning_effort="xhigh"', prompt_default)
        self.assertIn('fork_turns="none"', prompt_default)
        self.assertIn("同时活跃的子代理最多 `3` 个", prompt_default)
        self.assertIn("严格互不重叠的可写文件", prompt_default)
        self.assertIn("递归深度 `<= 2`", prompt_default)
        self.assertIn(f"cd {fixer_wt}/code && uv run python -m unittest discover -s tests", prompt_default)
        self.assertIn("codex:gpt-6.1-terra:xhigh", soak_ctl.FIXER_PRESETS)

        prompt_true = soak_ctl._render_fixer_prompt(inc_rec, {}, fixer_wt, allow_subagents=True)
        self.assertIn("2B. **子代理协作权限与模型配置（可选）**", prompt_true)

        prompt_false = soak_ctl._render_fixer_prompt(inc_rec, {}, fixer_wt, allow_subagents=False)
        self.assertNotIn("2B. **子代理协作权限", prompt_false)
        self.assertNotIn("spawn_agent", prompt_false)


class TestIncidentScopeAndConflictResolution(unittest.TestCase):
    """验证可写作用域判定、冲突矩阵、并行认领互斥与 worktree 初始化。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.session = self.root / "session"
        self.repo.mkdir()

        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Soak Tester")

        (self.repo / "code" / "mcagent" / "loop").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "skills").mkdir(parents=True)
        (self.repo / "code" / "tests").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "client.py").write_text("# frozen client\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "runtime.py").write_text("# frozen runtime\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "base.py").write_text("# frozen base\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "registry.py").write_text("# frozen registry\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "testing.py").write_text("# frozen testing\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").write_text("# frozen decision\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 40\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_dummy.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init baseline")
        _git(self.repo, "checkout", "-b", "feat/base-branch")

    def tearDown(self):
        self.tmp.cleanup()

    def test_incident_edit_scope_normalization_and_wildcard(self):
        inc_norm = {
            "code": "GOAL_MISS",
            "editable_files": [
                " ./code/mcagent/skills/combat.py ",
                "code\\mcagent\\skills\\sub\\foo.py",
                "",
                "   ",
            ],
        }
        scope = soak_ctl._incident_edit_scope(inc_norm)
        self.assertEqual(
            scope,
            frozenset({"code/mcagent/skills/combat.py", "code/mcagent/skills/sub/foo.py"}),
        )

        inc_empty = {"code": "GOAL_MISS", "editable_files": []}
        self.assertEqual(soak_ctl._incident_edit_scope(inc_empty), frozenset({"*"}))

        inc_blank = {"code": "GOAL_MISS", "editable_files": ["   ", ""]}
        self.assertEqual(soak_ctl._incident_edit_scope(inc_blank), frozenset({"*"}))

        inc_bug = {
            "code": "FATAL_ENV_DEATH",
            "editable_files": ["code/mcagent/skills/combat.py"],
        }
        self.assertEqual(soak_ctl._incident_edit_scope(inc_bug), frozenset({"*"}))

    def test_scopes_conflict_matrix(self):
        self.assertTrue(soak_ctl._scopes_conflict(frozenset({"*"}), frozenset({"code/foo.py"})))
        self.assertTrue(soak_ctl._scopes_conflict(frozenset({"code/foo.py"}), frozenset({"*"})))
        self.assertTrue(soak_ctl._scopes_conflict(frozenset({"*"}), frozenset({"*"})))

        self.assertTrue(
            soak_ctl._scopes_conflict(
                frozenset({"code/a.py", "code/b.py"}),
                frozenset({"code/b.py", "code/c.py"}),
            )
        )

        self.assertFalse(
            soak_ctl._scopes_conflict(
                frozenset({"code/a.py"}),
                frozenset({"code/b.py"}),
            )
        )
        self.assertFalse(
            soak_ctl._scopes_conflict(
                frozenset({"code/a.py", "code/b.py"}),
                frozenset({"code/c.py", "code/d.py"}),
            )
        )
        self.assertFalse(soak_ctl._scopes_conflict(frozenset(), frozenset()))

    def test_disjoint_parallel_claim_skips_conflicting_incident(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
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
                },
                "GOAL_MISS:combat2": {
                    "incident_id": "INC-C2",
                    "fingerprint": "GOAL_MISS:combat2",
                    "code": "GOAL_MISS",
                    "skill": "combat2",
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
                    "skill": "diamond",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                },
                "GOAL_MISS:nether": {
                    "incident_id": "INC-N1",
                    "fingerprint": "GOAL_MISS:nether",
                    "code": "GOAL_MISS",
                    "skill": "nether",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/nether.py"],
                    "attempts": 0,
                },
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        claimed_fps = [it["fingerprint"] for it in claimed]
        self.assertEqual(
            claimed_fps,
            ["GOAL_MISS:combat", "GOAL_MISS:diamond", "GOAL_MISS:nether"],
        )

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:combat2"]["status"], "pending")

    def test_wildcard_exclusion_when_wildcard_is_first(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "FATAL:server": {
                    "incident_id": "INC-BUG",
                    "fingerprint": "FATAL:server",
                    "code": "FATAL_ENV_DEATH",
                    "skill": "server",
                    "severity": "P0",
                    "category": "bug",
                    "status": "pending",
                    "attempts": 0,
                },
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
                },
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["fingerprint"], "FATAL:server")

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:combat"]["status"], "pending")
        self.assertEqual(st_after["incidents"]["GOAL_MISS:diamond"]["status"], "pending")

    def test_wildcard_exclusion_when_wildcard_is_second(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:combat": {
                    "incident_id": "INC-C1",
                    "fingerprint": "GOAL_MISS:combat",
                    "code": "GOAL_MISS",
                    "skill": "combat",
                    "severity": "P1",
                    "category": "bug",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/combat.py"],
                    "attempts": 0,
                },
                "BUG:wildcard": {
                    "incident_id": "INC-WILD",
                    "fingerprint": "BUG:wildcard",
                    "code": "LOOP_CRASH",
                    "skill": "loop",
                    "severity": "P2",
                    "category": "bug",
                    "status": "pending",
                    "attempts": 0,
                },
                "GOAL_MISS:diamond": {
                    "incident_id": "INC-D1",
                    "fingerprint": "GOAL_MISS:diamond",
                    "code": "GOAL_MISS",
                    "skill": "diamond",
                    "severity": "P1",
                    "category": "bug",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                },
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        claimed_fps = [it["fingerprint"] for it in claimed]
        self.assertEqual(claimed_fps, ["GOAL_MISS:combat", "GOAL_MISS:diamond"])

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["BUG:wildcard"]["status"], "pending")

    def test_claimed_item_metadata_worktree_and_leftover_cleanup(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        base_sha = _git(self.repo, "rev-parse", "refs/heads/feat/soak-autofix")

        leftover_dir = self.session / "worktrees" / "worker-0001"
        leftover_dir.mkdir(parents=True)
        (leftover_dir / "stray.txt").write_text("dirty leftover", encoding="utf-8")

        with soak_ctl.locked_state(self.session) as st:
            st["next_attempt_no"] = 1
            st["incidents"] = {
                "GOAL_MISS:combat": {
                    "incident_id": "INC-001",
                    "fingerprint": "GOAL_MISS:combat",
                    "code": "GOAL_MISS",
                    "skill": "combat",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/combat.py"],
                    "attempts": 0,
                },
                "GOAL_MISS:diamond": {
                    "incident_id": "INC-002",
                    "fingerprint": "GOAL_MISS:diamond",
                    "code": "GOAL_MISS",
                    "skill": "diamond",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                },
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=2)
        self.assertEqual(len(claimed), 2)

        item1, item2 = claimed[0], claimed[1]
        self.assertTrue(item1["active_attempt_dir"].endswith("attempt-0001"))
        self.assertTrue(item2["active_attempt_dir"].endswith("attempt-0002"))

        wt1 = Path(item1["worker_wt"])
        wt2 = Path(item2["worker_wt"])
        self.assertEqual(wt1.name, "worker-0001")
        self.assertEqual(wt2.name, "worker-0002")
        self.assertTrue(wt1.exists())
        self.assertTrue(wt2.exists())
        self.assertFalse((wt1 / "stray.txt").exists())

        self.assertEqual(_git(wt1, "rev-parse", "HEAD"), base_sha)
        self.assertEqual(_git(wt2, "rev-parse", "HEAD"), base_sha)
        self.assertEqual(item1["status"], "claimed")
        self.assertEqual(item2["status"], "claimed")

        prompt1 = Path(item1["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn(str(wt1), prompt1)


class TestRunFixerBatchExecution(unittest.TestCase):
    """验证 run_fixer_batch 真实并发、串行合并、门禁隔离、冲突重入与超时清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.session = self.root / "session"
        self.repo.mkdir()

        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Soak Tester")

        (self.repo / "code" / "mcagent" / "loop").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "skills").mkdir(parents=True)
        (self.repo / "code" / "tests").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "client.py").write_text("# frozen client\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "runtime.py").write_text("# frozen runtime\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "base.py").write_text("# frozen base\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "registry.py").write_text("# frozen registry\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "testing.py").write_text("# frozen testing\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").write_text("# frozen decision\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 40\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_dummy.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_shared.py").write_text("SHARED_VAR = 0\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init baseline")
        _git(self.repo, "checkout", "-b", "feat/base-branch")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_two_disjoint_incidents(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:combat": {
                    "incident_id": "INC-001",
                    "fingerprint": "GOAL_MISS:combat",
                    "code": "GOAL_MISS",
                    "skill": "combat",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/combat.py"],
                    "attempts": 0,
                },
                "GOAL_MISS:diamond": {
                    "incident_id": "INC-002",
                    "fingerprint": "GOAL_MISS:diamond",
                    "code": "GOAL_MISS",
                    "skill": "diamond",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                },
            }

    def test_batch_parallel_execution_barrier_and_serial_promotion(self):
        self._setup_two_disjoint_incidents()
        barrier_dir = self.root / "barrier"
        barrier_dir.mkdir(parents=True, exist_ok=True)

        worker_script = self.root / "barrier_worker.py"
        worker_script.write_text(
            f"""import sys, time
from pathlib import Path

bdir = Path({repr(str(barrier_dir))})
wt = Path(sys.argv[1])
skill = sys.argv[2]

(bdir / f"{{skill}}.ready").write_text("ready", encoding="utf-8")

t0 = time.time()
while time.time() - t0 < 8.0:
    if (bdir / "combat.ready").exists() and (bdir / "diamond.ready").exists():
        break
    time.sleep(0.02)
else:
    sys.exit(1)

(wt / "code" / "mcagent" / "skills" / f"{{skill}}.py").write_text(f"# {{skill}} fixed\\n", encoding="utf-8")
(wt / "code" / "tests" / f"test_soak_{{skill}}.py").write_text(f"TEST = '{{skill}}'\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )

        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"

        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get("ok") for r in results), f"results: {results}")

        tip_files = _git(
            self.repo,
            "ls-tree",
            "-r",
            "--name-only",
            "refs/heads/feat/soak-autofix",
        ).splitlines()
        self.assertIn("code/mcagent/skills/combat.py", tip_files)
        self.assertIn("code/mcagent/skills/diamond.py", tip_files)
        self.assertIn("code/tests/test_soak_combat.py", tip_files)
        self.assertIn("code/tests/test_soak_diamond.py", tip_files)

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:combat"]["status"], "fixed")
        self.assertEqual(st_after["incidents"]["GOAL_MISS:diamond"]["status"], "fixed")
        self.assertEqual(len(st_after["commits"]), 2)

        worker_wts = list((self.session / "worktrees").glob("worker-*"))
        self.assertEqual(worker_wts, [])
        self.assertNotIn("worker-", _git(self.repo, "worktree", "list"))

    def test_batch_conflict_requeue_does_not_burn_attempts(self):
        self._setup_two_disjoint_incidents()
        worker_script = self.root / "conflict_worker.py"
        worker_script.write_text(
            """import sys
from pathlib import Path

wt = Path(sys.argv[1])
skill = sys.argv[2]

(wt / "code" / "mcagent" / "skills" / f"{skill}.py").write_text(f"# {skill}\\n", encoding="utf-8")
(wt / "code" / "tests" / "test_shared.py").write_text(f"SHARED_VAR = '{skill}'\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )

        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"

        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        self.assertEqual(len(results), 2)
        successes = [r for r in results if r.get("ok")]
        requeued = [r for r in results if r.get("requeued")]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(requeued), 1)

        conflict_res = requeued[0]
        self.assertEqual(conflict_res["failed_gate"], "merge_conflict_requeue")
        self.assertFalse(conflict_res["ok"])

        st_after = soak_ctl.read_state(self.session)
        conflict_rec = st_after["incidents"][conflict_res["fingerprint"]]
        self.assertEqual(conflict_rec["status"], "pending")
        self.assertEqual(conflict_rec.get("attempts", 0), 0)
        self.assertTrue(conflict_rec["gate_history"])
        self.assertEqual(conflict_rec["gate_history"][-1]["failed_gate"], "merge_conflict_requeue")

        fixer_wt = Path(st_after["fixer_wt"])
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")
        self.assertEqual(list((self.session / "worktrees").glob("worker-*")), [])

    def test_batch_gate1_failure_isolation(self):
        self._setup_two_disjoint_incidents()
        worker_script = self.root / "bad_worker.py"
        worker_script.write_text(
            """import sys
from pathlib import Path

wt = Path(sys.argv[1])
skill = sys.argv[2]

if skill == "combat":
    (wt / "code" / "mcagent" / "client.py").write_text("# hacked frozen client\\n", encoding="utf-8")
else:
    (wt / "code" / "mcagent" / "skills" / "diamond.py").write_text("# diamond ok\\n", encoding="utf-8")
    (wt / "code" / "tests" / "test_soak_diamond.py").write_text("x = 1\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )

        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"

        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        res_by_fp = {r["fingerprint"]: r for r in results}
        bad_res = res_by_fp["GOAL_MISS:combat"]
        good_res = res_by_fp["GOAL_MISS:diamond"]

        self.assertFalse(bad_res["ok"])
        self.assertEqual(bad_res["failed_gate"], "gate1_whitelist")
        self.assertIn("命中冻结保护文件", bad_res["detail"])

        self.assertTrue(good_res["ok"])

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:combat"]["attempts"], 1)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:diamond"]["status"], "fixed")

        client_content = _git(
            self.repo,
            "show",
            "refs/heads/feat/soak-autofix:code/mcagent/client.py",
        )
        self.assertEqual(client_content, "# frozen client")

    def test_batch_worker_timeout_handling(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:slow": {
                    "incident_id": "INC-SLOW",
                    "fingerprint": "GOAL_MISS:slow",
                    "code": "GOAL_MISS",
                    "skill": "slow",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/slow.py"],
                    "attempts": 0,
                }
            }

        template = "sleep 10"

        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=1,
                fixer_timeout_s=1,
                live_canary=False,
            )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_gate"], "gate1_whitelist")
        self.assertIn("timeout after 1s", res["detail"])

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:slow"]["attempts"], 1)
        self.assertEqual(list((self.session / "worktrees").glob("worker-*")), [])

    def test_teardown_session_cleans_stray_worker_worktrees(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        stray_wt = self.session / "worktrees" / "worker-stray"
        stray_wt.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(stray_wt), "HEAD")
        self.assertTrue(stray_wt.exists())

        report_path = soak_ctl.teardown_session(self.session)
        self.assertTrue(report_path.exists())
        self.assertFalse(stray_wt.exists())
        self.assertNotIn("worker-stray", _git(self.repo, "worktree", "list"))


class TestWorkerBoundsAndCli(unittest.TestCase):
    """验证工人数区间校验、CLI 参数传递、状态解析与报告回写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.session = self.root / "session"
        self.repo.mkdir()

        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Soak Tester")

        (self.repo / "code" / "mcagent" / "loop").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "skills").mkdir(parents=True)
        (self.repo / "code" / "tests").mkdir(parents=True)
        (self.repo / "code" / "mcagent" / "client.py").write_text("# frozen client\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "runtime.py").write_text("# frozen runtime\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "base.py").write_text("# frozen base\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "registry.py").write_text("# frozen registry\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "skills" / "testing.py").write_text("# frozen testing\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "code" / "mcagent" / "planner" / "decision.py").write_text("# frozen decision\n", encoding="utf-8")
        (self.repo / "code" / "mcagent" / "loop" / "starter_skills.py").write_text("LIMIT = 40\n", encoding="utf-8")
        (self.repo / "code" / "tests" / "test_dummy.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init baseline")
        _git(self.repo, "checkout", "-b", "feat/base-branch")

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_fixer_workers_bounds(self):
        self.assertEqual(soak_ctl.FIXER_WORKERS_BOUNDS, (1, 6))

        for valid in (1, 2, 3, 4, 5, 6, "1", "6", " 3 "):
            self.assertIn(soak_ctl._validate_fixer_workers(valid), range(1, 7))

        for bad in (0, 7, -1, True, False, "abc", "2.5", None, [], 3.14):
            with self.assertRaises(ValueError):
                soak_ctl._validate_fixer_workers(bad)

    def test_resolve_fixer_workers_fallback(self):
        self.session.mkdir(parents=True, exist_ok=True)

        (self.session / "state.json").write_text(json.dumps({"fixer_workers": 4}), encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 1), 4)

        (self.session / "state.json").write_text(json.dumps({}), encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 2), 2)

        (self.session / "state.json").write_text(json.dumps({"fixer_workers": "corrupted"}), encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 1), 1)

        (self.session / "state.json").write_text(json.dumps({"fixer_workers": 99}), encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 1), 1)

        (self.session / "state.json").write_text("not json at all", encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 3), 3)

        (self.session / "state.json").unlink(missing_ok=True)
        self.assertEqual(soak_ctl.resolve_fixer_workers(self.session, 5), 5)

    def test_inject_and_set_goal_fixer_workers(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        res_low = soak_ctl.inject_and_set_goal(self.session, fixer_workers=0)
        self.assertFalse(res_low["ok"])
        self.assertEqual(res_low["failed_step"], "invalid_fixer_workers")

        res_high = soak_ctl.inject_and_set_goal(self.session, fixer_workers=7)
        self.assertFalse(res_high["ok"])
        self.assertEqual(res_high["failed_step"], "invalid_fixer_workers")

        res_ok = soak_ctl.inject_and_set_goal(self.session, fixer_workers=4)
        self.assertTrue(res_ok["ok"])
        self.assertEqual(res_ok["fixer_workers"], 4)
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_workers"], 4)

        report = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("fixer_workers=4", report)

        rc_set = soak_ctl.main([
            "set-goal",
            "--session-dir", str(self.session),
            "--fixer-workers", "5",
        ])
        self.assertEqual(rc_set, 0)
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_workers"], 5)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_status = soak_ctl.main(["status", "--session-dir", str(self.session)])
        self.assertEqual(rc_status, 0)
        status_data = json.loads(buf.getvalue())
        self.assertEqual(status_data["fixer_workers"], 5)

    def test_fixer_loop_once_workers_dispatch_and_exit_codes(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        rc_bad_low = soak_ctl.main([
            "fixer-loop",
            "--session-dir", str(self.session),
            "--fixer-preset", "codex",
            "--fixer-workers", "0",
        ])
        self.assertEqual(rc_bad_low, 2)

        rc_bad_high = soak_ctl.main([
            "fixer-loop",
            "--session-dir", str(self.session),
            "--fixer-preset", "codex",
            "--fixer-workers", "7",
        ])
        self.assertEqual(rc_bad_high, 2)

        with soak_ctl.locked_state(self.session) as st:
            st["fixer_workers"] = 1

        with patch.object(soak_ctl, "run_fixer_once", return_value={"ok": True}) as mock_once, \
             patch.object(soak_ctl, "run_fixer_batch") as mock_batch:
            rc = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--once",
            ])
            self.assertEqual(rc, 0)
            mock_once.assert_called_once()
            mock_batch.assert_not_called()

        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": True}]) as mock_batch, \
             patch.object(soak_ctl, "run_fixer_once") as mock_once:
            rc = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
            self.assertEqual(rc, 0)
            mock_batch.assert_called_once()
            self.assertEqual(mock_batch.call_args.kwargs["max_workers"], 3)
            mock_once.assert_not_called()

        with patch.object(soak_ctl, "run_fixer_batch", return_value=[]):
            rc_empty = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
            self.assertEqual(rc_empty, 0)

        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": False}]):
            rc_fail = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
            self.assertEqual(rc_fail, 1)

        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": False}, {"ok": True}]):
            rc_partial = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
            self.assertEqual(rc_partial, 0)

    def test_run_cli_fixer_workers_propagation(self):
        parsed_default = soak_ctl.build_parser().parse_args(["run", "--session-dir", str(self.session)])
        self.assertEqual(parsed_default.fixer_workers, 1)

        parsed_3 = soak_ctl.build_parser().parse_args([
            "run", "--session-dir", str(self.session), "--fixer-workers", "3"
        ])
        self.assertEqual(parsed_3.fixer_workers, 3)

        class DummyProc:
            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        spawned_cmds: list[list[str]] = []

        def capture_popen(cmd, *args, **kwargs):
            spawned_cmds.append(list(cmd))
            return DummyProc()

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "check_server_target", return_value=[]), \
             patch.object(soak_ctl, "init_session", return_value={}), \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=capture_popen):
            rc_default = soak_ctl.main([
                "run",
                "--session-dir", str(self.session),
                "--branch", "feat/soak-autofix",
                "--server", "local",
                "--episodes", "1",
            ])
        self.assertEqual(rc_default, 0)
        fixer_invocations = [c for c in spawned_cmds if "fixer-loop" in c]
        self.assertEqual(len(fixer_invocations), 1)
        self.assertNotIn("--fixer-workers", fixer_invocations[0])
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_workers"], 1)

        spawned_cmds.clear()
        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "check_server_target", return_value=[]), \
             patch.object(soak_ctl, "init_session", return_value={}), \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=capture_popen):
            rc_3 = soak_ctl.main([
                "run",
                "--session-dir", str(self.session),
                "--branch", "feat/soak-autofix",
                "--server", "local",
                "--episodes", "1",
                "--fixer-workers", "3",
            ])
        self.assertEqual(rc_3, 0)
        fixer_invocations_3 = [c for c in spawned_cmds if "fixer-loop" in c]
        self.assertEqual(len(fixer_invocations_3), 1)
        self.assertIn("--fixer-workers", fixer_invocations_3[0])
        idx = fixer_invocations_3[0].index("--fixer-workers")
        self.assertEqual(fixer_invocations_3[0][idx + 1], "3")
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_workers"], 3)

        rc_bad = soak_ctl.main([
            "run",
            "--session-dir", str(self.session),
            "--server", "local",
            "--fixer-workers", "9",
        ])
        self.assertEqual(rc_bad, 2)


if __name__ == "__main__":
    unittest.main()
