"""引擎层「零进度完成」护栏的离线单测。

真机事故（2026-11-24，console-match-20261124-231454）：`discover_structure` 在 20 分钟里
被启动 1879 次，每次只走 1 个动作就自称完成（把记忆里已有的 stronghold 地标当成果），
目标 `follow_ender_eye` 一步没推进，整轮空转。

护栏：同一 (技能, 目标) 连续零进度完成 → 指数退避打冷却，让策略这一拍选不到它。
"""
from __future__ import annotations

import unittest

from mcagent.loop.engine import (
    _NO_PROGRESS_STREAK_BEFORE_COOLDOWN,
    note_no_progress,
)


class _FakeSkill:
    def __init__(self, name="discover_structure", goal="discover:{structure}"):
        self.name = name
        self.goal = goal
        self.cooldowns: list[float] = []

    def mark_failed(self, *, cooldown_s=None):
        self.cooldowns.append(float(cooldown_s or 0.0))


class NoProgressGuardTest(unittest.TestCase):
    def test_first_zero_progress_completion_does_not_cool_down(self):
        book: dict = {}
        skill = _FakeSkill()
        streak = note_no_progress(book, skill, steps=1, moved=0.2)
        self.assertEqual(streak, 1)
        self.assertEqual(skill.cooldowns, [])

    def test_second_consecutive_zero_progress_applies_cooldown(self):
        book: dict = {}
        skill = _FakeSkill()
        note_no_progress(book, skill, steps=1, moved=0.2)
        streak = note_no_progress(book, skill, steps=1, moved=0.2)
        self.assertEqual(streak, _NO_PROGRESS_STREAK_BEFORE_COOLDOWN)
        self.assertEqual(skill.cooldowns, [30.0])

    def test_cooldown_backs_off_exponentially_and_caps(self):
        book: dict = {}
        skill = _FakeSkill()
        for _ in range(6):
            note_no_progress(book, skill, steps=0, moved=None, cap_s=120.0)
        self.assertEqual(skill.cooldowns[:4], [30.0, 60.0, 120.0, 120.0])

    def test_real_progress_resets_the_streak(self):
        book: dict = {}
        skill = _FakeSkill()
        note_no_progress(book, skill, steps=1, moved=0.1)
        note_no_progress(book, skill, steps=1, moved=0.1)
        self.assertTrue(book)
        self.assertEqual(note_no_progress(book, skill, steps=8, moved=0.1), 0)
        self.assertNotIn((skill.name, skill.goal), book)

    def test_moving_far_counts_as_progress_even_with_one_step(self):
        book: dict = {}
        skill = _FakeSkill()
        self.assertEqual(note_no_progress(book, skill, steps=1, moved=12.0), 0)
        self.assertEqual(skill.cooldowns, [])

    def test_different_skills_keep_separate_streaks(self):
        book: dict = {}
        a, b = _FakeSkill("gather_wood", "acq:wood"), _FakeSkill("mine_stone", "acq:cobblestone")
        note_no_progress(book, a, steps=1, moved=0.0)
        note_no_progress(book, b, steps=1, moved=0.0)
        note_no_progress(book, a, steps=1, moved=0.0)
        self.assertEqual(a.cooldowns, [30.0])
        self.assertEqual(b.cooldowns, [])


if __name__ == "__main__":
    unittest.main()
