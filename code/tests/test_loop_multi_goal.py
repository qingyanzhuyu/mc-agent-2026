"""多目标同轮聚焦（verified: offline）。

`--goal a,b` 表示同一轮里一起追 a 和 b：激活集合取两者前置闭包的并集、排序时两者都优先、
全部达成才算本轮达成并停下；每个没达成的目标各开一张 `GOAL_MISS:<id>`，并写明同轮还有谁，
方便判断缺口是不是互相抢材料 / 共享状态造成的。单目标是它的特例，行为不变。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcagent.loop import (
    GOAL_MAP,
    GOALS,
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    SoakObserver,
    reset_world_memo,
)
from mcagent.loop.goals import normalize_goal_ids, rank_actionable_goals, resolve_active_goals
from mcagent.runtime import State
from mcagent.skills import base as skill_base
from mcagent.skills.testing import CountingExecutor
from tests.test_loop_soak import RichGridWorld


def _island_blocks() -> dict:
    blocks = {}
    for i in range(6):
        blocks[(2 + i, 64, 0)] = "oak_log"
    for i in range(12):
        blocks[(2 + i, 64, 2)] = "stone"
    blocks[(12, 63, 5)] = "grass_block"
    return blocks


class MultiGoalFocusTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_normalize_goal_ids(self):
        self.assertEqual(normalize_goal_ids(None), ())
        self.assertEqual(normalize_goal_ids(""), ())
        self.assertEqual(normalize_goal_ids("a"), ("a",))
        self.assertEqual(normalize_goal_ids(" a, b ,a,,"), ("a", "b"))
        self.assertEqual(normalize_goal_ids(["b", "a", "b"]), ("b", "a"))

    def test_active_goals_is_union_of_prereq_closures(self):
        both = {g.id for g in resolve_active_goals(GOALS, target_goal="upgrade_tools,prep_stone_sword")}
        one = {g.id for g in resolve_active_goals(GOALS, target_goal="upgrade_tools")}
        self.assertEqual(one, {"prep_wood", "prep_wooden_pickaxe", "mine_stone", "upgrade_tools"})
        self.assertEqual(both, one | {"prep_stone_sword"})
        with self.assertRaises(KeyError):
            resolve_active_goals(GOALS, target_goal="upgrade_tools,no_such_goal")

    def test_ranking_prefers_every_focused_goal(self):
        state = State()
        active = resolve_active_goals(GOALS)
        # 两条链的下一步都是砍树，排序会按「下一步」去重，所以只看排在最前的那条属于聚焦目标
        for targets in ("mine_stone,prep_furnace", "prep_furnace,mine_stone"):
            ranked = rank_actionable_goals(state, active, target_goal=targets)
            self.assertIn(ranked[0].root_goal.id, set(targets.split(",")))
        ranked_plain = rank_actionable_goals(state, active, target_goal="prep_stone_sword,prep_furnace")
        self.assertIn(ranked_plain[0].root_goal.id, {"prep_stone_sword", "prep_furnace"})

    def test_mainloop_stops_only_after_all_focused_goals_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = RichGridWorld(blocks=_island_blocks(), keep_inventory=True)
            state = State()
            ex = CountingExecutor(world, state)
            goals = "upgrade_tools,prep_stone_sword"
            obs = SoakObserver(tmp, world_snapshot_id="snap-multi", source="local-target", target_goal=goals)
            planner = CampaignPlanner(mode="campaign", min_verified="offline", target_goal=goals)
            cfg = LoopConfig(max_steps=120, idle_sleep_s=0.0, respawn_poll_s=0.001,
                             enable_monitor=False, target_goal=goals, stop_on_goal=True)
            res = MainLoop(world, state, ex, planner, config=cfg, observer=obs, log=lambda *_: None).run()

            self.assertEqual(res.stop_reason, "goal_reached")
            self.assertGreaterEqual(state.count("stone_pickaxe"), 1)
            self.assertGreaterEqual(state.count("stone_sword"), 1)
            self.assertEqual(state.count("furnace"), 0)  # 两个都到手就停，不继续做熔炉
            self.assertEqual(list((Path(tmp) / "incidents").glob("*.json")), [])

            rows = [json.loads(x) for x in (Path(tmp) / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if x]
            run_end = [r for r in rows if r.get("event") == "run_end"][-1]
            self.assertEqual(run_end["target_goal"], goals)
            self.assertEqual(run_end["target_goals"], ["upgrade_tools", "prep_stone_sword"])
            self.assertEqual(run_end["target_goals_reached"], {"upgrade_tools": True, "prep_stone_sword": True})
            self.assertTrue(run_end["target_goal_reached"])
            self.assertEqual(
                run_end["target_goal_elapsed_s"],
                max(run_end["goals_reached"]["upgrade_tools"], run_end["goals_reached"]["prep_stone_sword"]),
            )
            summary = (Path(tmp) / "summary.md").read_text(encoding="utf-8")
            self.assertIn("同轮 2 个", summary)

    def test_each_missed_goal_gets_its_own_ticket_with_co_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State()
            obs = SoakObserver(tmp, world_snapshot_id="snap-miss", source="local-target",
                               target_goal="smelt_iron,kill_a_mob")
            obs.goals_reached["kill_a_mob"] = 50.0  # 一个达成、一个没达成
            obs.on_run_end(state, stop_reason="max_seconds_reached")

            incs = [json.loads(p.read_text(encoding="utf-8")) for p in (Path(tmp) / "incidents").glob("*.json")]
            self.assertEqual([i["fingerprint"] for i in incs], ["GOAL_MISS:smelt_iron"])
            inc = incs[0]
            self.assertEqual(inc["goal_id"], "smelt_iron")
            self.assertEqual(inc["co_targets"], ["kill_a_mob"])
            self.assertEqual(inc["editable_files"], list(GOAL_MAP["smelt_iron"].editable_files))
            self.assertIn("同轮", inc["title"])

            rows = [json.loads(x) for x in (Path(tmp) / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if x]
            run_end = [r for r in rows if r.get("event") == "run_end"][-1]
            self.assertEqual(run_end["target_goals_reached"], {"smelt_iron": False, "kill_a_mob": True})
            self.assertFalse(run_end["target_goal_reached"])
            self.assertIsNone(run_end["target_goal_elapsed_s"])

        with tempfile.TemporaryDirectory() as tmp:  # 两个都没达成：两张单，各自互为 co_targets
            obs = SoakObserver(tmp, source="local-target", target_goal="smelt_iron,kill_a_mob")
            obs.on_run_end(State(), stop_reason="max_steps_reached")
            fps = sorted(json.loads(p.read_text(encoding="utf-8"))["fingerprint"]
                         for p in (Path(tmp) / "incidents").glob("*.json"))
            self.assertEqual(fps, ["GOAL_MISS:kill_a_mob", "GOAL_MISS:smelt_iron"])

        with tempfile.TemporaryDirectory() as tmp:  # Ctrl+C 中断不开单
            obs = SoakObserver(tmp, source="local-target", target_goal="smelt_iron,kill_a_mob")
            obs.on_run_end(State(), stop_reason="keyboard_interrupt")
            self.assertEqual(list((Path(tmp) / "incidents").glob("*.json")), [])


    def test_cli_rejects_unknown_goal_in_list(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from mcagent.loop import __main__ as loop_main

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = loop_main.main(["--goal", "upgrade_tools,no_such_goal", "--dry"])
        self.assertEqual(code, 2)
        self.assertIn("no_such_goal", err.getvalue())

if __name__ == "__main__":
    unittest.main()
