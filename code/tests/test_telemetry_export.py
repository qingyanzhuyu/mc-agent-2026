"""离线单元测试：`telemetry_export` 聚合长跑日志生成 `goal_cost_table.json` 并被 `load_goal_telemetry` 消费。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mcagent.loop.goals import GOAL_MAP, effective_goal_cost, load_goal_telemetry
from mcagent.loop.telemetry_export import aggregate_goal_telemetry, write_goal_cost_table


class TelemetryExportTests(unittest.TestCase):
    def test_aggregate_and_roundtrip_into_effective_goal_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seg1 = root / "seg-01"
            seg2 = root / "seg-02"
            seg1.mkdir()
            seg2.mkdir()

            (seg1 / "progress_curve.jsonl").write_text(
                json.dumps({"elapsed_s": 25.0, "goals_reached": ["prep_wood", "mine_stone"]}) + "\n",
                encoding="utf-8",
            )
            (seg1 / "timeline.jsonl").write_text(
                "\n".join([
                    json.dumps({"event": "skill_start", "elapsed_s": 1.0, "skill": "gather_wood",
                                "reason": "[目标:shiny_gear] 推进目标 prep_wood（采集起步原木）", "pos": [10, 64, 20]}),
                    json.dumps({"event": "skill_end", "elapsed_s": 21.0, "skill": "gather_wood", "outcome": "completed"}),
                    json.dumps({"event": "skill_start", "elapsed_s": 22.0, "skill": "mine_stone",
                                "reason": "[目标:upgrade_tools] 推进目标 mine_stone（采集圆石）", "pos": [12, 64, 22]}),
                    json.dumps({"event": "skill_end", "elapsed_s": 42.0, "skill": "mine_stone", "outcome": "completed"}),
                    json.dumps({"event": "skill_start", "elapsed_s": 50.0, "skill": "smelt_iron",
                                "reason": "[目标:shiny_gear] 推进目标 smelt_iron（冶炼出铁锭）", "pos": [12, 50, 22]}),
                    json.dumps({"event": "skill_end", "elapsed_s": 140.0, "skill": "smelt_iron", "outcome": "env_blocked"}),
                ]) + "\n",
                encoding="utf-8",
            )

            (seg2 / "progress_curve.jsonl").write_text(
                json.dumps({"elapsed_s": 80.0, "goals_reached": ["prep_wood", "smelt_iron"]}) + "\n",
                encoding="utf-8",
            )
            (seg2 / "timeline.jsonl").write_text(
                "\n".join([
                    json.dumps({"event": "skill_start", "elapsed_s": 2.0, "skill": "gather_wood",
                                "reason": "[目标:shiny_gear] 推进目标 prep_wood（采集起步原木）"}),
                    json.dumps({"event": "skill_end", "elapsed_s": 32.0, "skill": "gather_wood", "outcome": "completed"}),
                    json.dumps({"event": "skill_start", "elapsed_s": 40.0, "skill": "smelt_iron",
                                "reason": "[目标:shiny_gear] 推进目标 smelt_iron（冶炼出铁锭）"}),
                    json.dumps({"event": "skill_end", "elapsed_s": 110.0, "skill": "smelt_iron", "outcome": "completed"}),
                ]) + "\n",
                encoding="utf-8",
            )

            out_path = root / "goal_cost_table.json"
            payload = write_goal_cost_table([root], out_path, source="local-target")
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["source"], "local-target")
            self.assertEqual(payload["runs_included"], ["seg-01", "seg-02"])

            loaded = load_goal_telemetry(out_path)
            self.assertEqual(loaded["prep_wood"]["episodes"], 2)
            self.assertEqual(loaded["prep_wood"]["hits"], 2)
            self.assertEqual(loaded["prep_wood"]["hit_rate"], 1.0)
            self.assertEqual(loaded["prep_wood"]["p50_elapsed_s"], 25.0)
            self.assertEqual(loaded["smelt_iron"]["episodes"], 2)
            self.assertEqual(loaded["smelt_iron"]["hits"], 1)
            self.assertEqual(loaded["smelt_iron"]["hit_rate"], 0.5)
            self.assertEqual(loaded["smelt_iron"]["p50_elapsed_s"], 80.0)
            # effective_goal_cost = 80.0 / 0.5 = 160.0
            self.assertAlmostEqual(effective_goal_cost(GOAL_MAP["smelt_iron"], loaded), 160.0, places=2)
            # 确认输出严格零坐标
            raw_text = out_path.read_text(encoding="utf-8")
            self.assertNotIn("[10, 64, 20]", raw_text)


if __name__ == "__main__":
    unittest.main()
