"""双工位挂机本地控制台（tools/soak-autofix/dashboard.py）离线单测（verified: offline）。

覆盖：台账静态解析、当前轮实时摘要、/proc 守护进程识别、实时参数写入与越界拒绝、
启动参数校验与命令拼装、重复启动拦截、HTTP 层 Host / Origin / Content-Type 防护。
不启动真实守护、不连游戏服。
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dashboard():
    mod_path = REPO_ROOT / "tools" / "soak-autofix" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("soak_dashboard_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dash = _load_dashboard()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        # 默认启动目标已改为比赛服；与比赛服无关的用例统一从「上次用过自建测试服」开始
        (self.session / dash.LAST_START_NAME).write_text(json.dumps({"server": "local"}), encoding="utf-8")
        self.console = dash.Console(self.root, self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def _init_state(self, **extra):
        st = {"target_branch": "feat/soak-autofix", "base_commit": "a" * 40, "current_tip": "b" * 40,
              "episodes": [], "incidents": {}, "commits": [], "player_model": "DeepSeek-V4.1-Flash"}
        st.update(extra)
        (self.session / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def test_parse_ledger_reads_real_goals_file(self):
        text = (REPO_ROOT / "code" / "mcagent" / "loop" / "goals.py").read_text(encoding="utf-8")
        goals = {g["id"]: g for g in dash.parse_ledger(text)}
        self.assertEqual(goals["prep_wood"]["stage"], "graduated")
        self.assertEqual(goals["smelt_iron"]["stage"], "draft")
        self.assertEqual(goals["shiny_gear"]["score"], 60)
        self.assertEqual(goals["smelt_iron"]["skill"], "smelt_iron")
        self.assertIn("prep_diamond_pickaxe", goals)

    def test_live_episode_summary_running_and_finished(self):
        ep = self.session / "episodes" / "ep-0001"
        rows = [
            {"event": "run_start", "t": 1000.0, "git": {"commit": "abcdef1234"},
             "meta": {"use_llm": True, "llm_model": "DeepSeek-V4.1-Flash", "max_steps": 45,
                      "max_seconds": 240.0, "bot_user": "agy_soak_r001", "target_goal": "smelt_iron"}},
            {"event": "llm_call", "elapsed_s": 2.0, "ms": 1500, "ok": True, "skill": "gather_wood",
             "cumulative_total_tokens": 600},
            {"event": "skill_start", "elapsed_s": 2.1, "skill": "gather_wood", "args": {"count": 3}},
            {"event": "step", "elapsed_s": 5.0, "op": "dig", "hp": 20.0, "food": 18, "record": {"op": "dig"}},
            {"event": "skill_end", "elapsed_s": 9.0, "skill": "gather_wood", "outcome": "completed"},
            {"event": "llm_call", "elapsed_s": 10.0, "ms": 3500, "ok": False, "skill": None,
             "cumulative_total_tokens": 900},
            {"event": "skill_start", "elapsed_s": 10.5, "skill": "craft_gear", "args": {}},
            {"event": "step", "elapsed_s": 12.0, "op": "craft", "hp": 19.0, "food": 18,
             "record": {"op": "craft", "error_code": "need_crafting_table"}},
        ]
        _write_jsonl(ep / "timeline.jsonl", rows)
        _write_jsonl(ep / "progress_curve.jsonl", [{"deaths": 0, "goals_reached": ["prep_wood"],
                                                    "server_score_v2": 0, "inferred_score_v2": 0}])
        live = dash.summarize_live_episode(ep, now=1030.0)
        self.assertFalse(live["finished"])
        self.assertEqual(live["elapsed_s"], 30.0)
        self.assertEqual((live["steps"], live["max_steps"], live["max_seconds"]), (2, 45, 240.0))
        self.assertEqual(live["current_skill"], "craft_gear")
        self.assertEqual((live["skills_completed"], live["llm_calls"], live["llm_fail"]), (1, 2, 1))
        self.assertEqual(live["tokens"], 900)
        self.assertEqual((live["hp"], live["commit"], live["target_goal"]), (19.0, "abcdef1", "smelt_iron"))
        self.assertEqual(live["goals_reached"], ["prep_wood"])
        self.assertIn("need_crafting_table", live["recent"][0]["text"])

        rows.append({"event": "run_end", "elapsed_s": 240.3, "stop_reason": "max_seconds_reached",
                     "total_tokens": 1200, "goals_reached": {"prep_wood": 9.0}})
        _write_jsonl(ep / "timeline.jsonl", rows)
        done = dash.summarize_live_episode(ep, now=99999.0)
        self.assertTrue(done["finished"])
        self.assertEqual((done["elapsed_s"], done["tokens"]), (240.3, 1200))
        self.assertIsNone(done["current_skill"])
        self.assertIsNone(dash.summarize_live_episode(self.session / "episodes" / "ep-0009"))

        # 中断时连写两条 run_end 取第一条；重启复用轮次号后只看最后一次 run_start 之后
        rows.append({"event": "run_end", "elapsed_s": 3.8, "stop_reason": "stopped"})
        _write_jsonl(ep / "timeline.jsonl", rows)
        self.assertEqual(dash.summarize_live_episode(ep)["stop_reason"], "max_seconds_reached")
        rows.extend([
            {"event": "run_start", "t": 5000.0, "meta": {"max_steps": 80, "max_seconds": 420.0}},
            {"event": "step", "elapsed_s": 1.0, "op": "dig", "record": {}},
        ])
        _write_jsonl(ep / "timeline.jsonl", rows)
        again = dash.summarize_live_episode(ep, now=5010.0)
        self.assertEqual((again["finished"], again["steps"], again["max_steps"], again["elapsed_s"]),
                         (False, 1, 80, 10.0))

    def test_find_soak_processes_matches_session_by_resolved_path(self):
        proc = self.root / "proc"
        cwd_dir = self.root
        def mk(pid: str, argv: list[str]):
            d = proc / pid
            d.mkdir(parents=True)
            (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            os.symlink(cwd_dir, d / "cwd")
        mk("101", ["python3", "tools/soak-autofix/soak_ctl.py", "run", "--session-dir", "session"])
        mk("102", ["python3", "/x/soak_ctl.py", "runner-loop", "--session-dir", str(self.session)])
        mk("103", ["python3", "/x/soak_ctl.py", "set-goal", "--session-dir", str(self.session)])
        mk("104", ["python3", "/x/soak_ctl.py", "run", "--session-dir", str(self.root / "other")])
        mk("105", ["python3", "-m", "mcagent.loop", "--run-dir", str(self.session)])
        (proc / "self").mkdir()
        found = dash.find_soak_processes(self.session, proc_root=proc)
        self.assertEqual([(p["pid"], p["role"]) for p in found], [(101, "run"), (102, "runner-loop")])

    def test_apply_settings_bounds_conflict_and_write(self):
        with self.assertRaises(ValueError):
            self.console.apply_settings({"episode_seconds": 300})  # 会话未初始化
        self._init_state()
        for bad in ({"episode_seconds": 5}, {"episode_steps": 100000},
                    {"clear_budget": True, "episode_steps": 50}, {"max_goal_attempts": 0}):
            with self.assertRaises(ValueError, msg=bad):
                self.console.apply_settings(bad)
        res = self.console.apply_settings({"episode_seconds": "420", "episode_steps": "80",
                                           "target_goal": "smelt_iron", "player_model": " Qwen3.6-Flash "})
        self.assertEqual((res["episode_seconds"], res["episode_steps"]), (420.0, 80))
        self.assertEqual((res["target_goal"], res["player_model"]), ("smelt_iron", "Qwen3.6-Flash"))
        st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["episode_seconds"], st["episode_steps"]), (420.0, 80))
        # 空字符串表示不改该项；清除覆盖后回到启动参数
        res2 = self.console.apply_settings({"episode_seconds": "", "episode_steps": None, "clear_budget": True})
        self.assertIsNone(res2["episode_seconds"])
        self.assertEqual(res2["target_goal"], "smelt_iron")

    def test_apply_settings_hot_switches_fixer_model_and_snapshot_lists_catalog(self):
        self._init_state(fixer_preset="codex")
        with self.assertRaises(ValueError):
            self.console.apply_settings({"fixer_preset": "codex:gpt-9:max"})
        res = self.console.apply_settings({"fixer_preset": "agy:gemini-4-hydrogen-xhigh"})
        self.assertEqual(res["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        # 空串表示不改
        self.assertEqual(self.console.apply_settings({"fixer_preset": ""})["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")

        snap = self.console.snapshot()
        self.assertEqual(snap["state"]["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        groups = {c["group"] for c in snap["fixer_catalog"]}
        self.assertTrue({"Codex · GPT-6.2-Sol", "Codex · GPT-6.1-Sol", "Codex · GPT-6-Astra",
                         "agy · Gemini 4 Hydrogen", "agy · Gemini 4 Carbon"} <= groups)
        carbon = [c["key"] for c in snap["fixer_catalog"] if c["group"] == "agy · Gemini 4 Carbon"]
        self.assertEqual(carbon, ["agy:gemini-4-carbon-low", "agy:gemini-4-carbon-medium", "agy:gemini-4-carbon-high"])
        self.assertIn("DeepSeek-V4.1-Flash", [m["name"] for m in snap["player_models"]])
        p = self.console._validate_start({"fixer_preset": "codex:gpt-6-astra:max"})
        cmd = self.console.build_run_cmd(p)
        self.assertEqual(cmd[cmd.index("--fixer-preset") + 1], "codex:gpt-6-astra:max")

    def test_start_official_server_needs_account_ack_and_never_remembers_it(self):
        env_file = self.root / "code" / "mcagent" / ".env"
        env_file.parent.mkdir(parents=True)
        clean = {k: v for k, v in os.environ.items() if k not in (
            "MC_USER", "MC_PASS", "LLM_KEY", "LLM_CUSTOM_BASE", "LLM_CUSTOM_KEY", "MC_LOCAL_API_BASE"
        )}
        with patch.dict(os.environ, clean, clear=True), patch.object(self.console, "ledger", return_value=[]):
            with self.assertRaises(ValueError):  # 缺账号
                self.console._validate_start({"server": "official", "llm": False, "account_reserved": True})
            env_file.write_text("MC_USER=team\nMC_PASS=pw\n", encoding="utf-8")
            with self.assertRaises(ValueError):  # 没勾占位
                self.console._validate_start({"server": "official", "llm": False})
            with self.assertRaises(ValueError) as ctx:  # 比赛服 + 自备 key 训练（默认）但没配 LLM_CUSTOM_*
                self.console._validate_start({"server": "official", "llm": True, "account_reserved": True})
            self.assertIn("LLM_CUSTOM_KEY", str(ctx.exception))
            with self.assertRaises(ValueError) as ctx:  # 比赛服 + AIA 官方代理但没有 LLM_KEY
                self.console._validate_start({"server": "official", "llm": True, "account_reserved": True,
                                              "official_llm": "official"})
            self.assertIn("LLM_KEY", str(ctx.exception))
            p = self.console._validate_start({"server": "official", "llm": False, "account_reserved": True})
            cmd = self.console.build_run_cmd(p)
            self.assertEqual(cmd[cmd.index("--server") + 1], "official")
            self.assertEqual(cmd[cmd.index("--official-llm") + 1], "custom")
            self.assertIn("--account-reserved", cmd)
            local_cmd = self.console.build_run_cmd(self.console._validate_start({}))
            self.assertEqual(local_cmd[local_cmd.index("--server") + 1], "local")
            self.assertNotIn("--account-reserved", local_cmd)

            class FakePopen:
                def __init__(self, cmd, **kw):
                    self.pid = 7

                def poll(self):
                    return None

            with patch.object(dash, "find_soak_processes", return_value=[]), \
                 patch.object(dash.subprocess, "Popen", FakePopen):
                self.console.start({"server": "official", "llm": False, "account_reserved": True})
            saved = json.loads((self.session / dash.LAST_START_NAME).read_text(encoding="utf-8"))
            self.assertEqual((saved["server"], saved["account_reserved"]), ("official", False))
            self.assertFalse(self.console.last_start_params()["account_reserved"])
            snap_checks = self.console.snapshot()["official_checks"]
            self.assertEqual(snap_checks["rules_only"], [])
            self.assertTrue(any("LLM_CUSTOM_KEY" in m for m in snap_checks["custom"]))
            self.assertTrue(any("LLM_KEY" in m for m in snap_checks["official"]))
            env_file.write_text("MC_USER=team\nMC_PASS=pw\nLLM_CUSTOM_BASE=https://x/v1\nLLM_CUSTOM_KEY=k\n", encoding="utf-8")
            self.assertEqual(self.console.snapshot()["official_checks"]["custom"], [])

    def test_multi_goal_focus_settings_start_and_inject(self):
        self._init_state()
        ledger = [{"id": g, "stage": "draft"} for g in ("smelt_iron", "kill_a_mob", "plant_seed")]
        with patch.object(self.console, "ledger", return_value=ledger):
            res = self.console.apply_settings({"target_goal": ["smelt_iron", "kill_a_mob", "smelt_iron"],
                                               "focus_mode": "rotate"})
            self.assertEqual((res["target_goal"], res["focus_mode"]), ("smelt_iron,kill_a_mob", "rotate"))
            st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
            self.assertEqual((st["target_goal"], st["focus_mode"]), ("smelt_iron,kill_a_mob", "rotate"))
            with self.assertRaises(ValueError):
                self.console.apply_settings({"target_goal": ["smelt_iron", "no_such"]})
            with self.assertRaises(ValueError):
                self.console.apply_settings({"focus_mode": "sideways"})
            self.assertEqual(self.console.apply_settings({"target_goal": []})["target_goal"], "")

            p = self.console._validate_start({"goal": ["kill_a_mob", "plant_seed"], "focus_mode": "together"})
            cmd = self.console.build_run_cmd(p)
            self.assertEqual(cmd[cmd.index("--goal") + 1], "kill_a_mob,plant_seed")
            self.assertEqual(cmd[cmd.index("--focus-mode") + 1], "together")
            with self.assertRaises(ValueError):
                self.console._validate_start({"focus_mode": "sideways"})

            captured = {}

            def fake_job(kind, fn):
                captured["kind"] = kind
                captured["fn"] = fn
                return {"kind": kind, "status": "running"}

            with patch.object(self.console, "_run_job", side_effect=fake_job), \
                 patch.object(dash.soak_ctl, "inject_and_set_goal", return_value={"ok": True}) as mock_inj:
                self.console.inject("feat/goal-staging", ["plant_seed", "kill_a_mob"])
                captured["fn"]()
                self.assertEqual(mock_inj.call_args.kwargs["target_goal"], "plant_seed,kill_a_mob")
                self.console.inject("feat/goal-staging", None)
                captured["fn"]()
                self.assertIsNone(mock_inj.call_args.kwargs["target_goal"])
            with self.assertRaises(ValueError):
                self.console.inject("feat/goal-staging", ["nope"])

    def test_apply_settings_rejects_unknown_goal_when_ledger_known(self):
        self._init_state()
        with patch.object(self.console, "ledger", return_value=[{"id": "smelt_iron", "stage": "draft"}]):
            with self.assertRaises(ValueError):
                self.console.apply_settings({"target_goal": "no_such_goal"})
            self.assertEqual(self.console.apply_settings({"target_goal": "smelt_iron"})["target_goal"], "smelt_iron")

    def test_start_validation_command_and_duplicate_guard(self):
        with self.assertRaises(ValueError):
            self.console.start({"branch": "main"})
        with self.assertRaises(ValueError):
            self.console.start({"fixer_preset": "nope"})
        with self.assertRaises(ValueError):
            self.console.start({"episode_seconds": 10})

        p = self.console._validate_start({"episodes": 0, "episode_seconds": 600, "episode_steps": 110,
                                          "goal": "smelt_iron", "fixer_preset": "agy", "llm": False,
                                          "skip_live_canary": True})
        cmd = self.console.build_run_cmd(p)
        self.assertEqual(cmd[2], "run")
        for flag, val in (("--session-dir", str(self.session)), ("--episodes", "0"),
                          ("--episode-seconds", "600.0"), ("--episode-steps", "110"),
                          ("--goal", "smelt_iron"), ("--fixer-preset", "agy"), ("--branch", "feat/soak-autofix")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("--no-llm", cmd)
        self.assertIn("--skip-live-canary", cmd)
        self.assertEqual(cmd[cmd.index("--bot-user") + 1], dash.soak_ctl.DEFAULT_BOT_USER)
        self.assertNotIn("--rotate-user", cmd)
        cmd_rot = self.console.build_run_cmd(self.console._validate_start({"bot_user": "team_bot", "rotate_user": True}))
        self.assertEqual(cmd_rot[cmd_rot.index("--bot-user") + 1], "team_bot")
        self.assertIn("--rotate-user", cmd_rot)
        with self.assertRaises(ValueError):
            self.console._validate_start({"bot_user": "bad name"})

        with patch.object(dash, "find_soak_processes", return_value=[{"pid": 1, "role": "run"}]):
            with self.assertRaises(ValueError):
                self.console.start({})

        class FakePopen:
            def __init__(self, cmd, **kw):
                self.pid, self.cmd, self.kw = 4242, cmd, kw
            def poll(self):
                return None
        with patch.object(dash, "find_soak_processes", return_value=[]), \
             patch.object(self.console, "ledger", return_value=[]), \
             patch.object(dash.subprocess, "Popen", FakePopen):
            res = self.console.start({"episode_seconds": 300})
        self.assertEqual(res["pid"], 4242)
        self.assertTrue(self.console._child.kw["start_new_session"])
        self.assertIn("启动", (self.session / dash.RUN_LOG_NAME).read_text(encoding="utf-8"))
        saved = json.loads((self.session / dash.LAST_START_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["episode_seconds"], 300.0)
        self.assertEqual(self.console.last_start_params()["episode_seconds"], 300.0)

    def test_stop_and_episode_detail_guard(self):
        self.console.request_stop()
        self.assertTrue((self.session / "STOP").exists())
        for bad in ("../state", "ep-1", ""):
            with self.assertRaises(ValueError):
                self.console.episode_detail(bad)

    def test_http_host_origin_and_content_type_guards(self):
        self._init_state()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        httpd = dash.ThreadingHTTPServer(("127.0.0.1", port), dash.make_handler(self.console, port))
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            def req(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request(method, path, body=body, headers=headers or {})
                r = conn.getresponse()
                data = r.read()
                conn.close()
                return r.status, data

            code, data = req("GET", "/api/snapshot")
            self.assertEqual(code, 200)
            snap = json.loads(data)
            self.assertEqual(snap["daemon"], "stopped")
            self.assertEqual(snap["state"]["player_model"], "DeepSeek-V4.1-Flash")

            code, _ = req("GET", "/", headers={"Host": "evil.example:80"})
            self.assertEqual(code, 403)
            code, _ = req("POST", "/api/stop", body="{}",
                          headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
            self.assertEqual(code, 403)
            code, _ = req("POST", "/api/stop", body="x=1",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 415)
            self.assertFalse((self.session / "STOP").exists())
            code, data = req("POST", "/api/settings", body=json.dumps({"episode_steps": 9999}),
                             headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
            self.assertEqual(code, 400)
            self.assertFalse(json.loads(data)["ok"])
            code, _ = req("POST", "/api/stop", body="{}", headers={"Content-Type": "application/json"})
            self.assertEqual(code, 200)
            self.assertTrue((self.session / "STOP").exists())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_live_episode_env_blocked_budget_stopped_death_and_objective(self):
        ep = self.session / "episodes" / "ep-0002"
        rows = [
            {"event": "run_start", "t": 2000.0, "source": "official", "git": {"commit": "1234567"},
             "meta": {"use_llm": True, "llm_model": "default", "strategy": "llm", "min_verified": "offline",
                      "max_steps": 50, "max_seconds": 300.0, "bot_user": "team_bot", "target_goal": "smelt_iron"}},
            {"event": "objective_decision", "elapsed_s": 1.0, "trigger": "init", "source": "llm",
             "goal_id": "smelt_iron", "adopted": True, "cumulative_prompt_tokens": 300,
             "cumulative_completion_tokens": 40, "cumulative_total_tokens": 340},
            {"event": "skill_start", "elapsed_s": 1.2, "skill": "smelt_iron", "args": {"count": 1}, "source": "rule"},
            {"event": "skill_end", "elapsed_s": 4.0, "skill": "smelt_iron", "outcome": "env_blocked",
             "environment": {"reason": "no_ore", "message": "半径 32 内无暴露铁矿", "wake_on": "chunk_moved"}},
            {"event": "death_recovered", "elapsed_s": 8.0, "deaths_total": 1, "cause": "fell", "respawn_ms": 500},
            {"event": "skill_start", "elapsed_s": 9.0, "skill": "scout_island", "args": {}, "source": "rule"},
            {"event": "skill_end", "elapsed_s": 10.0, "skill": "scout_island", "outcome": "budget_stopped"},
            {"event": "run_end", "elapsed_s": 10.0, "stop_reason": "max_seconds_reached", "source": "official",
             "environment_blocked_goals": {"smelt_iron": {"message": "缺铁矿", "wake_on": "chunk_moved"}},
             "decision_sources": {"rule": 2}, "prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
        ]
        _write_jsonl(ep / "timeline.jsonl", rows)
        live = dash.summarize_live_episode(ep, now=2020.0)
        self.assertEqual((live["source"], live["strategy"], live["min_verified"]), ("official", "llm", "offline"))
        self.assertEqual(live["current_objective"], "smelt_iron")
        self.assertEqual((live["skills_completed"], live["skills_env_blocked"], live["skills_budget_stopped"], live["skills_failed"]),
                         (0, 1, 1, 0))
        self.assertEqual((live["prompt_tokens"], live["completion_tokens"], live["tokens"]), (300, 40, 340))
        self.assertIn("smelt_iron", live["environment_blocked_goals"])
        texts = "\n".join(x["text"] for x in live["recent"])
        self.assertIn("环境挂起", texts)
        self.assertIn("chunk_moved", texts)
        self.assertIn("死亡重生 #1", texts)
        self.assertIn("战略目标 smelt_iron", texts)
        kinds = [x["kind"] for x in live["recent"]]
        self.assertIn("env_blocked", kinds)
        self.assertIn("budget_stopped", kinds)
        self.assertNotIn("skill_fail", kinds)

    def test_multi_source_telemetry_and_skills_ast_scan(self):
        (self.session / "goal_telemetry.local-target.json").write_text(json.dumps({
            "source": "local-target",
            "goals": {"prep_wood": {"episodes": 4, "hits": 3, "hit_rate": 0.75, "p50_elapsed_s": 25.0,
                                    "p50_tokens": 1000, "total_deaths": 0}},
        }), encoding="utf-8")
        (self.session / "goal_telemetry.official.json").write_text(json.dumps({
            "source": "official",
            "goals": {"prep_wood": {"episodes": 2, "hits": 2, "hit_rate": 1.0, "p50_elapsed_s": 40.0,
                                    "p50_tokens": 1500, "total_deaths": 1}},
        }), encoding="utf-8")
        self._init_state(server_target="official")
        snap = self.console.snapshot()
        rows = {(r["source"], r["goal_id"]): r for r in snap["telemetry_rows"]}
        self.assertIn(("local-target", "prep_wood"), rows)
        self.assertIn(("official", "prep_wood"), rows)
        self.assertEqual(rows[("official", "prep_wood")]["p50_elapsed_s"], 40.0)
        self.assertEqual(snap["telemetry"]["prep_wood"]["p50_elapsed_s"], 40.0)

        sk = dash.parse_skills_from_sources({
            "code/mcagent/loop/starter_skills.py": (
                "@register\nclass A(Skill):\n    name = 'a'\n\n"
                "@register\nclass B(Skill):\n    name = 'b'\n    verified = 'live'\n"
            ),
        })
        self.assertEqual((sk["total"], sk["live"], sk["offline"]), (2, 1, 1))
        real_sk = dash.parse_skills_from_sources({
            "code/mcagent/loop/starter_skills.py": (
                REPO_ROOT / "code" / "mcagent" / "loop" / "starter_skills.py"
            ).read_text(encoding="utf-8"),
        })
        self.assertGreaterEqual(real_sk["total"], 5)

    def test_official_endpoint_min_verified_incident_detail_and_session_guard(self):
        self._init_state()
        with patch.object(self.console, "ledger", return_value=[]):
            with self.assertRaises(ValueError):
                self.console._validate_start({"official_api_base": "https://evil.example/api/v1"})
            with self.assertRaises(ValueError):
                self.console._validate_start({"min_verified": "magic"})
            p = self.console._validate_start({
                "official_api_base": dash.soak_ctl.CAMPUS_MC_BASE,
                "min_verified": "offline",
            })
            cmd = self.console.build_run_cmd(p)
            self.assertEqual(cmd[cmd.index("--min-verified") + 1], "offline")
            res = self.console.apply_settings({"min_verified": "live"})
            self.assertEqual(res["min_verified"], "live")
            self.assertEqual(self.console.apply_settings({"min_verified": ""})["min_verified"], "")

        inc_file = self.session / "episodes" / "ep-0001" / "incidents" / "INC-001.json"
        inc_file.parent.mkdir(parents=True)
        inc_file.write_text(json.dumps({"title": "缺铁矿", "detail": {"goal_id": "smelt_iron"}, "http_tape": [1, 2]}),
                            encoding="utf-8")
        prompt_file = self.session / "fixer_runs" / "attempt-0001" / "prompt.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("# prompt body", encoding="utf-8")
        with dash.soak_ctl.locked_state(self.session) as st:
            st["incidents"]["GOAL_MISS:smelt_iron"] = {
                "incident_id": "INC-001", "fingerprint": "GOAL_MISS:smelt_iron", "code": "GOAL_MISS",
                "severity": "P1", "category": "strategy", "skill": "smelt_iron", "goal_id": "smelt_iron",
                "status": "pending", "attempts": 1, "incident_path": str(inc_file),
                "prompt_file": str(prompt_file), "gate_history": [{"ok": False, "failed_gate": "gate2_unittest"}],
            }
        det = self.console.incident_detail("GOAL_MISS:smelt_iron")
        self.assertEqual(det["title"], "缺铁矿")
        self.assertEqual(det["http_tape_count"], 2)
        self.assertIn("# prompt body", det["prompt_preview"])
        with self.assertRaises(ValueError):
            self.console.incident_detail("no_such")
        with self.assertRaises(ValueError):
            self.console.switch_session("/etc")

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        httpd = dash.ThreadingHTTPServer(("127.0.0.1", port), dash.make_handler(self.console, port))
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/incident?fp=GOAL_MISS%3Asmelt_iron")
            r1 = conn.getresponse()
            self.assertEqual(r1.status, 200)
            self.assertEqual(json.loads(r1.read())["record"]["incident_id"], "INC-001")
            with patch.object(dash, "_probe_local_bridge", return_value={"reachable": False, "url": "x"}):
                conn.request("GET", "/api/probe_env")
                r2 = conn.getresponse()
                self.assertEqual(r2.status, 200)
                self.assertIn("preflight_checks", json.loads(r2.read()))
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_bridge_env_target_and_snapshot_probe(self):
        self._init_state()
        bridge_dir = self.root / "tools" / "local-api-bridge"
        bridge_dir.mkdir(parents=True)
        (bridge_dir / ".env.example").write_text("MC_HOST=frp-net.com\nMC_PORT=64463\n", encoding="utf-8")
        (bridge_dir / ".env").write_text("BRIDGE_HOST=127.0.0.1\nBRIDGE_PORT=8787\nMC_HOST=10.6.17.10\nMC_PORT=25565\nMC_VERSION=1.21.11\n", encoding="utf-8")
        be = dash._read_bridge_env_target(self.root)
        self.assertEqual(be["mc_target"], "10.6.17.10:25565")
        self.assertEqual(be["source_file"], ".env")
        with patch.object(dash, "_probe_local_bridge",
                          return_value={"reachable": True, "url": "http://127.0.0.1:8787/healthz",
                                        "mc_target": "10.6.17.10:25565", "mc_version": "1.21.11", "active_bots": []}):
            snap = self.console.snapshot()
        self.assertTrue(snap["bridge"]["reachable"])
        self.assertEqual(snap["bridge"]["mc_target"], "10.6.17.10:25565")
        self.assertEqual(snap["bridge_env"]["mc_target"], "10.6.17.10:25565")

    def test_dashboard_html_script_ids_match_dom(self):
        import re
        html = (REPO_ROOT / "tools" / "soak-autofix" / "dashboard.html").read_text(encoding="utf-8")
        head_html, js_tail = html.split("<script>", 1)
        dom_ids = set(re.findall(r'id="([^"]+)"', head_html))
        used_ids = set(re.findall(r'\$\("([^"]+)"\)', js_tail))
        self.assertEqual(used_ids - dom_ids, set())
        self.assertIn("btnResetEscalated", dom_ids)
        self.assertIn("workersIn", dom_ids)
        self.assertIn("sWorkers", dom_ids)
        self.assertIn("focus_summary", js_tail)

    def test_fixer_workers_settings_validation_and_run_cmd(self):
        self._init_state(fixer_preset="codex", fixer_workers=3)
        snap = self.console.snapshot()
        self.assertEqual(snap["state"]["fixer_workers"], 3)
        self.assertEqual(snap["start_defaults"]["fixer_workers"], 3)
        self.assertEqual(snap["bounds"]["fixer_workers"], [1, 6])

        res = self.console.apply_settings({"fixer_workers": "4"})
        self.assertEqual(res["fixer_workers"], 4)
        st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["fixer_workers"], 4)

        for bad in ({"fixer_workers": 0}, {"fixer_workers": 7}, {"fixer_workers": "abc"}):
            with self.assertRaises(ValueError, msg=bad):
                self.console.apply_settings(bad)
            with self.assertRaises(ValueError, msg=bad):
                self.console._validate_start(bad)

        p = self.console._validate_start({"fixer_workers": 3})
        cmd = self.console.build_run_cmd(p)
        self.assertEqual(cmd[cmd.index("--fixer-workers") + 1], "3")

    def test_snapshot_exposes_focus_summary_goal_statuses_and_reset_goals_endpoint(self):
        self._init_state(
            target_goal="prep_wood,smelt_iron,kill_a_mob",
            focus_mode="together",
            episodes=[{
                "ep_id": "ep-0001",
                "at": "2026-11-16T20:00:00Z",
                "commit": "b" * 40,
                "source": "local-target",
                "elapsed_s": 4.5,
                "stop_reason": "goal_reached",
                "steps": 0,
                "skills_completed": 0,
                "target_goal": "prep_wood,kill_a_mob",
                "configured_target_goal": "prep_wood,smelt_iron,kill_a_mob",
                "skipped_goals": ["smelt_iron"],
                "goals_reached": {"prep_wood": 3.0},
            }],
            incidents={
                "GOAL_MISS:smelt_iron": {
                    "incident_id": "INC-012",
                    "fingerprint": "GOAL_MISS:smelt_iron",
                    "code": "GOAL_MISS",
                    "goal_id": "smelt_iron",
                    "skill": "smelt_iron",
                    "status": "escalated_human",
                    "attempts": 5,
                    "fix_commit": None,
                    "gate_history": [{"ok": True}],
                },
                "GOAL_MISS:kill_a_mob": {
                    "incident_id": "INC-016",
                    "fingerprint": "GOAL_MISS:kill_a_mob",
                    "code": "GOAL_MISS",
                    "goal_id": "kill_a_mob",
                    "skill": "hunt_mob",
                    "status": "pending",
                    "attempts": 1,
                },
            },
        )
        snap = self.console.snapshot()
        fs = snap["focus_summary"]
        self.assertEqual(fs["selected"], ["prep_wood", "smelt_iron", "kill_a_mob"])
        self.assertEqual(fs["escalated"], ["smelt_iron"])
        self.assertEqual(fs["active_pool"], ["prep_wood", "kill_a_mob"])
        self.assertEqual(fs["next_round"], ["prep_wood", "kill_a_mob"])
        self.assertEqual(fs["goal_statuses"]["smelt_iron"]["status"], "escalated_human")
        self.assertEqual(fs["goal_statuses"]["kill_a_mob"]["status"], "pending")
        self.assertEqual(snap["episodes"][0]["skipped_goals"], ["smelt_iron"])
        self.assertEqual(snap["episodes"][0]["configured_target_goal"], "prep_wood,smelt_iron,kill_a_mob")
        self.assertEqual(snap["episodes"][0]["steps"], 0)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        httpd = dash.ThreadingHTTPServer(("127.0.0.1", port), dash.make_handler(self.console, port))
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/reset_goals", body=json.dumps({"goals": ["smelt_iron"]}),
                         headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["result"]["reset"]["reset_goals"], ["smelt_iron"])
            self.assertEqual(body["result"]["snapshot"]["focus_summary"]["escalated"], [])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
