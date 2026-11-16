from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from mcagent.loop import ADVANCEMENT_SCORES_V2, SoakObserver
from mcagent.runtime import State


class AdvancementScoreTests(unittest.TestCase):
    def test_packaged_scores_match_every_positive_row_of_archived_draft(self):
        rules = Path(__file__).resolve().parents[2] / "docs" / "99-原始记录" / "rules-v2-draft.md"
        scores = {}
        for line in rules.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| `minecraft:"):
                continue
            _, aid, _, _, points, _, _ = (part.strip() for part in line.split("|"))
            if points.isdecimal() and int(points) > 0:
                scores[aid.strip("`")] = int(points)

        self.assertEqual(len(scores), 99)
        self.assertEqual(sum(scores.values()), 3385)
        self.assertEqual(ADVANCEMENT_SCORES_V2, scores)

    def test_progress_scores_newly_covered_server_ids_once(self):
        ids = (
            "minecraft:nether/all_effects",
            "minecraft:end/elytra",
            "minecraft:husbandry/wax_off",
            "minecraft:nether/fast_travel",
            "minecraft:story/root",
            "minecraft:recipes/building_blocks/oak_planks",
            "example:custom",
        )
        client = Mock()
        client.advancements.return_value = {"advancements": [{"id": aid} for aid in (*ids, ids[0])]}
        with tempfile.TemporaryDirectory() as run_dir:
            observer = SoakObserver(Path(run_dir))
            state = State()
            first = observer.on_progress_tick(state, client=client, force=True)
            second = observer.on_progress_tick(state, client=client, force=True)

        self.assertEqual(first["server_advancements"], len(ids))
        self.assertEqual(first["server_score_v2"], 165)
        self.assertEqual(second["server_score_v2"], 165)
        self.assertEqual(second["inferred_score_v2"], 0)
