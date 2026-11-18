"""Task #39 离线单元测试：TapeRecorder.path_stats、SoakObserver 局剖析与主动异常检测（verified: offline）。

全部用例断网可跑、零真实 HTTP 请求，覆盖：
1. `TapeRecorder.path_stats()`：多线程并发计数精度、`auth/*` 路径忽略与脱敏/环形缓冲兼容性、只读快照隔离；
2. `SoakObserver.build_episode_profile()`：技能耗时、参数频次、`repeated_skill_args`、决策历史与 `recent_decisions`、
   `http_stats`（含 `pct_of_elapsed`）、`skill_stats` 只读复制扩展与全程 zero-RTT；
3. 主动异常检测精确触发与边界防误报：
   - `DISPATCH_STARVATION` 分支 1（目标技能 0 启动）与分支 2（LLM 高回退率）及停机原因排除；
   - `SKILL_SPAM_LOOP`（`craft_gear` / `gather_wood` / `scout_island` 阈值与 `P1`/`P2` 升降级及未达成目标守卫）；
   - `API_BOTTLENECK_SLOW`（时长 >= 60s、端点 >= 20,000ms 且占比 >= 30%，及三项负向边界）；
4. 短局 `upgrade_tools` 正常通关零误报；
5. `GOAL_MISS` `INC-001` 编号优先级、全量 `INC-*.json` 携带 `episode_profile` + `run_dir` 与指纹刷新、
   `http_tape` 离线确定性重放及 `summary.md` 第 5 节渲染保留 `## 一` ~ `## 四`。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mcagent.client import Client, Config
from mcagent.loop import (
    CampaignPlanner,
    GoalSpec,
    LoopConfig,
    MainLoop,
    SoakObserver,
    TapeClient,
    TapeRecorder,
    replay_incident,
    reset_world_memo,
)
from mcagent.runtime import EnvBlocked, Executor, State
from mcagent.skills import REGISTRY, Skill, make, rec, register
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


def _make_ready_state(items: dict[str, int] | None = None) -> State:
    """构造已就绪的离线 State 夹具，不触发任何网络请求。"""
    state = State()
    state.set_status({
        "ready": True,
        "alive": True,
        "health": 20,
        "food": 20,
        "pos": [0.5, 64.0, 0.5],
        "dimension": "overworld",
        "is_day": True,
    })
    _set_inventory_counts(state, items or {})
    return state


def _set_inventory_counts(state: State, items: dict[str, int]) -> None:
    rows = [
        {"name": name, "count": count, "slot": idx}
        for idx, (name, count) in enumerate(items.items())
        if count > 0
    ]
    state.set_inventory({"items": rows})


def _bump_item(state: State, item: str, delta: int = 1) -> None:
    counts: dict[str, int] = {}
    for row in state.inventory.get("items") or []:
        name = str(row.get("name", ""))
        if name:
            counts[name] = counts.get(name, 0) + int(row.get("count", 0))
    counts[item] = counts.get(item, 0) + delta
    _set_inventory_counts(state, counts)


def _make_stub_skill(skill_name: str, goal_id: str, args: dict | None = None) -> Skill:
    """构造局部技能桩，用于测试未在基础包注册的台账技能（如 smelt_iron）。"""

    class _StubSkill(Skill):
        name = skill_name
        goal = goal_id
        params = {}

        def run(self, ex, state):
            return iter(())

    sk = _StubSkill()
    if args is not None:
        sk.args = dict(args)
    return sk


def _run_skill_cycle(
    obs: SoakObserver,
    skill: Skill,
    state: State,
    *,
    source: str = "rule",
    reason: str = "test",
    outcome: str = "completed",
    error_code: str = "",
    error_msg: str = "",
    elapsed_delta_s: float = 0.0,
    gain_item: str | None = None,
) -> None:
    """驱动一次零网络技能生命周期钩子。"""
    obs.on_skill_start(skill, source, reason, state)
    if elapsed_delta_s > 0.0:
        obs.started_mono -= elapsed_delta_s
    if gain_item:
        _bump_item(state, gain_item, 1)
    obs.on_skill_end(
        skill,
        outcome,
        state,
        error_code=error_code,
        error_msg=error_msg,
        died_or_disconnected=False,
    )


def _read_incidents(run_dir: Path) -> list[dict]:
    files = sorted((run_dir / "incidents").glob("INC-*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


class TapeRecorderPathStatsTests(unittest.TestCase):
    def test_multithreaded_record_aggregates_exact_path_stats(self):
        recorder = TapeRecorder(ring_size=500)
        threads_spec = [
            ("main", 0),
            ("main", 1),
            ("safety-monitor-1", 2),
            ("safety-monitor-2", 3),
        ]
        iters_per_thread = 40
        barrier = threading.Barrier(len(threads_spec))
        errors: list[BaseException] = []

        def worker(role: str, tid: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                for i in range(iters_per_thread):
                    ok_find = (i % 4) != 0
                    ms_find = 10.0 + tid + (i % 5)
                    recorder.record(
                        method="POST",
                        path="/find_blocks" if i % 2 == 0 else "find_blocks",
                        body={"radius": 16},
                        ms=ms_find,
                        ok=ok_find,
                        thread="monitor" if "monitor" in role else "main",
                        result={"total": 1} if ok_find else None,
                        error=None if ok_find else {"code": "timeout"},
                    )
                    recorder.record(
                        method="GET",
                        path="/status",
                        body=None,
                        ms=2.0 + (i % 3),
                        ok=True,
                        thread="monitor" if "monitor" in role else "main",
                        result={"ready": True},
                    )
            except BaseException as exc:  # pragma: no cover - 仅失败收集
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(role, tid)) for role, tid in threads_spec]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10.0)
        self.assertEqual(errors, [])

        expected_find_count = len(threads_spec) * iters_per_thread
        expected_find_errors = len(threads_spec) * (iters_per_thread // 4)
        expected_find_ok = expected_find_count - expected_find_errors
        expected_find_total = sum(
            10.0 + tid + (i % 5)
            for _, tid in threads_spec
            for i in range(iters_per_thread)
        )
        expected_find_max = max(
            10.0 + tid + (i % 5)
            for _, tid in threads_spec
            for i in range(iters_per_thread)
        )

        expected_status_count = len(threads_spec) * iters_per_thread
        expected_status_total = len(threads_spec) * sum(2.0 + (i % 3) for i in range(iters_per_thread))

        stats = recorder.path_stats()
        self.assertEqual(set(stats.keys()), {"find_blocks", "status"})

        fb = stats["find_blocks"]
        self.assertEqual(fb["count"], expected_find_count)
        self.assertEqual(fb["ok"], expected_find_ok)
        self.assertEqual(fb["errors"], expected_find_errors)
        self.assertAlmostEqual(fb["total_ms"], round(expected_find_total, 1), places=1)
        self.assertAlmostEqual(fb["avg_ms"], round(expected_find_total / expected_find_count, 1), places=1)
        self.assertAlmostEqual(fb["max_ms"], round(expected_find_max, 1), places=1)

        st = stats["status"]
        self.assertEqual(st["count"], expected_status_count)
        self.assertEqual(st["ok"], expected_status_count)
        self.assertEqual(st["errors"], 0)
        self.assertAlmostEqual(st["total_ms"], round(expected_status_total, 1), places=1)
        self.assertAlmostEqual(st["avg_ms"], round(expected_status_total / expected_status_count, 1), places=1)
        self.assertAlmostEqual(st["max_ms"], 4.0, places=1)

    def test_auth_paths_ignored_without_altering_ring_or_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tape_path = Path(tmp) / "http_tape.jsonl"
            recorder = TapeRecorder(tape_path=str(tape_path), ring_size=2)

            r1 = recorder.record(
                method="POST",
                path="/auth/login",
                body={"username": "u1", "password": "SECRET_PASSWORD"},
                ms=120.0,
                ok=True,
                thread="main",
                result={"token": "SECRET_TOKEN"},
            )
            r2 = recorder.record(
                method="POST",
                path="auth/refresh",
                body={"token": "SECRET_REFRESH"},
                ms=45.0,
                ok=False,
                thread="main",
                error={"code": "unauthorized"},
            )
            self.assertEqual(r1, {})
            self.assertEqual(r2, {})
            self.assertEqual(recorder.path_stats(), {})
            self.assertEqual(recorder.recent(10), [])
            self.assertFalse(tape_path.exists())

            # 非 auth 路径仍保持环形缓冲区容量限制（ring_size=2）与敏感字段脱敏
            for idx, p in enumerate(["/status", "/inventory", "/craft"]):
                recorder.record(
                    method="POST",
                    path=p,
                    body={"idx": idx, "password": "RAW_PASSWORD", "token": "RAW_TOKEN"},
                    ms=10.0 + idx,
                    ok=True,
                    thread="main",
                    result={"api_key": "RAW_API_KEY", "ok": True},
                )

            stats = recorder.path_stats()
            self.assertEqual(set(stats.keys()), {"status", "inventory", "craft"})
            self.assertNotIn("auth/login", stats)
            self.assertNotIn("auth/refresh", stats)

            recent = recorder.recent(10)
            self.assertEqual([e["path"] for e in recent], ["inventory", "craft"])
            for entry in recent:
                self.assertEqual(entry["body"]["password"], "<redacted>")
                self.assertEqual(entry["body"]["token"], "<redacted>")
                self.assertEqual(entry["result"]["api_key"], "<redacted>")

            raw_text = tape_path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_PASSWORD", raw_text)
            self.assertNotIn("SECRET_TOKEN", raw_text)
            self.assertNotIn("RAW_PASSWORD", raw_text)
            self.assertNotIn("RAW_TOKEN", raw_text)
            self.assertNotIn("RAW_API_KEY", raw_text)

    def test_path_stats_returns_readonly_snapshot(self):
        recorder = TapeRecorder()
        recorder.record(method="POST", path="/dig", body={}, ms=55.5, ok=True, thread="main", result={})
        snap = recorder.path_stats()
        snap["dig"]["count"] = 999
        snap["dig"]["total_ms"] = 1.0
        snap["tampered"] = {"count": 42}

        fresh = recorder.path_stats()
        self.assertEqual(set(fresh.keys()), {"dig"})
        self.assertEqual(fresh["dig"]["count"], 1)
        self.assertEqual(fresh["dig"]["total_ms"], 55.5)


class SoakObserverProfileTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_episode_profile_accumulates_stats_caps_and_top_k_without_polluting_skill_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            obs = SoakObserver(tmp, tape=recorder, target_goal="smelt_iron")
            state = _make_ready_state()

            # 36 次技能调用 (>30 上限)，覆盖 9 个 >=3 次的参数组合与 1 个 2 次的参数组合
            plan_batches = [
                ("gather_wood", {"count": 1}, 6, "oak_log"),
                ("gather_wood", {"count": 2}, 5, "oak_log"),
                ("gather_wood", {"count": 3}, 4, "oak_log"),
                ("gather_wood", {"count": 4}, 4, "oak_log"),
                ("mine_stone", {"count": 1}, 3, "cobblestone"),
                ("mine_stone", {"count": 2}, 3, "cobblestone"),
                ("mine_stone", {"count": 3}, 3, "cobblestone"),
                ("mine_stone", {"count": 4}, 3, "cobblestone"),
                ("scout_island", {"radius": 16}, 3, None),
                ("scout_island", {"radius": 20}, 2, None),
            ]
            # 预先持有木镐以便 mine_stone 不受限（on_skill_start/end 不查 can_start）
            for sname, sargs, n_calls, gain in plan_batches:
                for _ in range(n_calls):
                    _run_skill_cycle(
                        obs,
                        make(sname, sargs),
                        state,
                        elapsed_delta_s=0.5,
                        gain_item=gain,
                    )

            # 记录 10 个不同 HTTP 端点，验证 http_stats 按 -total_ms 排序且最多 8 条
            for idx in range(1, 11):
                recorder.record(
                    method="POST",
                    path=f"/ep_{idx:02d}",
                    body={},
                    ms=idx * 100.0,
                    ok=(idx % 3 != 0),
                    thread="main",
                )

            # _decision_history 上限 30
            self.assertEqual(len(obs._decision_history), 30)
            self.assertAlmostEqual(obs.skill_elapsed_s["gather_wood"], 19 * 0.5, places=1)
            self.assertAlmostEqual(obs.skill_elapsed_s["mine_stone"], 12 * 0.5, places=1)
            self.assertAlmostEqual(obs.skill_elapsed_s["scout_island"], 5 * 0.5, places=1)
            self.assertGreaterEqual(len(obs.skill_arg_stats), 10)

            profile = obs.build_episode_profile(state)

            # recent_decisions 取最近 15 条
            recent = profile["recent_decisions"]
            self.assertEqual(len(recent), 15)
            self.assertEqual(recent, list(obs._decision_history)[-15:])

            # repeated_skill_args：仅 started >= 3，按 -started 排序最多 8 条
            rep = profile["repeated_skill_args"]
            self.assertEqual(len(rep), 8)
            self.assertTrue(all(item["started"] >= 3 for item in rep))
            rep_starts = [item["started"] for item in rep]
            self.assertEqual(rep_starts, sorted(rep_starts, reverse=True))
            self.assertEqual(rep_starts, [6, 5, 4, 4, 3, 3, 3, 3])
            self.assertFalse(
                any(item.get("skill") == "scout_island" and item.get("args") == {"radius": 20} for item in rep)
            )

            # http_stats：带 pct_of_elapsed，按 -total_ms 排序最多 8 条
            http_stats = profile["http_stats"]
            self.assertEqual(len(http_stats), 8)
            totals = [row["total_ms"] for row in http_stats]
            self.assertEqual(totals, sorted(totals, reverse=True))
            self.assertEqual([row["path"] for row in http_stats], [f"ep_{i:02d}" for i in range(10, 2, -1)])
            for row in http_stats:
                self.assertIn("pct_of_elapsed", row)
                self.assertGreater(row["pct_of_elapsed"], 0.0)

            # profile.skill_stats 含 elapsed_s，且 obs.skill_stats 原字典保持 5 个既有字段不受污染
            expected_base_keys = {"started", "ok", "replan", "budget_stopped", "bug"}
            for sname in ("gather_wood", "mine_stone", "scout_island"):
                self.assertEqual(set(obs.skill_stats[sname].keys()), expected_base_keys)
                self.assertNotIn("elapsed_s", obs.skill_stats[sname])
                self.assertIn("elapsed_s", profile["skill_stats"][sname])
                self.assertEqual(
                    set(profile["skill_stats"][sname].keys()),
                    expected_base_keys | {"elapsed_s"},
                )
                self.assertAlmostEqual(
                    profile["skill_stats"][sname]["elapsed_s"],
                    obs.skill_elapsed_s[sname],
                    places=1,
                )

    def test_build_episode_profile_is_strictly_zero_rtt(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            recorder.record(method="POST", path="/status", body={}, ms=12.0, ok=True, thread="main")
            obs = SoakObserver(tmp, tape=recorder, target_goal="upgrade_tools")
            state = _make_ready_state({"stone_pickaxe": 1})

            with patch.object(
                Client, "_send", side_effect=AssertionError("build_episode_profile 不得发起任何 HTTP 调用")
            ), patch(
                "urllib.request.urlopen", side_effect=AssertionError("build_episode_profile 不得打开任何网络连接")
            ):
                profile = obs.build_episode_profile(state)

            self.assertIsInstance(profile, dict)
            self.assertIn("http_stats", profile)
            self.assertIn("skill_stats", profile)


class ProactiveAnomalyDetectionTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def _assert_proactive_fields(self, inc: dict, obs: SoakObserver, *, expected_severity: str) -> None:
        self.assertIs(inc.get("proactive"), True)
        self.assertEqual(inc.get("editable_files"), [])
        self.assertEqual(inc.get("category"), "strategy")
        self.assertEqual(inc.get("severity"), expected_severity)
        self.assertIn("episode_profile", inc)
        self.assertIsInstance(inc["episode_profile"], dict)
        self.assertEqual(inc.get("run_dir"), str(obs.run_dir))

    def test_dispatch_starvation_branch_1_starved_target_threshold(self):
        # 边界 1：total_starts == 9 时不触发 DISPATCH_STARVATION
        with tempfile.TemporaryDirectory() as tmp_low:
            obs_low = SoakObserver(tmp_low, target_goal="smelt_iron")
            state_low = _make_ready_state()
            for _ in range(6):
                _run_skill_cycle(obs_low, make("gather_wood", {"count": 3}), state_low, gain_item="oak_log")
            for _ in range(3):
                _run_skill_cycle(obs_low, make("mine_stone", {"count": 3}), state_low, gain_item="cobblestone")
            obs_low.on_run_end(state_low, stop_reason="max_steps")

            incs_low = _read_incidents(Path(tmp_low))
            self.assertFalse(any(i["code"] == "DISPATCH_STARVATION" for i in incs_low), incs_low)
            self.assertTrue(any(i["fingerprint"] == "GOAL_MISS:smelt_iron" for i in incs_low))

        # 边界 2：total_starts == 10 且 smelt_iron 0 次 -> 触发 DISPATCH_STARVATION:smelt_iron
        with tempfile.TemporaryDirectory() as tmp_hit:
            obs_hit = SoakObserver(tmp_hit, target_goal="smelt_iron")
            state_hit = _make_ready_state()
            for _ in range(7):
                _run_skill_cycle(obs_hit, make("gather_wood", {"count": 3}), state_hit, gain_item="oak_log")
            for _ in range(3):
                _run_skill_cycle(obs_hit, make("mine_stone", {"count": 3}), state_hit, gain_item="cobblestone")
            obs_hit.on_run_end(state_hit, stop_reason="max_steps")

            incs_hit = _read_incidents(Path(tmp_hit))
            starv = [i for i in incs_hit if i["code"] == "DISPATCH_STARVATION"]
            self.assertEqual(len(starv), 1, incs_hit)
            self.assertEqual(starv[0]["fingerprint"], "DISPATCH_STARVATION:smelt_iron")
            self._assert_proactive_fields(starv[0], obs_hit, expected_severity="P1")

    def test_dispatch_starvation_branch_2_llm_fallback_rate_and_stop_reason_exclusions(self):
        def _prepare_branch_2(obs: SoakObserver, state: State) -> None:
            # 启动过 smelt_iron（>=1次），排除分支 1，纯验证分支 2（llm_fallbacks>=5 且回退率>=0.35）
            for _ in range(2):
                _run_skill_cycle(obs, _make_stub_skill("smelt_iron", "iron:smelt"), state)
            for _ in range(8):
                _run_skill_cycle(obs, make("gather_wood", {"count": 3}), state, gain_item="oak_log")
            for _ in range(6):
                obs.on_llm_call({"ok": False, "ms": 120.0, "errors": ["invalid json"]})
            for _ in range(4):
                obs.on_llm_call({"ok": True, "ms": 80.0})

        with tempfile.TemporaryDirectory() as tmp_hit:
            obs = SoakObserver(tmp_hit, target_goal="smelt_iron")
            state = _make_ready_state()
            _prepare_branch_2(obs, state)
            obs.on_run_end(state, stop_reason="max_steps")

            incs = _read_incidents(Path(tmp_hit))
            starv = [i for i in incs if i["code"] == "DISPATCH_STARVATION"]
            self.assertEqual(len(starv), 1, incs)
            self.assertEqual(starv[0]["fingerprint"], "DISPATCH_STARVATION:smelt_iron")
            self._assert_proactive_fields(starv[0], obs, expected_severity="P1")

        # keyboard_interrupt 与 transient_outage 停机均不得触发 DISPATCH_STARVATION
        for excluded_reason in ("keyboard_interrupt", "transient_outage"):
            with tempfile.TemporaryDirectory() as tmp_excl:
                obs_excl = SoakObserver(tmp_excl, target_goal="smelt_iron")
                state_excl = _make_ready_state()
                _prepare_branch_2(obs_excl, state_excl)
                obs_excl.on_run_end(state_excl, stop_reason=excluded_reason)

                incs_excl = _read_incidents(Path(tmp_excl))
                self.assertFalse(
                    any(i["code"] == "DISPATCH_STARVATION" for i in incs_excl),
                    f"{excluded_reason} 不应触发 DISPATCH_STARVATION",
                )

    def test_skill_spam_loop_craft_gear_thresholds_and_severity(self):
        # 3 次不触发
        with tempfile.TemporaryDirectory() as tmp_3:
            obs3 = SoakObserver(tmp_3, target_goal="upgrade_tools")
            state3 = _make_ready_state()
            for _ in range(3):
                _run_skill_cycle(
                    obs3, make("craft_gear", {"item": "wooden_pickaxe"}), state3, gain_item="wooden_pickaxe"
                )
            obs3.on_run_end(state3, stop_reason="max_steps")
            incs3 = _read_incidents(Path(tmp_3))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs3), incs3)

        # 4 次 + 存在未达成目标 -> 触发 P1，指纹 SKILL_SPAM_LOOP:craft_gear:<6位sha1>
        with tempfile.TemporaryDirectory() as tmp_p1:
            obs_p1 = SoakObserver(tmp_p1, target_goal="upgrade_tools")
            state_p1 = _make_ready_state()
            for _ in range(4):
                _run_skill_cycle(
                    obs_p1, make("craft_gear", {"item": "wooden_pickaxe"}), state_p1, gain_item="wooden_pickaxe"
                )
            obs_p1.on_run_end(state_p1, stop_reason="max_steps")
            incs_p1 = _read_incidents(Path(tmp_p1))
            spam_p1 = [i for i in incs_p1 if i["code"] == "SKILL_SPAM_LOOP"]
            self.assertEqual(len(spam_p1), 1, incs_p1)
            self.assertRegex(spam_p1[0]["fingerprint"], r"^SKILL_SPAM_LOOP:craft_gear:[0-9a-f]{6}$")
            self._assert_proactive_fields(spam_p1[0], obs_p1, expected_severity="P1")

        # 4 次 + 无未达成目标 -> 触发 P2
        with tempfile.TemporaryDirectory() as tmp_p2:
            obs_p2 = SoakObserver(tmp_p2, target_goal=None)
            state_p2 = _make_ready_state()
            for _ in range(4):
                _run_skill_cycle(
                    obs_p2, make("craft_gear", {"item": "wooden_pickaxe"}), state_p2, gain_item="wooden_pickaxe"
                )
            obs_p2.on_run_end(state_p2, stop_reason="max_steps")
            incs_p2 = _read_incidents(Path(tmp_p2))
            spam_p2 = [i for i in incs_p2 if i["code"] == "SKILL_SPAM_LOOP"]
            self.assertEqual(len(spam_p2), 1, incs_p2)
            self.assertRegex(spam_p2[0]["fingerprint"], r"^SKILL_SPAM_LOOP:craft_gear:[0-9a-f]{6}$")
            self._assert_proactive_fields(spam_p2[0], obs_p2, expected_severity="P2")

        # 3 次重试/挂起 + 终于成功 1 次（ok == 1 < 2）：属于正常重试恢复而非刷屏，不误报 SKILL_SPAM_LOOP
        with tempfile.TemporaryDirectory() as tmp_retry:
            obs_retry = SoakObserver(tmp_retry, target_goal="upgrade_tools")
            state_retry = _make_ready_state()
            for _ in range(2):
                _run_skill_cycle(
                    obs_retry,
                    make("craft_gear", {"item": "stone_pickaxe"}),
                    state_retry,
                    outcome="replan",
                    error_code="path_not_found",
                )
            obs_retry.on_skill_start(make("craft_gear", {"item": "stone_pickaxe"}), "rule", "busy", state_retry)
            obs_retry.on_skill_end(
                make("craft_gear", {"item": "stone_pickaxe"}),
                "env_blocked",
                state_retry,
                environment=EnvBlocked("busy", missing_predicate="idle", searched_volume="-", wake_on="chunk_moved"),
            )
            _run_skill_cycle(
                obs_retry,
                make("craft_gear", {"item": "stone_pickaxe"}),
                state_retry,
                outcome="completed",
                gain_item="stone_pickaxe",
            )
            obs_retry.on_run_end(state_retry, stop_reason="goal_reached")
            incs_retry = _read_incidents(Path(tmp_retry))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs_retry), incs_retry)

    def test_skill_spam_loop_gather_wood_thresholds(self):
        # 7 次不触发
        with tempfile.TemporaryDirectory() as tmp_7:
            obs7 = SoakObserver(tmp_7, target_goal=None)
            state7 = _make_ready_state()
            for _ in range(7):
                _run_skill_cycle(obs7, make("gather_wood", {"count": 3}), state7, gain_item="oak_log")
            obs7.on_run_end(state7, stop_reason="max_steps")
            incs7 = _read_incidents(Path(tmp_7))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs7), incs7)

        # 8 次触发 SKILL_SPAM_LOOP:gather_wood:<6位sha1>
        with tempfile.TemporaryDirectory() as tmp_8:
            obs8 = SoakObserver(tmp_8, target_goal=None)
            state8 = _make_ready_state()
            for _ in range(8):
                _run_skill_cycle(obs8, make("gather_wood", {"count": 3}), state8, gain_item="oak_log")
            obs8.on_run_end(state8, stop_reason="max_steps")
            incs8 = _read_incidents(Path(tmp_8))
            spam8 = [i for i in incs8 if i["code"] == "SKILL_SPAM_LOOP"]
            self.assertEqual(len(spam8), 1, incs8)
            self.assertRegex(spam8[0]["fingerprint"], r"^SKILL_SPAM_LOOP:gather_wood:[0-9a-f]{6}$")
            self._assert_proactive_fields(spam8[0], obs8, expected_severity="P2")

    def test_skill_spam_loop_scout_island_thresholds_and_unreached_goal_guard(self):
        # 18 次（即使有未达成非 env_blocked 目标）不触发
        with tempfile.TemporaryDirectory() as tmp_18:
            obs18 = SoakObserver(tmp_18, target_goal="upgrade_tools")
            state18 = _make_ready_state()
            for _ in range(18):
                _run_skill_cycle(obs18, make("scout_island"), state18)
            obs18.on_run_end(state18, stop_reason="max_steps")
            incs18 = _read_incidents(Path(tmp_18))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs18), incs18)

        # 20 次但无未达成目标时不触发
        with tempfile.TemporaryDirectory() as tmp_no_goal:
            obs_ng = SoakObserver(tmp_no_goal, target_goal=None)
            state_ng = _make_ready_state()
            for _ in range(20):
                _run_skill_cycle(obs_ng, make("scout_island"), state_ng)
            obs_ng.on_run_end(state_ng, stop_reason="max_steps")
            incs_ng = _read_incidents(Path(tmp_no_goal))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs_ng), incs_ng)

        # 20 次但未达成目标属于 env_blocked 时不触发
        with tempfile.TemporaryDirectory() as tmp_env:
            obs_env = SoakObserver(tmp_env, target_goal="upgrade_tools")
            state_env = _make_ready_state()
            for _ in range(20):
                _run_skill_cycle(obs_env, make("scout_island"), state_env)
            obs_env.on_run_end(
                state_env,
                stop_reason="env_blocked",
                environment_blocked={
                    "upgrade_tools": EnvBlocked(
                        "周边无原木", missing_predicate="wood", searched_volume="r=24", wake_on="chunk_moved"
                    )
                },
            )
            incs_env = _read_incidents(Path(tmp_env))
            self.assertFalse(any(i["code"] == "SKILL_SPAM_LOOP" for i in incs_env), incs_env)

        # 20 次 + 存在未达成非 env_blocked 目标 -> 触发 P1
        with tempfile.TemporaryDirectory() as tmp_20:
            obs20 = SoakObserver(tmp_20, target_goal="upgrade_tools")
            state20 = _make_ready_state()
            for _ in range(20):
                _run_skill_cycle(obs20, make("scout_island"), state20)
            obs20.on_run_end(state20, stop_reason="max_steps")
            incs20 = _read_incidents(Path(tmp_20))
            spam20 = [i for i in incs20 if i["code"] == "SKILL_SPAM_LOOP"]
            self.assertEqual(len(spam20), 1, incs20)
            self.assertRegex(spam20[0]["fingerprint"], r"^SKILL_SPAM_LOOP:scout_island:[0-9a-f]{6}$")
            self._assert_proactive_fields(spam20[0], obs20, expected_severity="P1")

    def test_api_bottleneck_slow_thresholds_and_negative_bounds(self):
        # 正向命中：elapsed >= 60s, total_ms >= 20_000, pct >= 0.30
        with tempfile.TemporaryDirectory() as tmp_hit:
            recorder = TapeRecorder()
            recorder.record(method="POST", path="/find_blocks", body={}, ms=25_000.0, ok=True, thread="main")
            obs = SoakObserver(tmp_hit, tape=recorder)
            obs.started_mono -= 65.0
            state = _make_ready_state()
            obs.on_run_end(state, stop_reason="max_steps")

            incs = _read_incidents(Path(tmp_hit))
            slow = [i for i in incs if i["code"] == "API_BOTTLENECK_SLOW"]
            self.assertEqual(len(slow), 1, incs)
            self.assertEqual(slow[0]["fingerprint"], "API_BOTTLENECK_SLOW:find_blocks")
            self._assert_proactive_fields(slow[0], obs, expected_severity="P2")

        # 负向边界：(1) elapsed < 60s; (2) total_ms < 20_000; (3) pct < 0.30
        negative_cases = [
            ("elapsed_under_60s", 50.0, 25_000.0),
            ("total_ms_under_20s", 65.0, 19_500.0),
            ("ratio_under_30pct", 100.0, 25_000.0),
        ]
        for label, elapsed_back, total_ms in negative_cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp_neg:
                rec_neg = TapeRecorder()
                rec_neg.record(method="POST", path="/find_blocks", body={}, ms=total_ms, ok=True, thread="main")
                obs_neg = SoakObserver(tmp_neg, tape=rec_neg)
                obs_neg.started_mono -= elapsed_back
                state_neg = _make_ready_state()
                obs_neg.on_run_end(state_neg, stop_reason="max_steps")

                incs_neg = _read_incidents(Path(tmp_neg))
                self.assertFalse(
                    any(i["code"] == "API_BOTTLENECK_SLOW" for i in incs_neg),
                    f"{label} 不应误报 API_BOTTLENECK_SLOW: {incs_neg}",
                )


class NormalRunZeroFalsePositiveTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_short_normal_upgrade_tools_run_emits_zero_incidents(self):
        blocks = {}
        for i in range(6):
            blocks[(2 + i, 64, 0)] = "oak_log"
        for i in range(12):
            blocks[(2 + i, 64, 2)] = "stone"
        blocks[(12, 63, 5)] = "grass_block"

        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            world = RichGridWorld(blocks=blocks, keep_inventory=True)
            state = State()
            ex = CountingExecutor(world, state)
            obs = SoakObserver(
                tmp,
                tape=recorder,
                world_snapshot_id="snap-proactive-ok",
                source="local-target",
                target_goal="upgrade_tools",
            )
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
            self.assertIn("upgrade_tools", obs.goals_reached)
            self.assertGreaterEqual(state.count("stone_pickaxe"), 1)
            self.assertEqual(list((Path(tmp) / "incidents").glob("*.json")), [])


class GoalMissPriorityReplayAndSummaryTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_goal_miss_gets_inc001_before_proactive_incidents_and_bundles_include_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            recorder.record(method="POST", path="/find_blocks", body={}, ms=150.0, ok=True, thread="main")
            obs = SoakObserver(tmp, tape=recorder, target_goal="smelt_iron")
            state = _make_ready_state()

            # 同时触发 GOAL_MISS + DISPATCH_STARVATION + SKILL_SPAM_LOOP
            for _ in range(8):
                _run_skill_cycle(obs, make("gather_wood", {"count": 3}), state, gain_item="oak_log")
            for _ in range(2):
                _run_skill_cycle(obs, make("mine_stone", {"count": 3}), state, gain_item="cobblestone")

            obs.on_run_end(state, stop_reason="max_steps")

            incs = _read_incidents(Path(tmp))
            codes = {i["code"]: i for i in incs}
            self.assertIn("GOAL_MISS", codes)
            self.assertIn("DISPATCH_STARVATION", codes)
            self.assertIn("SKILL_SPAM_LOOP", codes)

            # GOAL_MISS 必定拿到 INC-001，主动异常拿到 INC-002+
            self.assertEqual(codes["GOAL_MISS"]["id"], "INC-001")
            self.assertTrue(Path(codes["GOAL_MISS"]["path"]).name.startswith("INC-001-"))
            for code_name in ("DISPATCH_STARVATION", "SKILL_SPAM_LOOP"):
                seq_num = int(codes[code_name]["id"].split("-")[1])
                self.assertGreaterEqual(seq_num, 2)

            # 所有落盘 INC-*.json 均含 episode_profile 与 run_dir
            for inc in incs:
                self.assertIn("episode_profile", inc)
                self.assertIsInstance(inc["episode_profile"], dict)
                self.assertEqual(inc.get("run_dir"), str(tmp))

    def test_duplicate_incident_refresh_updates_episode_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            obs = SoakObserver(tmp, tape=recorder)
            state = _make_ready_state()

            mine = make("mine_stone", {"count": 3})
            obs.on_skill_start(mine, "rule", "test", state)
            obs.on_step(mine, {"op": "dig", "pos": [2, 64, 0], "result": {"warning": "wrong_tool"}}, state)

            inc_files = sorted((Path(tmp) / "incidents").glob("INC-*.json"))
            self.assertEqual(len(inc_files), 1)
            inc1 = json.loads(inc_files[0].read_text(encoding="utf-8"))
            self.assertEqual(inc1["count"], 1)
            self.assertEqual(inc1["run_dir"], str(tmp))
            ep1 = inc1["episode_profile"]

            # 结束当前技能并推进统计与 HTTP 磁带，再触发同一指纹事件
            obs.on_skill_end(mine, "budget_stopped", state)
            recorder.record(method="POST", path="/dig", body={}, ms=320.0, ok=True, thread="main")
            _run_skill_cycle(obs, make("scout_island"), state, elapsed_delta_s=1.0)

            obs.on_skill_start(mine, "rule", "test", state)
            obs.on_step(mine, {"op": "dig", "pos": [3, 64, 0], "result": {"warning": "wrong_tool"}}, state)

            inc2 = json.loads(inc_files[0].read_text(encoding="utf-8"))
            self.assertEqual(inc2["count"], 2)
            self.assertEqual(inc2["run_dir"], str(tmp))
            self.assertNotEqual(inc2["episode_profile"], ep1)
            self.assertEqual(inc2["episode_profile"]["skill_stats"]["mine_stone"]["started"], 2)
            self.assertEqual(inc2["episode_profile"]["http_stats"][0]["path"], "dig")

    def test_goal_miss_with_recorded_http_tape_replays_deterministically_offline(self):
        ran_count = {"n": 0}

        @register
        class QuietNoopDiamondReplay(Skill):
            name = "quiet_noop_diamond_p39"
            description = "Task39 离线回放验证用假完成技能"
            params = {}
            goal = "test:quiet_diamond_p39"
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
                id="quiet_diamond_p39",
                title="采集至少 2 颗钻石",
                tier="T1",
                score=15,
                skill="quiet_noop_diamond_p39",
                done_advancements=("minecraft:story/mine_diamond",),
                done_items=("diamond>=2",),
                budget_s=60.0,
                max_fails_per_ep=1,
                est_cost_s=30.0,
                stage="candidate",
                editable_files=("code/mcagent/skills/diamond.py",),
            ),
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tape_path = Path(tmp) / "http_tape.jsonl"
                recorder = TapeRecorder(tape_path=str(tape_path))
                cfg = Config(base="http://mock.local/api/v1", username="u1", password="pw", token_file="")
                client = TapeClient(cfg, recorder, log=lambda *_: None)
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
                    world_snapshot_id="snap-p39-replay",
                    source="local-target",
                    target_goal="quiet_diamond_p39",
                    ledger=custom_ledger,
                )
                planner = CampaignPlanner(
                    mode="campaign",
                    min_verified="offline",
                    registry={"quiet_noop_diamond_p39": QuietNoopDiamondReplay},
                    target_goal="quiet_diamond_p39",
                    ledger=custom_ledger,
                )
                loop_cfg = LoopConfig(
                    max_steps=5,
                    idle_sleep_s=0.0,
                    enable_monitor=False,
                    target_goal="quiet_diamond_p39",
                    stop_on_goal=True,
                )
                loop = MainLoop(client, state, ex, planner, config=loop_cfg, observer=obs, log=lambda *_: None)
                loop.run()

                inc_files = sorted((Path(tmp) / "incidents").glob("INC-001-P1-goal_miss-*.json"))
                self.assertEqual(len(inc_files), 1)
                inc = json.loads(inc_files[0].read_text(encoding="utf-8"))
                self.assertIn("episode_profile", inc)
                self.assertEqual(inc.get("run_dir"), str(tmp))
                self.assertGreater(len(inc["http_tape"]), 0)

                ran_count["n"] = 0
                replayed = replay_incident(inc_files[0])
                self.assertEqual(replayed["skill"], "quiet_noop_diamond_p39")
                self.assertEqual(replayed["outcome"], "completed")
                self.assertEqual(replayed["steps_executed"], 1)
                self.assertEqual(replayed["tape_consumed"], replayed["tape_total"])
        finally:
            REGISTRY.pop("quiet_noop_diamond_p39", None)

    def test_summary_md_includes_section_5_and_preserves_sections_1_to_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = TapeRecorder()
            recorder.record(method="POST", path="/find_blocks", body={}, ms=420.0, ok=True, thread="main")
            recorder.record(method="POST", path="/dig", body={}, ms=180.0, ok=True, thread="main")

            obs = SoakObserver(tmp, tape=recorder, target_goal="upgrade_tools")
            state = _make_ready_state()

            # 1) 产生环境波动计数以渲染 ## 四
            _run_skill_cycle(
                obs,
                make("gather_wood", {"count": 3}),
                state,
                outcome="replan",
                error_code="path_not_found",
                error_msg="blocked",
            )
            # 2) 累计 >=3 次同参调用以渲染 repeated_skill_args
            for _ in range(3):
                _run_skill_cycle(obs, make("gather_wood", {"count": 3}), state, gain_item="oak_log")

            obs.on_run_end(state, stop_reason="max_steps")
            summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")

            for heading in (
                "## 一、",
                "## 二、",
                "## 三、",
                "## 四、",
                "## 五、接口耗时与高频行为剖析",
            ):
                self.assertIn(heading, summary)

            positions = [
                summary.index("## 一、"),
                summary.index("## 二、"),
                summary.index("## 三、"),
                summary.index("## 四、"),
                summary.index("## 五、接口耗时与高频行为剖析"),
            ]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("find_blocks", summary)
            self.assertIn("gather_wood", summary)

        # 仅有 http_stats 或仅有 repeated_skill_args 时均应产出第 5 节
        with tempfile.TemporaryDirectory() as tmp_http_only:
            rec_only = TapeRecorder()
            rec_only.record(method="GET", path="/status", body=None, ms=35.0, ok=True, thread="main")
            obs_http = SoakObserver(tmp_http_only, tape=rec_only)
            s_http = obs_http.write_summary()
            self.assertIn("## 五、接口耗时与高频行为剖析", s_http)

        with tempfile.TemporaryDirectory() as tmp_rep_only:
            obs_rep = SoakObserver(tmp_rep_only, tape=None)
            st_rep = _make_ready_state()
            for _ in range(3):
                _run_skill_cycle(obs_rep, make("gather_wood", {"count": 3}), st_rep, gain_item="oak_log")
            s_rep = obs_rep.write_summary()
            self.assertIn("## 五、接口耗时与高频行为剖析", s_rep)


if __name__ == "__main__":
    unittest.main()
