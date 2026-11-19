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

    def test_proactive_domain_partitioning_fills_three_workers_without_breaking_wildcards(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "DISPATCH_STARVATION:enter_the_nether": {
                    "incident_id": "INC-N-STARVE",
                    "fingerprint": "DISPATCH_STARVATION:enter_the_nether",
                    "code": "DISPATCH_STARVATION",
                    "goal_id": "enter_the_nether",
                    "skill": "build_and_enter_portal",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": [],
                    "attempts": 0,
                },
                "GOAL_MISS:enter_the_nether": {
                    "incident_id": "INC-N-MISS",
                    "fingerprint": "GOAL_MISS:enter_the_nether",
                    "code": "GOAL_MISS",
                    "goal_id": "enter_the_nether",
                    "skill": "build_and_enter_portal",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/nether.py"],
                    "attempts": 0,
                },
                "GOAL_MISS:prep_diamond_pickaxe": {
                    "incident_id": "INC-D-MISS",
                    "fingerprint": "GOAL_MISS:prep_diamond_pickaxe",
                    "code": "GOAL_MISS",
                    "goal_id": "prep_diamond_pickaxe",
                    "skill": "craft_diamond_gear",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/diamond.py"],
                    "attempts": 0,
                },
                "SKILL_SPAM_LOOP:scout_island:4fe949": {
                    "incident_id": "INC-S-SPAM",
                    "fingerprint": "SKILL_SPAM_LOOP:scout_island:4fe949",
                    "code": "SKILL_SPAM_LOOP",
                    "skill": "scout_island",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": [],
                    "attempts": 0,
                },
                "SKILL_SPAM_LOOP:gather_wood:dfd326": {
                    "incident_id": "INC-W-SPAM",
                    "fingerprint": "SKILL_SPAM_LOOP:gather_wood:dfd326",
                    "code": "SKILL_SPAM_LOOP",
                    "skill": "gather_wood",
                    "severity": "P2",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": [],
                    "attempts": 0,
                },
                "API_BOTTLENECK_SLOW:move_to": {
                    "incident_id": "INC-API-1",
                    "fingerprint": "API_BOTTLENECK_SLOW:move_to",
                    "code": "API_BOTTLENECK_SLOW",
                    "skill": "session",
                    "severity": "P2",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": [],
                    "attempts": 0,
                },
                "API_BOTTLENECK_SLOW:find_blocks": {
                    "incident_id": "INC-API-2",
                    "fingerprint": "API_BOTTLENECK_SLOW:find_blocks",
                    "code": "API_BOTTLENECK_SLOW",
                    "skill": "session",
                    "severity": "P2",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": [],
                    "attempts": 0,
                },
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual(
            [c["fingerprint"] for c in claimed],
            [
                "DISPATCH_STARVATION:enter_the_nether",
                "GOAL_MISS:prep_diamond_pickaxe",
                "SKILL_SPAM_LOOP:scout_island:4fe949",
            ],
        )

        # Next batch mixes API_BOTTLENECK_SLOW + remaining starter/nether domains, while keeping two API_BOTTLENECK_SLOW mutually exclusive.
        claimed2 = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual(
            [c["fingerprint"] for c in claimed2],
            [
                "GOAL_MISS:enter_the_nether",
                "SKILL_SPAM_LOOP:gather_wood:dfd326",
                "API_BOTTLENECK_SLOW:move_to",
            ],
        )

        # _incident_edit_scope stays wildcard for non-GOAL_MISS contracts.
        st_now = soak_ctl.read_state(self.session)
        self.assertEqual(
            soak_ctl._incident_edit_scope(st_now["incidents"]["API_BOTTLENECK_SLOW:find_blocks"]),
            frozenset({"*"}),
        )

        # A merge_conflict_requeue history escalates the scope to include policy.py / goals.py / starter_skills.py.
        scope_clean = soak_ctl._claim_parallel_scope(st_now["incidents"]["GOAL_MISS:enter_the_nether"])
        conflict_rec = {
            "code": "SKILL_SPAM_LOOP",
            "skill": "hunt_mob",
            "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
        }
        conflict_scope = soak_ctl._claim_parallel_scope(conflict_rec)
        self.assertIn("code/mcagent/loop/policy.py", conflict_scope)
        self.assertFalse(soak_ctl._scopes_conflict(scope_clean, conflict_scope))

    def test_dispatch_starvation_scope_and_cross_domain_conflict_and_escalation(self):
        nether_starve = {
            "incident_id": "INC-DS-NETHER",
            "fingerprint": "DISPATCH_STARVATION:enter_the_nether",
            "code": "DISPATCH_STARVATION",
            "goal_id": "enter_the_nether",
            "skill": "build_and_enter_portal",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }
        iron_starve = {
            "incident_id": "INC-DS-IRON",
            "fingerprint": "DISPATCH_STARVATION:smelt_iron",
            "code": "DISPATCH_STARVATION",
            "goal_id": "smelt_iron",
            "skill": "smelt_iron",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }

        nether_scope = soak_ctl._claim_parallel_scope(nether_starve)
        iron_scope = soak_ctl._claim_parallel_scope(iron_starve)
        self.assertEqual(
            nether_scope,
            frozenset({
                "code/mcagent/skills/nether.py",
                "code/mcagent/loop/policy.py",
                "code/mcagent/loop/goals.py",
            }),
        )
        self.assertEqual(
            iron_scope,
            frozenset({
                "code/mcagent/skills/iron.py",
                "code/mcagent/loop/policy.py",
                "code/mcagent/loop/goals.py",
            }),
        )
        self.assertTrue(soak_ctl._scopes_conflict(nether_scope, iron_scope))

        escalated_starve = {
            **nether_starve,
            "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
        }
        self.assertEqual(soak_ctl._claim_parallel_scope(escalated_starve), frozenset({"*"}))

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                nether_starve["fingerprint"]: dict(nether_starve),
                iron_starve["fingerprint"]: dict(iron_starve),
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual([c["fingerprint"] for c in claimed], ["DISPATCH_STARVATION:enter_the_nether"])
        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["DISPATCH_STARVATION:smelt_iron"]["status"], "pending")

    def test_dispatch_starvation_conflicts_with_recurring_goal_miss_and_keeps_three_way_parallelism(self):
        starve_rec = {
            "incident_id": "INC-DS-1",
            "fingerprint": "DISPATCH_STARVATION:enter_the_nether",
            "code": "DISPATCH_STARVATION",
            "goal_id": "enter_the_nether",
            "skill": "build_and_enter_portal",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }
        recurring_goal = {
            "incident_id": "INC-GM-REC",
            "fingerprint": "GOAL_MISS:prep_diamond_pickaxe",
            "code": "GOAL_MISS",
            "goal_id": "prep_diamond_pickaxe",
            "skill": "craft_diamond_gear",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": ["code/mcagent/skills/diamond.py"],
            "last_failed_fix_commit": "deadbeef",
            "attempts": 1,
        }
        spam_rec = {
            "incident_id": "INC-SPAM-1",
            "fingerprint": "SKILL_SPAM_LOOP:gather_wood:abc123",
            "code": "SKILL_SPAM_LOOP",
            "skill": "gather_wood",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }
        api_rec = {
            "incident_id": "INC-API-1",
            "fingerprint": "API_BOTTLENECK_SLOW:move_to",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "session",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }

        starve_scope = soak_ctl._claim_parallel_scope(starve_rec)
        recurring_scope = soak_ctl._claim_parallel_scope(recurring_goal)
        self.assertTrue(set(soak_ctl.GOAL_RECURRENCE_EXTRA_FILES).issubset(recurring_scope))
        self.assertTrue(soak_ctl._scopes_conflict(starve_scope, recurring_scope))

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                starve_rec["fingerprint"]: dict(starve_rec),
                recurring_goal["fingerprint"]: dict(recurring_goal),
                spam_rec["fingerprint"]: dict(spam_rec),
                api_rec["fingerprint"]: dict(api_rec),
            }

        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual(
            [c["fingerprint"] for c in claimed],
            [
                "DISPATCH_STARVATION:enter_the_nether",
                "SKILL_SPAM_LOOP:gather_wood:abc123",
                "API_BOTTLENECK_SLOW:move_to",
            ],
        )
        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:prep_diamond_pickaxe"]["status"], "pending")

    def test_relaxed_three_way_parallelism_same_starter_domain_and_llm_session(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        incidents = {
            "SKILL_SPAM_LOOP:gather_wood:aaa111": {
                "incident_id": "INC-WOOD",
                "fingerprint": "SKILL_SPAM_LOOP:gather_wood:aaa111",
                "code": "SKILL_SPAM_LOOP",
                "skill": "gather_wood",
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "editable_files": [],
                "attempts": 0,
            },
            "REPLAN_OSCILLATION:surface_for_air:bbb222": {
                "incident_id": "INC-AIR",
                "fingerprint": "REPLAN_OSCILLATION:surface_for_air:bbb222",
                "code": "REPLAN_OSCILLATION",
                "skill": "surface_for_air",
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "editable_files": [],
                "attempts": 0,
            },
            "LLM_INVALID_OR_TIMEOUT:session:ccc333": {
                "incident_id": "INC-LLM",
                "fingerprint": "LLM_INVALID_OR_TIMEOUT:session:ccc333",
                "code": "LLM_INVALID_OR_TIMEOUT",
                "skill": "session",
                "severity": "P3",
                "category": "bug",
                "status": "pending",
                "editable_files": [],
                "attempts": 0,
            },
        }
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {k: dict(v) for k, v in incidents.items()}

        # Under strict: gather_wood and surface_for_air collide on starter_skills.py, LLM is wildcard.
        strict_claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="strict")
        self.assertEqual([c["fingerprint"] for c in strict_claimed], ["SKILL_SPAM_LOOP:gather_wood:aaa111"])

        # Reset to pending and check explicit relaxed scope & 3-way claim.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {k: dict(v) for k, v in incidents.items()}

        self.assertEqual(
            soak_ctl._claim_parallel_scope(incidents["SKILL_SPAM_LOOP:gather_wood:aaa111"], parallel_policy="relaxed"),
            frozenset({"skill:gather_wood"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(incidents["REPLAN_OSCILLATION:surface_for_air:bbb222"], parallel_policy="relaxed"),
            frozenset({"skill:surface_for_air"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(incidents["LLM_INVALID_OR_TIMEOUT:session:ccc333"], parallel_policy="relaxed"),
            frozenset({"domain:llm_session"}),
        )

        relaxed_claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            set(c["fingerprint"] for c in relaxed_claimed),
            set(incidents.keys()),
        )

        # When parallel_policy=None, claim_parallel_incidents honors state.json["fixer_parallel_policy"].
        with soak_ctl.locked_state(self.session) as st:
            st["fixer_parallel_policy"] = "relaxed"
            st["incidents"] = {k: dict(v) for k, v in incidents.items()}
        state_claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3)
        self.assertEqual(len(state_claimed), 3)

    def test_relaxed_recurring_goal_miss_and_replan_keep_gate1_expansion(self):
        goal_rec = {
            "incident_id": "INC-GM-KILL",
            "fingerprint": "GOAL_MISS:kill_a_mob",
            "code": "GOAL_MISS",
            "goal_id": "kill_a_mob",
            "skill": "hunt_mob",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": ["code/mcagent/skills/combat.py"],
            "last_failed_fix_commit": "deadbeef",
            "attempts": 1,
        }
        replan_rec = {
            "incident_id": "INC-RP-SEED",
            "fingerprint": "REPLAN_OSCILLATION:plant_seed",
            "code": "REPLAN_OSCILLATION",
            "goal_id": "plant_seed",
            "skill": "plant_seed",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "last_failed_fix_commit": "cafebabe",
            "attempts": 1,
        }
        spam_rec = {
            "incident_id": "INC-SP-STONE",
            "fingerprint": "SKILL_SPAM_LOOP:mine_stone",
            "code": "SKILL_SPAM_LOOP",
            "skill": "mine_stone",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }

        # Gate 1 expansion remains intact for recurring GOAL_MISS.
        narrow = soak_ctl._effective_goal_narrow_files(goal_rec)
        self.assertIsNotNone(narrow)
        self.assertEqual(narrow[0], "code/mcagent/skills/combat.py")
        for extra in soak_ctl.GOAL_RECURRENCE_EXTRA_FILES:
            self.assertIn(extra, narrow)

        # Under strict: GOAL_RECURRENCE_EXTRA_FILES causes starter_skills.py / policy.py collisions.
        strict_goal_scope = soak_ctl._claim_parallel_scope(goal_rec, parallel_policy="strict")
        strict_replan_scope = soak_ctl._claim_parallel_scope(replan_rec, parallel_policy="strict")
        self.assertTrue(set(soak_ctl.GOAL_RECURRENCE_EXTRA_FILES).issubset(strict_goal_scope))
        self.assertTrue(soak_ctl._scopes_conflict(strict_goal_scope, strict_replan_scope))

        # Under relaxed: no GOAL_RECURRENCE_EXTRA_FILES in claim scope; all 3 claim simultaneously.
        relaxed_goal_scope = soak_ctl._claim_parallel_scope(goal_rec, parallel_policy="relaxed")
        self.assertEqual(relaxed_goal_scope, frozenset({"goal:kill_a_mob", "skill:hunt_mob"}))
        self.assertEqual(
            soak_ctl._claim_parallel_scope(replan_rec, parallel_policy="relaxed"),
            frozenset({"skill:plant_seed"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(spam_rec, parallel_policy="relaxed"),
            frozenset({"skill:mine_stone"}),
        )

        # GOAL_MISS without goal_id falls back to fingerprint (+ skill if set).
        goal_no_id = {
            "fingerprint": "GOAL_MISS:custom_fp",
            "code": "GOAL_MISS",
            "skill": "gather_wood",
            "editable_files": ["code/mcagent/loop/starter_skills.py"],
        }
        self.assertEqual(
            soak_ctl._claim_parallel_scope(goal_no_id, parallel_policy="relaxed"),
            frozenset({"GOAL_MISS:custom_fp", "skill:gather_wood"}),
        )

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                goal_rec["fingerprint"]: dict(goal_rec),
                replan_rec["fingerprint"]: dict(replan_rec),
                spam_rec["fingerprint"]: dict(spam_rec),
            }
        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            set(c["fingerprint"] for c in claimed),
            {"GOAL_MISS:kill_a_mob", "REPLAN_OSCILLATION:plant_seed", "SKILL_SPAM_LOOP:mine_stone"},
        )

    def test_relaxed_api_bottleneck_slow_and_combat_or_mob_death_scopes(self):
        api_move = {
            "incident_id": "INC-API-M",
            "fingerprint": "API_BOTTLENECK_SLOW:move_to",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "move_to",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        api_find = {
            "incident_id": "INC-API-F",
            "fingerprint": "API_BOTTLENECK_SLOW:find_blocks",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "find_blocks",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        api_sess1 = {
            "incident_id": "INC-API-S1",
            "fingerprint": "API_BOTTLENECK_SLOW:session1",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "session",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        api_sess2 = {
            "incident_id": "INC-API-S2",
            "fingerprint": "API_BOTTLENECK_SLOW:session2",
            "code": "API_BOTTLENECK_SLOW",
            "skill": "",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        combat1 = {
            "incident_id": "INC-CMB-1",
            "fingerprint": "COMBAT_OR_MOB_DEATH:1",
            "code": "COMBAT_OR_MOB_DEATH",
            "skill": "combat",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }
        combat2 = {
            "incident_id": "INC-CMB-2",
            "fingerprint": "COMBAT_OR_MOB_DEATH:2",
            "code": "COMBAT_OR_MOB_DEATH",
            "skill": "hunt_mob",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }

        self.assertEqual(
            soak_ctl._claim_parallel_scope(api_move, parallel_policy="relaxed"),
            frozenset({"domain:api_bottleneck_slow:move_to"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(api_find, parallel_policy="relaxed"),
            frozenset({"domain:api_bottleneck_slow:find_blocks"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(api_sess1, parallel_policy="relaxed"),
            frozenset({"domain:api_bottleneck_slow"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(api_sess2, parallel_policy="relaxed"),
            frozenset({"domain:api_bottleneck_slow"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(combat1, parallel_policy="relaxed"),
            frozenset({"domain:combat_session"}),
        )
        self.assertEqual(
            soak_ctl._claim_parallel_scope(combat1, parallel_policy="strict"),
            frozenset({"*"}),
        )

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                combat1["fingerprint"]: dict(combat1),
                combat2["fingerprint"]: dict(combat2),
                api_move["fingerprint"]: dict(api_move),
                api_find["fingerprint"]: dict(api_find),
            }
        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed],
            ["COMBAT_OR_MOB_DEATH:1", "API_BOTTLENECK_SLOW:move_to", "API_BOTTLENECK_SLOW:find_blocks"],
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["COMBAT_OR_MOB_DEATH:2"]["status"],
            "pending",
        )

        # Two session-level API_BOTTLENECK_SLOWs mutually exclude each other.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                api_sess1["fingerprint"]: dict(api_sess1),
                api_sess2["fingerprint"]: dict(api_sess2),
            }
        claimed_sess = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(len(claimed_sess), 1)

    def test_relaxed_dispatch_starvation_mutual_exclusion_and_three_way_mix(self):
        starve1 = {
            "incident_id": "INC-DS-1",
            "fingerprint": "DISPATCH_STARVATION:enter_the_nether",
            "code": "DISPATCH_STARVATION",
            "goal_id": "enter_the_nether",
            "skill": "build_and_enter_portal",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        starve2 = {
            "incident_id": "INC-DS-2",
            "fingerprint": "DISPATCH_STARVATION:smelt_iron",
            "code": "DISPATCH_STARVATION",
            "goal_id": "smelt_iron",
            "skill": "smelt_iron",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        replan_stone = {
            "incident_id": "INC-RP-STONE",
            "fingerprint": "REPLAN_OSCILLATION:mine_stone",
            "code": "REPLAN_OSCILLATION",
            "skill": "mine_stone",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        llm_rec = {
            "incident_id": "INC-LLM",
            "fingerprint": "LLM_INVALID_OR_TIMEOUT:session",
            "code": "LLM_INVALID_OR_TIMEOUT",
            "skill": "session",
            "severity": "P3",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }

        s1 = soak_ctl._claim_parallel_scope(starve1, parallel_policy="relaxed")
        s2 = soak_ctl._claim_parallel_scope(starve2, parallel_policy="relaxed")
        self.assertIn("domain:dispatch", s1)
        self.assertIn("domain:dispatch", s2)
        self.assertTrue(soak_ctl._scopes_conflict(s1, s2))

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                starve1["fingerprint"]: dict(starve1),
                starve2["fingerprint"]: dict(starve2),
                replan_stone["fingerprint"]: dict(replan_stone),
                llm_rec["fingerprint"]: dict(llm_rec),
            }
        claimed = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed],
            [
                "DISPATCH_STARVATION:enter_the_nether",
                "REPLAN_OSCILLATION:mine_stone",
                "LLM_INVALID_OR_TIMEOUT:session",
            ],
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["DISPATCH_STARVATION:smelt_iron"]["status"],
            "pending",
        )

    def test_relaxed_progressive_conflict_fallback(self):
        wood_base = {
            "incident_id": "INC-WOOD",
            "fingerprint": "SKILL_SPAM_LOOP:gather_wood",
            "code": "SKILL_SPAM_LOOP",
            "skill": "gather_wood",
            "gate_history": [{"failed_gate": "gate1_whitelist"}],  # non-conflict gates must not count
        }
        self.assertEqual(
            soak_ctl._claim_parallel_scope(wood_base, parallel_policy="relaxed"),
            frozenset({"skill:gather_wood"}),
        )

        # 1 merge_conflict_requeue -> expands strict file scope with overlapping relaxed symbolic tokens.
        wood_one_conflict = {
            **wood_base,
            "gate_history": [
                {"failed_gate": "gate1_whitelist"},
                {"failed_gate": "merge_conflict_requeue"},
            ],
        }
        strict_wood = soak_ctl._claim_parallel_scope(wood_one_conflict, parallel_policy="strict")
        relaxed_one = soak_ctl._claim_parallel_scope(wood_one_conflict, parallel_policy="relaxed")
        self.assertTrue(strict_wood.issubset(relaxed_one))
        self.assertIn("code/mcagent/loop/starter_skills.py", strict_wood)
        self.assertTrue(set(soak_ctl.GOAL_RECURRENCE_EXTRA_FILES).issubset(strict_wood))
        for tok in (
            "skill:gather_wood",
            "skill:scout_island",
            "skill:surface_for_air",
            "domain:dispatch",
            "domain:llm_session",
        ):
            self.assertIn(tok, relaxed_one)
        for unrelated in (
            "skill:mine_diamond",
            "goal:mine_diamond",
            "code/mcagent/skills/diamond.py",
            "domain:combat_session",
            "*",
        ):
            self.assertNotIn(unrelated, relaxed_one)

        # DISPATCH_STARVATION and API_BOTTLENECK_SLOW downgrade to '*' on 1 conflict (matching strict).
        for wide_rec in (
            {
                "code": "DISPATCH_STARVATION",
                "goal_id": "enter_the_nether",
                "skill": "build_and_enter_portal",
                "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
            },
            {
                "code": "API_BOTTLENECK_SLOW",
                "skill": "move_to",
                "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
            },
        ):
            self.assertEqual(
                soak_ctl._claim_parallel_scope(wide_rec, parallel_policy="relaxed"),
                soak_ctl._claim_parallel_scope(wide_rec, parallel_policy="strict"),
            )
            self.assertEqual(
                soak_ctl._claim_parallel_scope(wide_rec, parallel_policy="relaxed"),
                frozenset({"*"}),
            )

        # 2 merge_conflict_requeues -> escalates to wildcard '*' even for GOAL_MISS / starter skill.
        wood_two_conflicts = {
            **wood_base,
            "gate_history": [
                {"failed_gate": "merge_conflict_requeue"},
                {"failed_gate": "gate2_unittest"},
                {"failed_gate": "merge_conflict_requeue"},
            ],
        }
        self.assertEqual(
            soak_ctl._claim_parallel_scope(wood_two_conflicts, parallel_policy="relaxed"),
            frozenset({"*"}),
        )
        goal_two_conflicts = {
            "code": "GOAL_MISS",
            "goal_id": "mine_diamond",
            "skill": "mine_diamond",
            "editable_files": ["code/mcagent/skills/diamond.py"],
            "gate_history": [
                {"failed_gate": "merge_conflict_requeue"},
                {"failed_gate": "merge_conflict_requeue"},
            ],
        }
        self.assertEqual(
            soak_ctl._claim_parallel_scope(goal_two_conflicts, parallel_policy="relaxed"),
            frozenset({"*"}),
        )

        # 1 merge_conflict_requeue on combat/survival GOAL_MISS expands to combat_session + hunt_mob.
        combat_one_conflict = {
            "incident_id": "INC-CMB-1C",
            "fingerprint": "GOAL_MISS:kill_a_mob",
            "code": "GOAL_MISS",
            "goal_id": "kill_a_mob",
            "skill": "combat",
            "editable_files": ["code/mcagent/skills/combat.py"],
            "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
        }
        combat_relaxed = soak_ctl._claim_parallel_scope(combat_one_conflict, parallel_policy="relaxed")
        for tok in (
            "code/mcagent/skills/combat.py",
            "domain:combat_session",
            "skill:hunt_mob",
            "goal:kill_a_mob",
        ):
            self.assertIn(tok, combat_relaxed)
        self.assertNotIn("skill:mine_diamond", combat_relaxed)
        self.assertNotIn("domain:dispatch", combat_relaxed)

    def test_relaxed_wild_fatal_and_empty_goal_stay_exclusive(self):
        fatal_rec = {
            "incident_id": "INC-FATAL",
            "fingerprint": "FATAL_ENV_DEATH:lava",
            "code": "FATAL_ENV_DEATH",
            "skill": "lava",
            "severity": "P0",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }
        wild_goal = {
            "incident_id": "INC-WILD-GOAL",
            "fingerprint": "GOAL_MISS:explore",
            "code": "GOAL_MISS",
            "goal_id": "explore",
            "skill": "explore",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "editable_files": [],
            "attempts": 0,
        }
        unknown_rec = {
            "incident_id": "INC-UNK",
            "fingerprint": "LOOP_CRASH:unknown",
            "code": "LOOP_CRASH",
            "skill": "loop",
            "severity": "P2",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }
        for rec in (fatal_rec, wild_goal, unknown_rec):
            self.assertEqual(
                soak_ctl._claim_parallel_scope(rec, parallel_policy="relaxed"),
                frozenset({"*"}),
            )

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        wood_rec = {
            "incident_id": "INC-WOOD",
            "fingerprint": "SKILL_SPAM_LOOP:gather_wood",
            "code": "SKILL_SPAM_LOOP",
            "skill": "gather_wood",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        llm_rec = {
            "incident_id": "INC-LLM",
            "fingerprint": "LLM_INVALID_OR_TIMEOUT:session",
            "code": "LLM_INVALID_OR_TIMEOUT",
            "skill": "session",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }

        # Wild FATAL_ENV_DEATH first (P0) blocks concurrent relaxed claims.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                fatal_rec["fingerprint"]: dict(fatal_rec),
                wood_rec["fingerprint"]: dict(wood_rec),
                llm_rec["fingerprint"]: dict(llm_rec),
            }
        claimed1 = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual([c["fingerprint"] for c in claimed1], ["FATAL_ENV_DEATH:lava"])
        st1 = soak_ctl.read_state(self.session)
        self.assertEqual(st1["incidents"]["SKILL_SPAM_LOOP:gather_wood"]["status"], "pending")
        self.assertEqual(st1["incidents"]["LLM_INVALID_OR_TIMEOUT:session"]["status"], "pending")

        # Lower-priority wild incident is skipped while relaxed disjoint incidents proceed.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                wood_rec["fingerprint"]: dict(wood_rec),
                llm_rec["fingerprint"]: dict(llm_rec),
                unknown_rec["fingerprint"]: dict(unknown_rec),
            }
        claimed2 = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            set(c["fingerprint"] for c in claimed2),
            {"SKILL_SPAM_LOOP:gather_wood", "LLM_INVALID_OR_TIMEOUT:session"},
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["LOOP_CRASH:unknown"]["status"],
            "pending",
        )

    def test_relaxed_one_conflict_blocks_overlapping_tokens_both_orders(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        def make_scout_ccnt1(severity: str) -> dict:
            return {
                "incident_id": "INC-SCOUT-1",
                "fingerprint": "SKILL_SPAM_LOOP:scout_island",
                "code": "SKILL_SPAM_LOOP",
                "skill": "scout_island",
                "severity": severity,
                "category": "strategy",
                "status": "pending",
                "attempts": 0,
                "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
            }

        def make_scout_ccnt0(severity: str) -> dict:
            return {
                "incident_id": "INC-SCOUT-0",
                "fingerprint": "REPLAN_OSCILLATION:scout_island",
                "code": "REPLAN_OSCILLATION",
                "skill": "scout_island",
                "severity": severity,
                "category": "strategy",
                "status": "pending",
                "attempts": 0,
            }

        def make_air_ccnt0(severity: str) -> dict:
            return {
                "incident_id": "INC-AIR-0",
                "fingerprint": "REPLAN_OSCILLATION:surface_for_air",
                "code": "REPLAN_OSCILLATION",
                "skill": "surface_for_air",
                "severity": severity,
                "category": "strategy",
                "status": "pending",
                "attempts": 0,
            }

        diamond_goal = {
            "incident_id": "INC-DIA",
            "fingerprint": "GOAL_MISS:mine_diamond",
            "code": "GOAL_MISS",
            "goal_id": "mine_diamond",
            "skill": "mine_diamond",
            "severity": "P2",
            "category": "strategy",
            "status": "pending",
            "editable_files": ["code/mcagent/skills/diamond.py"],
            "attempts": 0,
        }

        # (a) ccnt=1 scout_island claimed first -> blocks ccnt=0 scout_island and surface_for_air,
        # while allowing disjoint GOAL_MISS:mine_diamond concurrently.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "SKILL_SPAM_LOOP:scout_island": make_scout_ccnt1("P0"),
                "REPLAN_OSCILLATION:scout_island": make_scout_ccnt0("P1"),
                "REPLAN_OSCILLATION:surface_for_air": make_air_ccnt0("P1"),
                "GOAL_MISS:mine_diamond": dict(diamond_goal),
            }
        claimed_a = soak_ctl.claim_parallel_incidents(self.session, max_workers=4, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed_a],
            ["SKILL_SPAM_LOOP:scout_island", "GOAL_MISS:mine_diamond"],
        )
        st_a = soak_ctl.read_state(self.session)
        self.assertEqual(st_a["incidents"]["REPLAN_OSCILLATION:scout_island"]["status"], "pending")
        self.assertEqual(st_a["incidents"]["REPLAN_OSCILLATION:surface_for_air"]["status"], "pending")

        # (b) ccnt=0 scout_island has higher priority -> claims with mine_diamond, blocks ccnt=1 scout_island.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "REPLAN_OSCILLATION:scout_island": make_scout_ccnt0("P0"),
                "SKILL_SPAM_LOOP:scout_island": make_scout_ccnt1("P1"),
                "GOAL_MISS:mine_diamond": dict(diamond_goal),
            }
        claimed_b = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed_b],
            ["REPLAN_OSCILLATION:scout_island", "GOAL_MISS:mine_diamond"],
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["SKILL_SPAM_LOOP:scout_island"]["status"],
            "pending",
        )

        # (c) ccnt=0 surface_for_air has higher priority -> claims with mine_diamond, blocks ccnt=1 scout_island.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "REPLAN_OSCILLATION:surface_for_air": make_air_ccnt0("P0"),
                "SKILL_SPAM_LOOP:scout_island": make_scout_ccnt1("P1"),
                "GOAL_MISS:mine_diamond": dict(diamond_goal),
            }
        claimed_c = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed_c],
            ["REPLAN_OSCILLATION:surface_for_air", "GOAL_MISS:mine_diamond"],
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["SKILL_SPAM_LOOP:scout_island"]["status"],
            "pending",
        )

        # ccnt=1 scout_island also blocks DISPATCH_STARVATION (domain:dispatch) and LLM_INVALID_OR_TIMEOUT (domain:llm_session).
        starve_rec = {
            "incident_id": "INC-DS-1",
            "fingerprint": "DISPATCH_STARVATION:enter_the_nether",
            "code": "DISPATCH_STARVATION",
            "goal_id": "enter_the_nether",
            "skill": "build_and_enter_portal",
            "severity": "P1",
            "category": "strategy",
            "status": "pending",
            "attempts": 0,
        }
        llm_rec = {
            "incident_id": "INC-LLM-1",
            "fingerprint": "LLM_INVALID_OR_TIMEOUT:session",
            "code": "LLM_INVALID_OR_TIMEOUT",
            "skill": "session",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "SKILL_SPAM_LOOP:scout_island": make_scout_ccnt1("P0"),
                "DISPATCH_STARVATION:enter_the_nether": dict(starve_rec),
                "LLM_INVALID_OR_TIMEOUT:session": dict(llm_rec),
                "GOAL_MISS:mine_diamond": dict(diamond_goal),
            }
        claimed_d = soak_ctl.claim_parallel_incidents(self.session, max_workers=4, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed_d],
            ["SKILL_SPAM_LOOP:scout_island", "GOAL_MISS:mine_diamond"],
        )
        st_d = soak_ctl.read_state(self.session)
        self.assertEqual(st_d["incidents"]["DISPATCH_STARVATION:enter_the_nether"]["status"], "pending")
        self.assertEqual(st_d["incidents"]["LLM_INVALID_OR_TIMEOUT:session"]["status"], "pending")

        # ccnt=1 GOAL_MISS:kill_a_mob (combat.py) blocks ccnt=0 COMBAT_OR_MOB_DEATH (domain:combat_session)
        # while still allowing GOAL_MISS:mine_diamond.
        combat_goal_ccnt1 = {
            "incident_id": "INC-CMB-GOAL",
            "fingerprint": "GOAL_MISS:kill_a_mob",
            "code": "GOAL_MISS",
            "goal_id": "kill_a_mob",
            "skill": "combat",
            "severity": "P0",
            "category": "strategy",
            "status": "pending",
            "editable_files": ["code/mcagent/skills/combat.py"],
            "attempts": 0,
            "gate_history": [{"failed_gate": "merge_conflict_requeue"}],
        }
        combat_death_ccnt0 = {
            "incident_id": "INC-CMB-DEATH",
            "fingerprint": "COMBAT_OR_MOB_DEATH:1",
            "code": "COMBAT_OR_MOB_DEATH",
            "skill": "combat",
            "severity": "P1",
            "category": "bug",
            "status": "pending",
            "attempts": 0,
        }
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:kill_a_mob": dict(combat_goal_ccnt1),
                "COMBAT_OR_MOB_DEATH:1": dict(combat_death_ccnt0),
                "GOAL_MISS:mine_diamond": dict(diamond_goal),
            }
        claimed_e = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual(
            [c["fingerprint"] for c in claimed_e],
            ["GOAL_MISS:kill_a_mob", "GOAL_MISS:mine_diamond"],
        )
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["COMBAT_OR_MOB_DEATH:1"]["status"],
            "pending",
        )


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

    @unittest.skipUnless(os.name == "posix", "POSIX process group semantics required")
    def test_batch_worker_timeout_kills_descendant_pgids_and_keeps_logs(self):
        import time

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:slow": {
                    "incident_id": "INC-TREE",
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

        pid_file = self.root / "detached_child.pid"
        detached_script = self.root / "detached_child.py"
        detached_script.write_text(
            f"""import os, signal, time
from pathlib import Path
os.setsid()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({repr(str(pid_file))}).write_text(str(os.getpid()), encoding="utf-8")
print("partial-before-timeout", flush=True)
time.sleep(30)
""",
            encoding="utf-8",
        )
        wrapper_script = self.root / "spawn_detached.py"
        wrapper_script.write_text(
            f"""import subprocess, sys, time
subprocess.Popen([sys.executable, {repr(str(detached_script))}])
time.sleep(30)
""",
            encoding="utf-8",
        )
        template = f"{sys.executable} {shlex.quote(str(wrapper_script))}"

        real_run = subprocess.run

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        t0 = time.monotonic()
        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=1,
                fixer_timeout_s=1,
                live_canary=False,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 6.0)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])

        self.assertTrue(pid_file.exists(), "detached child did not start before timeout")
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 1.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, f"detached child PID {child_pid} leaked after timeout")

        agent_log = (self.session / "fixer_runs" / "attempt-0001" / "fixer_agent.log").read_text(encoding="utf-8")
        self.assertIn("[fixer] worker timeout after 1s", agent_log)
        self.assertEqual(agent_log.count("partial-before-timeout"), 1)

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

    def test_run_fixer_batch_default_relaxed_three_way_parallel(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "SKILL_SPAM_LOOP:gather_wood": {
                    "incident_id": "INC-WOOD",
                    "fingerprint": "SKILL_SPAM_LOOP:gather_wood",
                    "code": "SKILL_SPAM_LOOP",
                    "skill": "gather_wood",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "attempts": 0,
                },
                "REPLAN_OSCILLATION:surface_for_air": {
                    "incident_id": "INC-AIR",
                    "fingerprint": "REPLAN_OSCILLATION:surface_for_air",
                    "code": "REPLAN_OSCILLATION",
                    "skill": "surface_for_air",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "attempts": 0,
                },
                "LLM_INVALID_OR_TIMEOUT:session": {
                    "incident_id": "INC-LLM",
                    "fingerprint": "LLM_INVALID_OR_TIMEOUT:session",
                    "code": "LLM_INVALID_OR_TIMEOUT",
                    "skill": "session",
                    "severity": "P2",
                    "category": "bug",
                    "status": "pending",
                    "attempts": 0,
                },
            }

        barrier_dir = self.root / "barrier_relaxed"
        barrier_dir.mkdir(parents=True, exist_ok=True)
        worker_script = self.root / "relaxed_barrier_worker.py"
        worker_script.write_text(
            f"""import sys, time
from pathlib import Path

bdir = Path({repr(str(barrier_dir))})
wt = Path(sys.argv[1])
skill = sys.argv[2]

(bdir / f"{{skill}}.ready").write_text("ready", encoding="utf-8")

t0 = time.time()
while time.time() - t0 < 8.0:
    if (bdir / "gather_wood.ready").exists() and (bdir / "surface_for_air.ready").exists() and (bdir / "session.ready").exists():
        break
    time.sleep(0.02)
else:
    sys.exit(1)

(wt / "code" / "mcagent" / "skills" / f"fix_{{skill}}.py").write_text(f"# {{skill}} fixed\\n", encoding="utf-8")
(wt / "code" / "tests" / f"test_relaxed_{{skill}}.py").write_text(f"OK = '{{skill}}'\\n", encoding="utf-8")
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
            # Omitting parallel_policy defaults to resolve_parallel_policy(session_dir, "relaxed").
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=3,
                live_canary=False,
            )

        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.get("ok") for r in results), f"results: {results}")
        st_after = soak_ctl.read_state(self.session)
        for fp in ("SKILL_SPAM_LOOP:gather_wood", "REPLAN_OSCILLATION:surface_for_air", "LLM_INVALID_OR_TIMEOUT:session"):
            self.assertEqual(st_after["incidents"][fp]["status"], "fixed")

    def test_run_fixer_batch_relaxed_progressive_conflict_fallback(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "SKILL_SPAM_LOOP:gather_wood": {
                    "incident_id": "INC-WOOD",
                    "fingerprint": "SKILL_SPAM_LOOP:gather_wood",
                    "code": "SKILL_SPAM_LOOP",
                    "skill": "gather_wood",
                    "severity": "P2",
                    "category": "strategy",
                    "status": "pending",
                    "attempts": 0,
                },
                "REPLAN_OSCILLATION:surface_for_air": {
                    "incident_id": "INC-AIR",
                    "fingerprint": "REPLAN_OSCILLATION:surface_for_air",
                    "code": "REPLAN_OSCILLATION",
                    "skill": "surface_for_air",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "attempts": 0,
                },
            }

        promote_dir = self.root / "promote_signals"
        promote_dir.mkdir(parents=True, exist_ok=True)
        worker_script = self.root / "starter_conflict_worker.py"
        worker_script.write_text(
            f"""import sys, time
from pathlib import Path

pdir = Path({repr(str(promote_dir))})
wt = Path(sys.argv[1])
skill = sys.argv[2]
val_map = {{"surface_for_air": 100, "gather_wood": 200, "mine_diamond": 300}}
if skill == "gather_wood":
    expected = "batch2.promoted" if (pdir / "batch2.active").exists() else "batch1.promoted"
    t0 = time.time()
    while time.time() - t0 < 8.0:
        if (pdir / expected).exists():
            break
        time.sleep(0.02)
    else:
        sys.exit(1)
(wt / "code" / "mcagent" / "loop" / "starter_skills.py").write_text(
    f"LIMIT = {{val_map[skill]}}\\n",
    encoding="utf-8",
)
""",
            encoding="utf-8",
        )
        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"
        real_run = subprocess.run
        real_verify = soak_ctl.verify_and_promote

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        def signaled_verify(session_dir, fp, **kw):
            res = real_verify(session_dir, fp, **kw)
            if fp == "REPLAN_OSCILLATION:surface_for_air":
                (promote_dir / "batch1.promoted").write_text("ok", encoding="utf-8")
            elif fp == "REPLAN_OSCILLATION:mine_diamond":
                (promote_dir / "batch2.promoted").write_text("ok", encoding="utf-8")
            return res

        # Batch 1: relaxed claims both starter tickets; surface_for_air promotes first,
        # gather_wood hits cherry-pick conflict (conflict_count=1) and expands strict files with relaxed tokens.
        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only), \
             patch.object(soak_ctl, "verify_and_promote", side_effect=signaled_verify):
            res1 = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        by_fp1 = {r["fingerprint"]: r for r in res1}
        self.assertTrue(by_fp1["REPLAN_OSCILLATION:surface_for_air"]["ok"])
        self.assertFalse(by_fp1["SKILL_SPAM_LOOP:gather_wood"]["ok"])
        self.assertEqual(by_fp1["SKILL_SPAM_LOOP:gather_wood"]["failed_gate"], "merge_conflict_requeue")

        st1 = soak_ctl.read_state(self.session)
        wood_rec1 = st1["incidents"]["SKILL_SPAM_LOOP:gather_wood"]
        self.assertEqual(wood_rec1["status"], "pending")
        self.assertEqual(wood_rec1.get("attempts", 0), 0)
        strict_wood1 = soak_ctl._claim_parallel_scope(wood_rec1, parallel_policy="strict")
        relaxed_wood1 = soak_ctl._claim_parallel_scope(wood_rec1, parallel_policy="relaxed")
        self.assertTrue(strict_wood1.issubset(relaxed_wood1))
        self.assertIn("skill:surface_for_air", relaxed_wood1)
        self.assertIn("domain:dispatch", relaxed_wood1)
        self.assertNotIn("skill:mine_diamond", relaxed_wood1)

        # Batch 2: add disjoint-domain mine_diamond that also edits starter_skills.py;
        # both claim (expanded starter scope vs skill:mine_diamond), mine_diamond promotes first,
        # gather_wood conflicts a 2nd time -> scope escalates to wildcard '*'.
        (promote_dir / "batch2.active").write_text("1", encoding="utf-8")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"]["REPLAN_OSCILLATION:mine_diamond"] = {
                "incident_id": "INC-DIA",
                "fingerprint": "REPLAN_OSCILLATION:mine_diamond",
                "code": "REPLAN_OSCILLATION",
                "skill": "mine_diamond",
                "severity": "P1",
                "category": "strategy",
                "status": "pending",
                "attempts": 0,
            }

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only), \
             patch.object(soak_ctl, "verify_and_promote", side_effect=signaled_verify):
            res2 = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        by_fp2 = {r["fingerprint"]: r for r in res2}
        self.assertTrue(by_fp2["REPLAN_OSCILLATION:mine_diamond"]["ok"])
        self.assertEqual(by_fp2["SKILL_SPAM_LOOP:gather_wood"]["failed_gate"], "merge_conflict_requeue")

        st2 = soak_ctl.read_state(self.session)
        wood_rec2 = st2["incidents"]["SKILL_SPAM_LOOP:gather_wood"]
        self.assertEqual(
            soak_ctl._claim_parallel_scope(wood_rec2, parallel_policy="relaxed"),
            frozenset({"*"}),
        )

        # With conflict_count==2, gather_wood now blocks concurrent GOAL_MISS claims under relaxed.
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"]["GOAL_MISS:combat"] = {
                "incident_id": "INC-CMB",
                "fingerprint": "GOAL_MISS:combat",
                "code": "GOAL_MISS",
                "goal_id": "kill_a_mob",
                "skill": "combat",
                "severity": "P3",
                "category": "strategy",
                "status": "pending",
                "editable_files": ["code/mcagent/skills/combat.py"],
                "attempts": 0,
            }
        claimed3 = soak_ctl.claim_parallel_incidents(self.session, max_workers=3, parallel_policy="relaxed")
        self.assertEqual([c["fingerprint"] for c in claimed3], ["SKILL_SPAM_LOOP:gather_wood"])
        self.assertEqual(
            soak_ctl.read_state(self.session)["incidents"]["GOAL_MISS:combat"]["status"],
            "pending",
        )


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

    def test_codex_fixer_presets_and_default_timeout(self):
        self.assertEqual(soak_ctl.DEFAULT_FIXER_TIMEOUT_S, 1000)
        parser = soak_ctl.build_parser()
        self.assertEqual(parser.parse_args(["fixer-loop", "--session-dir", "/tmp/s"]).fixer_timeout, 1000)
        self.assertEqual(parser.parse_args(["run", "--session-dir", "/tmp/s"]).fixer_timeout, 1000)

        for preset_name, tmpl in soak_ctl.FIXER_PRESETS.items():
            if not (preset_name == "codex" or preset_name.startswith("codex:")):
                continue
            self.assertIn("-s workspace-write", tmpl, preset_name)
            self.assertIn("sandbox_workspace_write.network_access=true", tmpl, preset_name)

    def test_verify_and_promote_handles_gate2_and_gate3_timeouts(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        with soak_ctl.locked_state(self.session) as st:
            st["incidents"] = {
                "GOAL_MISS:combat": {
                    "incident_id": "INC-G3",
                    "fingerprint": "GOAL_MISS:combat",
                    "code": "GOAL_MISS",
                    "skill": "combat",
                    "goal_id": "kill_a_mob",
                    "severity": "P1",
                    "category": "strategy",
                    "status": "pending",
                    "editable_files": ["code/mcagent/skills/combat.py"],
                    "attempts": 0,
                }
            }

        claimed = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(claimed)
        fixer_wt = Path(soak_ctl.read_state(self.session)["fixer_wt"])
        (fixer_wt / "code" / "mcagent" / "skills" / "combat.py").write_text("# candidate\n", encoding="utf-8")

        real_run = subprocess.run

        def mock_gate3_timeout(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                if "--live" in cmd:
                    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 90), output="connecting...", stderr="stuck")
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_gate3_timeout):
            res_g3 = soak_ctl.verify_and_promote(self.session, "GOAL_MISS:combat", live_canary=True)

        self.assertFalse(res_g3["ok"])
        self.assertEqual(res_g3["failed_gate"], "gate3_live_canary_timeout")
        g3_log = (Path(claimed["active_attempt_dir"]) / "gate3_live_canary.log").read_text(encoding="utf-8")
        self.assertIn("live canary timeout", g3_log)
        self.assertIn("connecting...", g3_log)
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")
        st_after_g3 = soak_ctl.read_state(self.session)
        self.assertEqual(st_after_g3["incidents"]["GOAL_MISS:combat"]["status"], "pending")
        self.assertEqual(st_after_g3["incidents"]["GOAL_MISS:combat"]["attempts"], 1)

        claimed2 = soak_ctl.claim_next_incident(self.session)
        self.assertIsNotNone(claimed2)
        (fixer_wt / "code" / "mcagent" / "skills" / "combat.py").write_text("# candidate 2\n", encoding="utf-8")

        def mock_gate2_timeout(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                if "unittest" in cmd:
                    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 180), output="hung test", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_gate2_timeout):
            res_g2 = soak_ctl.verify_and_promote(self.session, "GOAL_MISS:combat", live_canary=False)

        self.assertFalse(res_g2["ok"])
        self.assertEqual(res_g2["failed_gate"], "gate2_unittest_timeout")
        g2_log = (Path(claimed2["active_attempt_dir"]) / "gate2_unittest.log").read_text(encoding="utf-8")
        self.assertIn("unittest timeout after 180s", g2_log)
        self.assertIn("hung test", g2_log)
        self.assertEqual(_git(fixer_wt, "status", "--porcelain"), "")

    def test_run_fixer_batch_isolates_promotion_exception_and_keeps_siblings(self):
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

        worker_script = self.root / "batch_ok_worker.py"
        worker_script.write_text(
            """import sys
from pathlib import Path

wt = Path(sys.argv[1])
skill = sys.argv[2]
(wt / "code" / "mcagent" / "skills" / f"{skill}.py").write_text(f"# fixed {skill}\\n", encoding="utf-8")
""",
            encoding="utf-8",
        )
        template = f"{sys.executable} {shlex.quote(str(worker_script))} {{fixer_wt}} {{skill}}"

        real_run = subprocess.run
        real_verify = soak_ctl.verify_and_promote

        def mock_uv_only(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "uv":
                return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")
            return real_run(cmd, *args, **kwargs)

        def fail_combat_verify(session_dir, fp, **kw):
            if fp == "GOAL_MISS:combat":
                raise RuntimeError("transient promotion boom")
            return real_verify(session_dir, fp, **kw)

        with patch.object(soak_ctl.subprocess, "run", side_effect=mock_uv_only), \
             patch.object(soak_ctl, "verify_and_promote", side_effect=fail_combat_verify):
            results = soak_ctl.run_fixer_batch(
                self.session,
                fixer_cmd_template=template,
                max_workers=2,
                live_canary=False,
            )

        res_by_fp = {r["fingerprint"]: r for r in results}
        self.assertEqual(len(res_by_fp), 2)
        self.assertFalse(res_by_fp["GOAL_MISS:combat"]["ok"])
        self.assertEqual(res_by_fp["GOAL_MISS:combat"]["failed_gate"], "batch_promotion_error")
        self.assertTrue(res_by_fp["GOAL_MISS:diamond"]["ok"])

        st_after = soak_ctl.read_state(self.session)
        self.assertEqual(st_after["incidents"]["GOAL_MISS:combat"]["status"], "pending")
        self.assertEqual(st_after["incidents"]["GOAL_MISS:diamond"]["status"], "fixed")
        self.assertEqual(_git(Path(st_after["fixer_wt"]), "status", "--porcelain"), "")
        self.assertEqual(list((self.session / "worktrees").glob("worker-*")), [])

    def test_fixer_loop_daemon_continues_after_batch_exception(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")
        calls = 0

        def flaky_batch(session_dir, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient batch crash")
            (session_dir / "STOP").write_text("done", encoding="utf-8")
            return []

        with patch.object(soak_ctl, "run_fixer_batch", side_effect=flaky_batch), \
             patch.object(soak_ctl.time, "sleep"):
            rc = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--poll-s", "0.5",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX process group semantics required")
    def test_gate_subprocess_timeout_kills_child_tree(self):
        import time

        pid_file = self.root / "gate_detached.pid"
        child_script = self.root / "gate_child.py"
        child_script.write_text(
            f"""import os, signal, time
from pathlib import Path
os.setsid()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({repr(str(pid_file))}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
""",
            encoding="utf-8",
        )
        parent_script = self.root / "gate_parent.py"
        parent_script.write_text(
            f"""import subprocess, sys, time
subprocess.Popen([sys.executable, {repr(str(child_script))}])
time.sleep(30)
""",
            encoding="utf-8",
        )

        with self.assertRaises(subprocess.TimeoutExpired):
            soak_ctl._run_gate_subprocess(
                [sys.executable, str(parent_script)],
                cwd=self.root,
                env=dict(os.environ),
                timeout_s=1,
            )

        self.assertTrue(pid_file.exists())
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 1.0
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, f"gate child PID {child_pid} leaked after timeout")

    def test_validate_and_resolve_parallel_policy(self):
        self.assertEqual(soak_ctl.PARALLEL_POLICIES, ("relaxed", "strict"))

        self.assertEqual(soak_ctl._validate_parallel_policy("relaxed"), "relaxed")
        self.assertEqual(soak_ctl._validate_parallel_policy("strict"), "strict")
        self.assertEqual(soak_ctl._validate_parallel_policy(" relaxed "), "relaxed")
        self.assertEqual(soak_ctl._validate_parallel_policy(" strict\n"), "strict")

        for bad in ("", "   ", "RELAXED", "fast", None, 123, True, False, []):
            with self.assertRaises(ValueError):
                soak_ctl._validate_parallel_policy(bad)

        self.session.mkdir(parents=True, exist_ok=True)
        # Missing state.json -> falls back to default_policy ("relaxed" by default).
        (self.session / "state.json").unlink(missing_ok=True)
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session), "relaxed")
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session, "strict"), "strict")

        # Corrupted JSON -> fallback.
        (self.session / "state.json").write_text("{corrupted", encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session), "relaxed")

        # Missing key / invalid value -> fallback.
        (self.session / "state.json").write_text(json.dumps({}), encoding="utf-8")
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session), "relaxed")
        (self.session / "state.json").write_text(
            json.dumps({"fixer_parallel_policy": "invalid"}),
            encoding="utf-8",
        )
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session), "relaxed")

        # Valid values in state.json (including whitespace).
        (self.session / "state.json").write_text(
            json.dumps({"fixer_parallel_policy": " strict "}),
            encoding="utf-8",
        )
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session), "strict")
        (self.session / "state.json").write_text(
            json.dumps({"fixer_parallel_policy": "relaxed"}),
            encoding="utf-8",
        )
        self.assertEqual(soak_ctl.resolve_parallel_policy(self.session, "strict"), "relaxed")

    def test_run_cli_fixer_parallel_policy_propagation(self):
        parser = soak_ctl.build_parser()
        self.assertEqual(
            parser.parse_args(["run", "--session-dir", str(self.session)]).fixer_parallel_policy,
            "relaxed",
        )
        self.assertEqual(
            parser.parse_args([
                "run", "--session-dir", str(self.session), "--fixer-parallel-policy", "strict"
            ]).fixer_parallel_policy,
            "strict",
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            parser.parse_args([
                "run", "--session-dir", str(self.session), "--fixer-parallel-policy", "invalid"
            ])
        self.assertEqual(cm.exception.code, 2)

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

        # Default "relaxed" saves to state.json and propagates ["--fixer-parallel-policy", "relaxed"] to fixer-loop.
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
        fixer_inv = [c for c in spawned_cmds if "fixer-loop" in c]
        self.assertEqual(len(fixer_inv), 1)
        idx = fixer_inv[0].index("--fixer-parallel-policy")
        self.assertEqual(fixer_inv[0][idx + 1], "relaxed")
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_parallel_policy"], "relaxed")

        # Explicit "strict" saves to state.json and propagates ["--fixer-parallel-policy", "strict"] to fixer-loop.
        spawned_cmds.clear()
        with patch.object(soak_ctl, "find_repo_root", return_value=self.repo), \
             patch.object(soak_ctl, "check_server_target", return_value=[]), \
             patch.object(soak_ctl, "init_session", return_value={}), \
             patch.object(soak_ctl.subprocess, "Popen", side_effect=capture_popen):
            rc_strict = soak_ctl.main([
                "run",
                "--session-dir", str(self.session),
                "--branch", "feat/soak-autofix",
                "--server", "local",
                "--episodes", "1",
                "--fixer-parallel-policy", "strict",
            ])
        self.assertEqual(rc_strict, 0)
        fixer_inv_s = [c for c in spawned_cmds if "fixer-loop" in c]
        self.assertEqual(len(fixer_inv_s), 1)
        idx_s = fixer_inv_s[0].index("--fixer-parallel-policy")
        self.assertEqual(fixer_inv_s[0][idx_s + 1], "strict")
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_parallel_policy"], "strict")

    def test_fixer_loop_cli_parallel_policy_dispatch(self):
        parser = soak_ctl.build_parser()
        self.assertIsNone(
            parser.parse_args(["fixer-loop", "--session-dir", str(self.session)]).fixer_parallel_policy
        )

        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        # Omitted CLI arg with no state override -> falls back to "relaxed".
        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": True}]) as mock_batch:
            rc = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_batch.call_args.kwargs["parallel_policy"], "relaxed")

        # Omitted CLI arg honors state.json["fixer_parallel_policy"] = "strict".
        with soak_ctl.locked_state(self.session) as st:
            st["fixer_parallel_policy"] = "strict"
        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": True}]) as mock_batch:
            rc_state = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--once",
            ])
        self.assertEqual(rc_state, 0)
        self.assertEqual(mock_batch.call_args.kwargs["parallel_policy"], "strict")

        # Explicit CLI arg overrides state.json.
        with soak_ctl.locked_state(self.session) as st:
            st["fixer_parallel_policy"] = "relaxed"
        with patch.object(soak_ctl, "run_fixer_batch", return_value=[{"ok": True}]) as mock_batch:
            rc_override = soak_ctl.main([
                "fixer-loop",
                "--session-dir", str(self.session),
                "--fixer-preset", "codex",
                "--fixer-workers", "3",
                "--fixer-parallel-policy", "strict",
                "--once",
            ])
        self.assertEqual(rc_override, 0)
        self.assertEqual(mock_batch.call_args.kwargs["parallel_policy"], "strict")

    def test_inject_and_set_goal_parallel_policy_hot_switch_and_status(self):
        soak_ctl.init_session(self.repo, self.session, target_branch="feat/soak-autofix")

        res_bad = soak_ctl.inject_and_set_goal(self.session, fixer_parallel_policy="invalid")
        self.assertFalse(res_bad["ok"])
        self.assertEqual(res_bad["failed_step"], "invalid_fixer_parallel_policy")

        res_strict = soak_ctl.inject_and_set_goal(self.session, fixer_parallel_policy="strict")
        self.assertTrue(res_strict["ok"])
        self.assertEqual(res_strict["fixer_parallel_policy"], "strict")
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_parallel_policy"], "strict")
        report_strict = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("fixer_parallel_policy=strict", report_strict)

        rc_set = soak_ctl.main([
            "set-goal",
            "--session-dir", str(self.session),
            "--fixer-parallel-policy", "relaxed",
        ])
        self.assertEqual(rc_set, 0)
        self.assertEqual(soak_ctl.read_state(self.session)["fixer_parallel_policy"], "relaxed")
        report_relaxed = (self.session / "REVIEW_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("fixer_parallel_policy=relaxed", report_relaxed)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_status = soak_ctl.main(["status", "--session-dir", str(self.session)])
        self.assertEqual(rc_status, 0)
        status_data = json.loads(buf.getvalue())
        self.assertEqual(status_data["fixer_parallel_policy"], "relaxed")


if __name__ == "__main__":
    unittest.main()

