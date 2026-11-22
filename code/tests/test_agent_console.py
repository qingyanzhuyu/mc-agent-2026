"""Offline unit & loopback HTTP tests for `tools/agent-console/server.py`.

Zero network calls, zero real Minecraft logins, and zero writes to real `.env*`
or `logs/` directories.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
REPO_ROOT = CODE_DIR.parent
SERVER_PATH = REPO_ROOT / "tools" / "agent-console" / "server.py"


def _load_console_server():
    spec = importlib.util.spec_from_file_location("agent_console_server", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


srv = _load_console_server()


def _write_env(path: Path, mapping: dict[str, str]) -> Path:
    lines = [f"{k}={v}" for k, v in mapping.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class ReadEnvSummaryTests(unittest.TestCase):
    def test_never_exposes_secret_values_or_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            env_file = _write_env(
                tmp_root / ".env.match",
                {
                    "MC_API_BASE": srv.OFFICIAL_CAMPUS_API,
                    "MC_USER": "team_official_bot",
                    "MC_PASS": "super-secret-mc-pass-987",
                    "LLM_BASE": "https://api.nju-aia.com/v1",
                    "LLM_KEY": "sk-team-secret-key-xyz",
                    "LLM_CUSTOM_KEY": "sk-custom-secret-key-abc",
                    "LLM_MODEL": "default",
                    "LLM_PROFILE": "official",
                    "LLM_MAX_CONCURRENCY": "2",
                },
            )
            with mock.patch.object(srv, "REPO_ROOT", tmp_root):
                summary = srv.read_env_summary(env_file)

        self.assertTrue(summary["exists"])
        self.assertEqual(summary["path"], ".env.match")
        values = summary["values"]
        for secret_key in ("MC_PASS", "LLM_KEY", "LLM_CUSTOM_KEY"):
            self.assertNotIn(secret_key, values)

        serialized = json.dumps(summary, ensure_ascii=False)
        for secret_val in (
            "super-secret-mc-pass-987",
            "sk-team-secret-key-xyz",
            "sk-custom-secret-key-abc",
        ):
            self.assertNotIn(secret_val, serialized)

        self.assertEqual(
            summary["secrets"],
            {
                "has_mc_pass": True,
                "mc_pass_is_localdev": False,
                "has_llm_key": True,
                "llm_key_is_test_prefix": False,
                "has_llm_custom_key": True,
            },
        )

    def test_placeholder_localdev_and_test_prefix_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            env_file = _write_env(
                tmp_root / ".env",
                {
                    "MC_USER": "qyzy_dev",
                    "MC_PASS": "localdev",
                    "LLM_KEY": "sk-mRbe_public_test_000",
                },
            )
            with mock.patch.object(srv, "REPO_ROOT", tmp_root):
                summary = srv.read_env_summary(env_file)

        self.assertEqual(
            summary["secrets"],
            {
                "has_mc_pass": False,
                "mc_pass_is_localdev": True,
                "has_llm_key": True,
                "llm_key_is_test_prefix": True,
                "has_llm_custom_key": False,
            },
        )

    def test_missing_env_file_returns_safe_empty_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            with mock.patch.object(srv, "REPO_ROOT", tmp_root):
                summary = srv.read_env_summary(tmp_root / ".env.missing")

        self.assertFalse(summary["exists"])
        self.assertFalse(summary["secrets"]["has_mc_pass"])
        self.assertFalse(summary["secrets"]["has_llm_key"])
        self.assertFalse(summary["secrets"]["has_llm_custom_key"])


class BuildLaunchSpecMatchModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.env_dev = _write_env(
            self.tmp_dir / ".env",
            {
                "MC_API_BASE": "http://127.0.0.1:8787/api/v1",
                "MC_USER": "qyzy_dev",
                "MC_PASS": "localdev",
                "LLM_KEY": "sk-mRbe_dev_test",
                "LLM_CUSTOM_KEY": "sk-custom-dev",
            },
        )
        self.env_match = _write_env(
            self.tmp_dir / ".env.match",
            {
                "MC_API_BASE": srv.OFFICIAL_CAMPUS_API,
                "MC_USER": "nmo_official_player",
                "MC_PASS": "real-match-pass-secret",
                "MC_PROXY": "http://127.0.0.1:7897",
                "LLM_BASE": "https://api.nju-aia.com/v1",
                "LLM_KEY": "sk-team-real-aia-secret",
                "LLM_MODEL": "some-other-model",
                "LLM_MAX_CONCURRENCY": "8",
                "MCAGENT_TRAIN_ON_OFFICIAL": "1",
            },
        )
        self.run_dir = self.tmp_dir / "run-match"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_missing_account_reserved(self) -> None:
        with self.assertRaisesRegex(ValueError, "docs/05"):
            srv.build_launch_spec(
                {"mode": "match", "api_base": srv.OFFICIAL_CAMPUS_API},
                env_dev_path=self.env_dev,
                env_match_path=self.env_match,
                run_dir=self.run_dir,
            )

    def test_rejects_non_official_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "官方端点"):
            srv.build_launch_spec(
                {
                    "mode": "match",
                    "account_reserved": True,
                    "api_base": "http://127.0.0.1:8787/api/v1",
                },
                env_dev_path=self.env_dev,
                env_match_path=self.env_match,
                run_dir=self.run_dir,
            )

    def test_rejects_placeholder_user_or_pass(self) -> None:
        bad_match = _write_env(
            self.tmp_dir / ".env.match.baduser",
            {
                "MC_API_BASE": srv.OFFICIAL_CAMPUS_API,
                "MC_USER": "qyzy_dev",
                "MC_PASS": "localdev",
                "LLM_KEY": "sk-team-real-aia-secret",
            },
        )
        with self.assertRaisesRegex(ValueError, "正式皮肤站账号或密码"):
            srv.build_launch_spec(
                {"mode": "match", "account_reserved": True},
                env_dev_path=self.env_dev,
                env_match_path=bad_match,
                run_dir=self.run_dir,
            )

    def test_rejects_test_prefix_llm_key(self) -> None:
        bad_key_match = _write_env(
            self.tmp_dir / ".env.match.badkey",
            {
                "MC_API_BASE": srv.OFFICIAL_CAMPUS_API,
                "MC_USER": "nmo_official_player",
                "MC_PASS": "real-match-pass-secret",
                "LLM_KEY": "sk-mRbe_public_test_key",
            },
        )
        with self.assertRaisesRegex(ValueError, "sk-mRbe"):
            srv.build_launch_spec(
                {"mode": "match", "account_reserved": True, "strategy": "llm"},
                env_dev_path=self.env_dev,
                env_match_path=bad_key_match,
                run_dir=self.run_dir,
            )

    def test_valid_match_spec_enforces_compliance_and_no_secret_leak(self) -> None:
        spec = srv.build_launch_spec(
            {
                "mode": "match",
                "account_reserved": True,
                "api_base": srv.OFFICIAL_CAMPUS_API,
                "strategy": "llm",
                "llm_max_concurrency": 16,
            },
            env_dev_path=self.env_dev,
            env_match_path=self.env_match,
            run_dir=self.run_dir,
        )

        self.assertEqual(spec["mode"], "match")
        self.assertIn("--live", spec["argv"])
        self.assertIn("--llm", spec["argv"])
        self.assertIn("--strategy", spec["argv"])

        env_ov = spec["env_overrides"]
        self.assertEqual(env_ov["LLM_PROFILE"], "official")
        self.assertEqual(env_ov["LLM_MODEL"], "default")
        self.assertLessEqual(int(env_ov["LLM_MAX_CONCURRENCY"]), 2)
        self.assertEqual(env_ov["MC_PROXY"], "")
        self.assertEqual(env_ov["MCAGENT_TRAIN_ON_OFFICIAL"], "")

        for secret in ("real-match-pass-secret", "sk-team-real-aia-secret", "sk-custom-dev"):
            self.assertNotIn(secret, spec["cmd_display"])
            self.assertNotIn(secret, " ".join(spec["argv"]))
            self.assertNotIn(secret, json.dumps(spec["meta"], ensure_ascii=False))


class BuildLaunchSpecLocalModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.env_dev = _write_env(
            self.tmp_dir / ".env",
            {
                "MC_LOCAL_API_BASE": "http://127.0.0.1:8787/api/v1",
                "LLM_CUSTOM_BASE": "http://127.0.0.1:11434/v1",
                "LLM_CUSTOM_KEY": "sk-local-custom-secret-999",
                "LLM_CUSTOM_MODEL": "qwen-local-test",
            },
        )
        self.env_match = _write_env(
            self.tmp_dir / ".env.match",
            {
                "MC_API_BASE": srv.OFFICIAL_CAMPUS_API,
                "MC_USER": "nmo_official_player",
                "MC_PASS": "real-match-pass",
                "LLM_KEY": "sk-team-real-aia",
            },
        )
        self.run_dir = self.tmp_dir / "run-local"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_official_endpoints(self) -> None:
        for official_url in (srv.OFFICIAL_CAMPUS_API, srv.OFFICIAL_PUBLIC_API):
            with self.subTest(api_base=official_url):
                with self.assertRaisesRegex(ValueError, "禁止填写官方比赛服端点"):
                    srv.build_launch_spec(
                        {"mode": "local", "api_base": official_url},
                        env_dev_path=self.env_dev,
                        env_match_path=self.env_match,
                        run_dir=self.run_dir,
                    )

    def test_custom_llm_profile_and_user_isolation(self) -> None:
        spec = srv.build_launch_spec(
            {
                "mode": "local",
                "bot_user": "local_qa_bot",
                "strategy": "llm",
                "llm_profile": "custom",
            },
            env_dev_path=self.env_dev,
            env_match_path=self.env_match,
            run_dir=self.run_dir,
        )

        argv = spec["argv"]
        self.assertIn("--local-target", argv)
        idx_user = argv.index("--user")
        self.assertEqual(argv[idx_user + 1], "local_qa_bot")
        self.assertNotIn("--live", argv)
        self.assertIn("--llm", argv)

        env_ov = spec["env_overrides"]
        self.assertEqual(env_ov["LLM_PROFILE"], "custom")
        self.assertEqual(env_ov["LLM_CUSTOM_BASE"], "http://127.0.0.1:11434/v1")
        self.assertEqual(env_ov["LLM_CUSTOM_KEY"], "sk-local-custom-secret-999")
        self.assertEqual(env_ov["LLM_CUSTOM_MODEL"], "qwen-local-test")
        self.assertNotIn("sk-local-custom-secret-999", spec["cmd_display"])

    def test_none_llm_profile_uses_rule_strategy(self) -> None:
        spec = srv.build_launch_spec(
            {
                "mode": "local",
                "bot_user": "rule_bot",
                "strategy": "llm",
                "llm_profile": "none",
            },
            env_dev_path=self.env_dev,
            env_match_path=self.env_match,
            run_dir=self.run_dir,
        )

        argv = spec["argv"]
        self.assertIn("--local-target", argv)
        idx_strategy = argv.index("--strategy")
        self.assertEqual(argv[idx_strategy + 1], "rule")
        self.assertNotIn("--llm", argv)
        self.assertEqual(spec["meta"]["llm_profile"], "none")
        self.assertEqual(spec["meta"]["llm_model"], "rule-only")

    def test_focus_mode_with_json_skill_args(self) -> None:
        spec = srv.build_launch_spec(
            {
                "mode": "local",
                "bot_user": "focus_bot",
                "llm_profile": "none",
                "run_mode": "focus",
                "focus_skill": "gather_wood",
                "focus_args": '{"count": 4, "prefer": "oak"}',
            },
            env_dev_path=self.env_dev,
            env_match_path=self.env_match,
            run_dir=self.run_dir,
        )

        argv = spec["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "focus")
        self.assertEqual(argv[argv.index("--skill") + 1], "gather_wood")
        self.assertEqual(argv[argv.index("--skill-args") + 1], '{"count":4,"prefer":"oak"}')


class ParseRunDetailTests(unittest.TestCase):
    def test_parses_synthetic_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "synthetic-run"
            inc_dir = run_dir / "incidents"
            inc_dir.mkdir(parents=True)

            timeline_rows = [
                {
                    "event": "run_start",
                    "t": 1700000000.0,
                    "elapsed_s": 0.0,
                    "source": "local-target",
                    "git": {"commit": "abc1234", "branch": "main", "dirty": False},
                    "meta": {
                        "strategy": "llm",
                        "bot_user": "synth_bot",
                        "llm_model": "default",
                        "min_verified": "offline",
                        "max_seconds": 600,
                        "max_steps": 100,
                    },
                },
                {
                    "event": "objective_decision",
                    "elapsed_s": 1.0,
                    "goal_id": "prep_wood",
                    "source": "llm",
                    "trigger": "boot",
                    "adopted": True,
                    "reason": "need initial wood",
                },
                {
                    "event": "skill_start",
                    "elapsed_s": 2.0,
                    "skill": "gather_wood",
                    "args": {"count": 4},
                    "source": "llm",
                    "reason": "推进目标 prep_wood",
                },
                {
                    "event": "step",
                    "elapsed_s": 5.0,
                    "skill": "gather_wood",
                    "op": "move_to",
                    "pos": [0.0, 64.0, 0.0],
                    "hp": 20.0,
                    "food": 20.0,
                    "record": {"op": "move_to", "ok": True, "ms": 42.0},
                },
                {
                    "event": "step",
                    "elapsed_s": 10.0,
                    "skill": "gather_wood",
                    "op": "mine_block",
                    "pos": [3.0, 64.0, 4.0],
                    "hp": 18.5,
                    "food": 19.0,
                    "record": {"op": "mine_block", "ok": True, "ms": 118.0, "item": "oak_log"},
                },
                {
                    "event": "llm_call",
                    "elapsed_s": 12.0,
                    "ok": True,
                    "ms": 350.0,
                    "prompt_tokens": 1200,
                    "completion_tokens": 150,
                    "total_tokens": 1350,
                    "skill": "gather_wood",
                },
                {
                    "event": "goal_reached",
                    "elapsed_s": 14.0,
                    "goal_id": "prep_wood",
                },
                {
                    "event": "death_recovered",
                    "elapsed_s": 16.0,
                    "deaths_total": 1,
                    "cause": "fell from height",
                    "respawn_ms": 420,
                },
                {
                    "event": "skill_end",
                    "elapsed_s": 20.0,
                    "skill": "gather_wood",
                    "outcome": "completed",
                },
                {
                    "event": "run_end",
                    "elapsed_s": 22.0,
                    "stop_reason": "target_goal_reached",
                    "deaths": 1,
                    "goals_reached": {"prep_wood": 14.0},
                },
            ]
            (run_dir / "timeline.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in timeline_rows) + "\n",
                encoding="utf-8",
            )

            curve_rows = [
                {
                    "elapsed_s": 10.0,
                    "server_score_v2": 10,
                    "inferred_score_v2": 15,
                    "server_advancements": 1,
                    "deaths": 0,
                    "prompt_tokens": 600,
                    "completion_tokens": 80,
                    "total_tokens": 680,
                    "goals_reached": [],
                    "inferred_milestones": ["oak_log"],
                },
                {
                    "elapsed_s": 22.0,
                    "server_score_v2": 35,
                    "inferred_score_v2": 45,
                    "server_advancements": 3,
                    "deaths": 1,
                    "prompt_tokens": 1200,
                    "completion_tokens": 150,
                    "total_tokens": 1350,
                    "goals_reached": ["prep_wood"],
                    "inferred_milestones": ["oak_log", "crafting_table"],
                },
            ]
            (run_dir / "progress_curve.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in curve_rows) + "\n",
                encoding="utf-8",
            )

            inc_payload = {
                "code": "NAV_STUCK",
                "severity": "P1",
                "category": "navigation",
                "title": "Blocked by cliff",
                "skill": "gather_wood",
                "elapsed_s": 15.2,
                "count": 2,
            }
            (inc_dir / "INC-001.json").write_text(json.dumps(inc_payload), encoding="utf-8")
            (run_dir / "summary.md").write_text("# Synthetic Summary\nAll good.\n", encoding="utf-8")
            (run_dir / "world_memo.json").write_text(
                json.dumps(
                    {
                        "biomes": {"plains": {}, "forest": {}},
                        "landmarks": {"base_camp": [0, 64, 0], "oak_grove": [12, 65, -8]},
                    }
                ),
                encoding="utf-8",
            )

            detail = srv.parse_run_detail(run_dir, is_active=False)

        vitals = detail["vitals"]
        self.assertEqual(vitals["pos"], [3.0, 64.0, 4.0])
        self.assertEqual(vitals["hp"], 18.5)
        self.assertEqual(vitals["food"], 19.0)
        self.assertEqual(vitals["elapsed_s"], 22.0)
        self.assertEqual(vitals["steps"], 2)
        self.assertEqual(vitals["deaths"], 1)
        self.assertEqual(vitals["distance_m"], 5.0)
        self.assertEqual(vitals["stop_reason"], "target_goal_reached")
        self.assertEqual(vitals["last_op"], "mine_block")
        self.assertTrue(vitals["last_op_ok"])
        self.assertEqual(vitals["last_op_ms"], 118.0)

        scores = detail["scores"]
        self.assertEqual(scores["server_score_v2"], 35)
        self.assertEqual(scores["server_advancements"], 3)
        self.assertEqual(scores["inferred_score_v2"], 45)
        self.assertEqual(scores["inferred_milestones"], ["oak_log", "crafting_table"])
        self.assertEqual([g["id"] for g in scores["goals_reached"]], ["prep_wood"])
        self.assertEqual(scores["goals_reached"][0]["t"], 14.0)

        traj = detail["trajectory"]
        self.assertEqual(len(traj["points"]), 4)  # 2 steps + 1 goal + 1 death
        self.assertEqual(
            traj["bounds"],
            {
                "min_x": 0.0,
                "max_x": 3.0,
                "min_y": 64.0,
                "max_y": 64.0,
                "min_z": 0.0,
                "max_z": 4.0,
            },
        )

        self.assertEqual(len(detail["curve"]), 2)
        self.assertEqual(detail["curve"][-1]["server_score"], 35)

        self.assertEqual(len(detail["skill_stats"]), 1)
        sk = detail["skill_stats"][0]
        self.assertEqual(sk["skill"], "gather_wood")
        self.assertEqual(sk["started"], 1)
        self.assertEqual(sk["ok"], 1)
        self.assertEqual(sk["steps"], 2)
        self.assertEqual(sk["total_s"], 18.0)

        self.assertEqual(len(detail["events"]), len(timeline_rows))
        self.assertEqual(len(detail["incidents"]), 1)
        self.assertEqual(detail["incidents"][0]["code"], "NAV_STUCK")
        self.assertIn("--replay", detail["incidents"][0]["replay_cmd"])
        self.assertEqual(detail["landmarks"]["biomes"], ["plains", "forest"])
        self.assertEqual(detail["landmarks"]["landmarks"], ["base_camp", "oak_grove"])
        self.assertIn("Synthetic Summary", detail["summary_md"])


class HandoverAuditTests(unittest.TestCase):
    def test_handover_audit_totals_and_zero_tracked_secrets(self) -> None:
        audit = srv.build_handover_audit()

        self.assertEqual(audit["skills"]["total"], len(srv.REGISTRY))
        self.assertEqual(
            audit["skills"]["live"] + audit["skills"]["offline"],
            audit["skills"]["total"],
        )
        self.assertEqual(audit["goals"]["total"], len(srv.GOALS))
        self.assertGreater(audit["goals"]["max_score"], 0)
        self.assertEqual(audit["tracked_secrets"], [])


class LoopbackHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = srv.create_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)

    def _get(self, path: str) -> tuple[int, str, bytes]:
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        with self.opener.open(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read().decode("utf-8"))
            finally:
                err.close()

    def test_refuses_non_loopback_bind(self) -> None:
        with self.assertRaises(ValueError):
            srv.create_server("0.0.0.0", 0)

    def test_static_assets_and_api_get_endpoints(self) -> None:
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"AIAMC", body)

        status, ctype, body = self._get("/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", ctype)
        self.assertGreater(len(body), 0)

        status, ctype, body = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        self.assertGreater(len(body), 0)

        status, _, body = self._get("/api/bootstrap")
        self.assertEqual(status, 200)
        boot = json.loads(body.decode("utf-8"))
        self.assertIn("endpoints", boot)
        self.assertIn("env_dev", boot)
        self.assertIn("env_match", boot)
        self.assertIn("goals_catalog", boot)
        self.assertIn("skills_catalog", boot)
        self.assertIn("handover", boot)

        status, _, body = self._get("/api/runs")
        self.assertEqual(status, 200)
        runs_data = json.loads(body.decode("utf-8"))
        self.assertIn("process", runs_data)
        self.assertIsInstance(runs_data["runs"], list)

        status, _, body = self._get("/api/preflight?source=match&live=0")
        self.assertEqual(status, 200)
        pf_data = json.loads(body.decode("utf-8"))
        self.assertIn("ok", pf_data)
        self.assertIn("checks", pf_data)

        status, _, body = self._get("/api/handover")
        self.assertEqual(status, 200)
        handover = json.loads(body.decode("utf-8"))
        self.assertEqual(handover["tracked_secrets"], [])
        self.assertEqual(handover["skills"]["total"], len(srv.REGISTRY))

    def test_post_run_start_invalid_mode_returns_400_json(self) -> None:
        status, data = self._post_json("/api/run/start", {"mode": "invalid_mode"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("不支持的模式", data["error"])


if __name__ == "__main__":
    unittest.main()
