"""挂在比赛主循环上的轻量观察者：预期核对 + 噪声收敛分流 + 进度曲线 + 去重取证包。

遵循评审修订 #1 / #4 / #5：
1. 只做观察者（`SoakObserver`），不接管主循环控制流；比赛当天同样可开启实时输出。
2. 预期核对以技能自带的 `verify_done()` 为主；动作级只利用已有返回值做零额外 RTT 检查；
   姿态泄漏仅在**非死亡/非断线**结束时检查（规范 §4.11 死亡重生服务端自动复位姿态）。
3. 严格区分 `bug`（契约/安全缺陷）、`strategy`（策略弱/震荡卡死）与 `env_variance`（正常环境波动），
   普通单次 `Replan` 只进流水账不刷屏异常榜。
4. 每份异常包附 `git_commit`、`world_snapshot_id`、运行配置与 `http_tape` 切片，支持离线确定性回放。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from ..runtime import State, _inv_fingerprint
from ..skills.items import AIR, bare
from .goals import GOALS, GoalSpec, diagnose_goal_gap, is_goal_achieved, normalize_goal_ids
from .tape import TapeRecorder

# RULES v2 草案常见 T1/T2 计分表映射（用于计算进度分曲线）
ADVANCEMENT_SCORES_V2: dict[str, int] = {
    "minecraft:story/mine_stone": 5,
    "minecraft:story/upgrade_tools": 5,
    "minecraft:story/smelt_iron": 5,
    "minecraft:story/iron_tools": 5,
    "minecraft:story/obtain_armor": 5,
    "minecraft:story/lava_bucket": 15,
    "minecraft:story/deflect_arrow": 15,
    "minecraft:story/form_obsidian": 15,
    "minecraft:story/mine_diamond": 15,
    "minecraft:story/enchant_item": 30,
    "minecraft:story/enter_the_nether": 30,
    "minecraft:story/shiny_gear": 60,
    "minecraft:story/follow_ender_eye": 60,
    "minecraft:adventure/kill_a_mob": 5,
    "minecraft:adventure/shoot_arrow": 5,
    "minecraft:adventure/sleep_in_bed": 5,
    "minecraft:adventure/ol_betsy": 15,
    "minecraft:adventure/bullseye": 15,
    "minecraft:adventure/use_lodestone": 15,
    "minecraft:husbandry/plant_seed": 5,
    "minecraft:husbandry/breed_an_animal": 5,
    "minecraft:husbandry/fishy_business": 15,
}

# 本地靶场 /advancements 返回 0 时的背包里程碑代理判据（与服务端真实进度分列展示，绝不混同）
_ITEM_MILESTONES: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("story/mine_stone", ("cobblestone", "cobbled_deepslate"), 5),
    ("story/upgrade_tools", ("stone_pickaxe",), 5),
    ("story/smelt_iron", ("iron_ingot",), 5),
    ("story/iron_tools", ("iron_pickaxe",), 5),
    ("story/obtain_armor", ("iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots"), 5),
    ("story/lava_bucket", ("lava_bucket",), 15),
    ("story/form_obsidian", ("obsidian",), 15),
    ("story/mine_diamond", ("diamond",), 15),
    ("story/shiny_gear", ("diamond_helmet", "diamond_chestplate", "diamond_leggings", "diamond_boots"), 60),
)

_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 初赛公告参考额度口径与满场时长（用于报告额度占比与 8h 满场外推）
QUOTA_INPUT_TOKENS = 8_000_000
QUOTA_OUTPUT_TOKENS = 1_500_000
MATCH_DURATION_S = 8 * 3600


def _nonneg_float(val: Any) -> float:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) and x > 0.0 else 0.0


def _nonneg_int(val: Any) -> int:
    if isinstance(val, bool):
        return 0
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def compute_token_valuation(
    prompt_tokens: int,
    completion_tokens: int,
    elapsed_s: float,
    *,
    price_in_per_m: Optional[float] = None,
    price_out_per_m: Optional[float] = None,
) -> dict[str, Any]:
    """计算 token 花费的两种价值口径：初赛公告额度占比（含 8h 外推）与可选金额。"""
    p_tok = max(0, int(prompt_tokens))
    c_tok = max(0, int(completion_tokens))
    p_in = _nonneg_float(os.environ.get("LLM_PRICE_IN_PER_M", "0")) if price_in_per_m is None else max(0.0, price_in_per_m)
    p_out = _nonneg_float(os.environ.get("LLM_PRICE_OUT_PER_M", "0")) if price_out_per_m is None else max(0.0, price_out_per_m)

    in_quota_pct = p_tok / QUOTA_INPUT_TOKENS * 100.0
    out_quota_pct = c_tok / QUOTA_OUTPUT_TOKENS * 100.0

    has_8h = elapsed_s > 0.0
    scale_8h = (MATCH_DURATION_S / elapsed_s) if has_8h else 0.0
    proj_8h_in = round(p_tok * scale_8h) if has_8h else 0
    proj_8h_out = round(c_tok * scale_8h) if has_8h else 0
    proj_8h_in_pct = proj_8h_in / QUOTA_INPUT_TOKENS * 100.0
    proj_8h_out_pct = proj_8h_out / QUOTA_OUTPUT_TOKENS * 100.0

    has_price = (p_in > 0.0) or (p_out > 0.0)
    cost = (p_tok / 1_000_000.0) * p_in + (c_tok / 1_000_000.0) * p_out if has_price else 0.0
    proj_8h_cost = cost * scale_8h if (has_price and has_8h) else 0.0

    return {
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "total_tokens": p_tok + c_tok,
        "in_quota_pct": in_quota_pct,
        "out_quota_pct": out_quota_pct,
        "has_8h": has_8h,
        "proj_8h_in": proj_8h_in,
        "proj_8h_out": proj_8h_out,
        "proj_8h_in_pct": proj_8h_in_pct,
        "proj_8h_out_pct": proj_8h_out_pct,
        "has_price": has_price,
        "price_in_per_m": p_in,
        "price_out_per_m": p_out,
        "cost": cost,
        "proj_8h_cost": proj_8h_cost,
    }


def _format_source_breakdown(counts: dict[str, int], unit: str) -> str:
    total = sum(counts.values())
    if total <= 0:
        return f"0 {unit}"
    ordered_keys = ["llm", "llm_fallback_rule", "rule"] + [
        k for k in sorted(counts) if k not in ("llm", "llm_fallback_rule", "rule") and counts[k] > 0
    ]
    parts = [
        f"`{k}` `{counts.get(k, 0)}` {unit} (`{counts.get(k, 0) / total * 100.0:.1f}%`)"
        for k in ordered_keys
    ]
    return f"共 `{total}` {unit}（" + " · ".join(parts) + "）"


def git_provenance(cwd: Optional[str] = None) -> dict[str, Any]:
    work = cwd or os.path.dirname(os.path.abspath(__file__))
    info: dict[str, Any] = {"commit": "unknown", "branch": "unknown", "dirty": None}
    try:
        info["commit"] = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=work, stderr=subprocess.DEVNULL, text=True
        ).strip()
        info["branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work, stderr=subprocess.DEVNULL, text=True
        ).strip()
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=work, stderr=subprocess.DEVNULL, text=True
        ).strip()
        info["dirty"] = bool(dirty_out)
    except Exception:
        pass
    return info


class SoakObserver:
    """挂接在 `MainLoop` 上的运行记录与异常收敛核对器。"""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        tape: Optional[TapeRecorder] = None,
        world_snapshot_id: str = "unspecified",
        source: str = "local-target",
        sample_verify_rate: float = 0.0,
        stuck_steps: int = 12,
        replan_oscillation_threshold: int = 3,
        baseline_dir: Optional[str | Path] = None,
        run_meta: Optional[dict] = None,
        target_goal: Optional[str] = None,
        ledger: tuple[GoalSpec, ...] = GOALS,
    ):
        self.run_dir = Path(run_dir)
        self.incidents_dir = self.run_dir / "incidents"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.incidents_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.run_dir / "timeline.jsonl"
        self.curve_path = self.run_dir / "progress_curve.jsonl"
        self.summary_path = self.run_dir / "summary.md"

        self.tape = tape
        self.world_snapshot_id = world_snapshot_id
        self.source = source
        self.sample_verify_rate = max(0.0, min(1.0, sample_verify_rate))
        self.stuck_steps = max(4, stuck_steps)
        self.replan_oscillation_threshold = max(2, replan_oscillation_threshold)
        self.baseline_dir = Path(baseline_dir) if baseline_dir else None
        self.git = git_provenance()
        self.run_meta = dict(run_meta or {})
        self.ledger: tuple[GoalSpec, ...] = tuple(ledger)
        self._ledger_map: dict[str, GoalSpec] = {g.id: g for g in self.ledger}
        meta_goal = self.run_meta.get("target_goal")
        # 聚焦目标可以是一个或多个（"a,b"）；多个表示同一轮一起追，每个未达成的目标各开一张 GOAL_MISS
        self.target_goals: tuple[str, ...] = normalize_goal_ids(
            target_goal if target_goal is not None else meta_goal
        )
        self.target_goal: Optional[str] = ",".join(self.target_goals) or None
        if self.target_goal is not None:
            self.run_meta.setdefault("target_goal", self.target_goal)
        self.started_at = time.time()
        self.started_mono = time.monotonic()

        self._recent_steps: deque[dict] = deque(maxlen=20)
        self._stuck_window: deque[tuple[list[float], dict[str, int]]] = deque(maxlen=self.stuck_steps)
        self._goal_failures: dict[str, deque[float]] = {}
        self._incidents: dict[str, dict] = {}
        self._incident_seq = 0
        self._skill_ctx: Optional[dict] = None
        self._last_skill_snapshots: dict[str, tuple[dict, list[dict]]] = {}
        self._last_replan: Optional[dict[str, Any]] = None

        self.skill_stats: dict[str, dict[str, int]] = {}
        self.env_variance_counts: dict[str, int] = {}
        self.decision_sources: dict[str, int] = {}
        self.step_sources: dict[str, int] = {}
        self.llm_latencies_ms: list[float] = []
        self.llm_fallbacks = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.deaths_count = 0
        self.respawn_ms_samples: list[float] = []
        self.keep_inventory_inferred: Optional[bool] = None

        self.server_advancements: dict[str, float] = {}
        self.inferred_milestones: dict[str, float] = {}
        self.goals_reached: dict[str, float] = {}
        self.curve_points: list[dict] = []

    def _elapsed_s(self) -> float:
        return round(time.monotonic() - self.started_mono, 1)

    def _append_jsonl(self, path: Path, row: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- 主循环生命周期钩子 --------------------------------------------
    def on_run_start(self, state: State) -> None:
        self._append_jsonl(self.timeline_path, {
            "event": "run_start",
            "t": round(time.time(), 3),
            "source": self.source,
            "git": self.git,
            "world_snapshot_id": self.world_snapshot_id,
            "meta": self.run_meta,
            "pos": state.pos,
        })
        self.on_progress_tick(state, client=None, force=True)
        self.write_summary()

    def on_skill_start(self, skill, plan_source: str, reason: str, state: State) -> dict[str, int]:
        st = state.snapshot()
        inv = copy.deepcopy(state.inventory)
        fp = _inv_fingerprint(inv) or {}
        if self.tape is not None:
            self.tape.begin_skill_slice()
        self._skill_ctx = {
            "skill": skill.name,
            "goal": skill.goal,
            "args": dict(skill.args),
            "plan_source": plan_source,
            "reason": reason,
            "started_elapsed_s": self._elapsed_s(),
            "initial_status": st,
            "initial_inventory": inv,
            "before_counts": fp,
            "pre_sneaking": bool(st.get("sneaking", False)),
            "pre_blocking": bool(st.get("blocking", False)),
            "steps": 0,
        }
        stat = self.skill_stats.setdefault(skill.name, {"started": 0, "ok": 0, "replan": 0, "budget_stopped": 0, "bug": 0})
        stat["started"] += 1
        self.decision_sources[plan_source] = self.decision_sources.get(plan_source, 0) + 1
        self._append_jsonl(self.timeline_path, {
            "event": "skill_start",
            "elapsed_s": self._elapsed_s(),
            "skill": skill.name,
            "args": skill.args,
            "source": plan_source,
            "reason": reason,
            "pos": state.pos,
        })
        return fp

    def on_step(self, skill, record: Optional[dict], state: State, client=None) -> None:
        if self._skill_ctx is not None:
            self._skill_ctx["steps"] += 1
        plan_source = str((self._skill_ctx or {}).get("plan_source") or "rule")
        self.step_sources[plan_source] = self.step_sources.get(plan_source, 0) + 1
        rec_copy = copy.deepcopy(record or {})
        step_row = {
            "event": "step",
            "elapsed_s": self._elapsed_s(),
            "skill": skill.name,
            "source": plan_source,
            "op": rec_copy.get("op"),
            "pos": state.pos,
            "hp": state.hp,
            "food": state.st.get("food"),
            "record": rec_copy,
        }
        self._recent_steps.append(step_row)
        self._append_jsonl(self.timeline_path, step_row)

        # 1) 零额外 RTT 动作返回值检查
        self._check_step_cheap(skill, rec_copy, state, client=client)

        # 2) 连续无进展卡死检测
        fp = _inv_fingerprint(state.inventory) or {}
        self._stuck_window.append((list(state.pos), fp))
        if len(self._stuck_window) >= self.stuck_steps:
            p0, fp0 = self._stuck_window[0]
            max_disp = max(math.dist(p0, px) for px, _ in self._stuck_window)
            inv_changed = any(fpx != fp0 for _, fpx in self._stuck_window)
            if max_disp < 0.8 and not inv_changed:
                self._stuck_window.clear()
                self._report_incident(
                    code="STUCK_NO_PROGRESS",
                    severity="P1",
                    category="strategy",
                    title=f"连续 {self.stuck_steps} 步位移 <0.8 格且背包无变化",
                    detail={"skill": skill.name, "pos": state.pos, "max_displacement": round(max_disp, 2)},
                    state=state,
                    outcome="stuck",
                )

    def _check_step_cheap(self, skill, rec_obj: dict, state: State, client=None) -> None:
        op = rec_obj.get("op")
        res = rec_obj.get("result")
        if not isinstance(res, dict):
            return

        if op == "dig" and res.get("warning"):
            self._report_incident(
                code="DIG_TOOL_WARNING",
                severity="P2",
                category="strategy",
                title=f"挖掘带服务端工具告警: {res.get('warning')}",
                detail={"pos": rec_obj.get("pos"), "dug": res.get("dug"), "warning": res.get("warning")},
                state=state,
                outcome="running",
            )
            return

        if op == "move_to" and isinstance(rec_obj.get("pos"), list) and isinstance(res.get("pos"), list):
            tx, _, tz = rec_obj["pos"][0], rec_obj["pos"][1], rec_obj["pos"][2]
            rx, _, rz = res["pos"][0], res["pos"][1], res["pos"][2]
            horiz = math.hypot(rx - tx, rz - tz)
            if horiz > 6.0:
                self._report_incident(
                    code="MOVE_RANGE_SHORTFALL",
                    severity="P2",
                    category="bug",
                    title=f"move_to 返回成功但水平距目标仍差 {horiz:.1f} 格",
                    detail={"target": rec_obj.get("pos"), "actual": res.get("pos"), "horiz_err": round(horiz, 2)},
                    state=state,
                    outcome="running",
                )

        # 低频抽样复核（默认 0%，仅在显式开启 --sample-verify 时偶发调用 get_block）
        if (
            client is not None
            and self.sample_verify_rate > 0
            and op == "dig"
            and isinstance(rec_obj.get("pos"), list)
            and random.random() < self.sample_verify_rate
        ):
            try:
                bname = bare(client.get_block(rec_obj["pos"]).get("name", ""))
                if bname and bname not in AIR and bname not in ("water", "lava", "sand", "gravel"):
                    self._report_incident(
                        code="GHOST_DIG_ROLLBACK",
                        severity="P1",
                        category="bug",
                        title=f"dig 返回成功但抽样复核方块仍为 {bname}",
                        detail={"pos": rec_obj["pos"], "remaining_block": bname},
                        state=state,
                        outcome="running",
                    )
            except Exception:
                pass

    def on_skill_end(
        self,
        skill,
        outcome: str,
        state: State,
        *,
        error_code: str = "",
        error_msg: str = "",
        died_or_disconnected: bool = False,
    ) -> None:
        tape_slice = self.tape.end_skill_slice() if self.tape is not None else []
        ctx = self._skill_ctx or {"skill": skill.name, "args": dict(skill.args), "before_counts": {}}
        ctx["outcome"] = outcome
        stat = self.skill_stats.setdefault(skill.name, {"started": 0, "ok": 0, "replan": 0, "budget_stopped": 0, "bug": 0})

        post_err = ""
        if outcome == "completed":
            verifier = getattr(skill, "verify_done", None)
            if callable(verifier):
                post_err = verifier(state, ctx.get("before_counts") or {}) or ""
            if post_err:
                outcome = "postcondition_failed"
                ctx["outcome"] = outcome
                stat["bug"] += 1
                self._report_incident(
                    code="SKILL_POSTCONDITION_FAIL",
                    severity="P1",
                    category="bug",
                    title=post_err,
                    detail={"skill": skill.name, "args": skill.args},
                    state=state,
                    outcome=outcome,
                    skill_ctx=ctx,
                    tape_slice=tape_slice,
                )
            else:
                stat["ok"] += 1
                # 经后置判据确认的成功打断该目标的连续失败，其他目标的历史保持不变。
                self._goal_failures.pop(skill.goal, None)
        elif outcome == "budget_stopped":
            # 外层运行预算只说明未继续执行，不能判定技能失败；保留流水与姿态核对。
            stat["budget_stopped"] += 1
        else:
            stat["replan"] += 1
            if outcome == "replan":
                self._last_replan = {
                    "skill": skill.name,
                    "args": dict(skill.args),
                    "error_code": error_code,
                    "error_msg": error_msg[:200],
                    "elapsed_s": self._elapsed_s(),
                }
            self._classify_skill_failure(skill, outcome, error_code, error_msg, state, ctx, tape_slice)

        self._last_skill_snapshots[skill.name] = (copy.deepcopy(ctx), copy.deepcopy(tape_slice))

        # 姿态泄漏检查：仅在未死亡、未断线的正常结束或软中断时核对（规范 §4.11 重生自动复位姿态）
        if not died_or_disconnected and state.st.get("alive", True):
             leaked_sneak = bool(state.st.get("sneaking", False)) and not ctx.get("pre_sneaking", False)
             leaked_block = bool(state.st.get("blocking", False)) and not ctx.get("pre_blocking", False)
             if leaked_sneak or leaked_block:
                 stat["bug"] += 1
                 self._report_incident(
                     code="POSTURE_LEAK",
                     severity="P1",
                     category="bug",
                     title=f"技能 {skill.name} 结束后姿态未复位 (sneaking={leaked_sneak}, blocking={leaked_block})",
                     detail={"skill": skill.name, "outcome": outcome},
                     state=state,
                     outcome=outcome,
                     skill_ctx=ctx,
                     tape_slice=tape_slice,
                 )

        self._append_jsonl(self.timeline_path, {
            "event": "skill_end",
            "elapsed_s": self._elapsed_s(),
            "skill": skill.name,
            "outcome": outcome,
            "error_code": error_code,
            "error_msg": error_msg[:200],
            "postcondition_error": post_err,
        })
        self._skill_ctx = None
        self.write_summary()

    def _classify_skill_failure(
        self,
        skill,
        outcome: str,
        error_code: str,
        error_msg: str,
        state: State,
        ctx: dict,
        tape_slice: list[dict],
    ) -> None:
        if outcome in ("fatal", "unexpected_exception") or error_code in ("bad_args", "unknown_api"):
            self._report_incident(
                code="CONTRACT_OR_FATAL_ERROR",
                severity="P0",
                category="bug",
                title=f"{skill.name} 触发接口契约或致命异常 ({error_code or outcome})",
                detail={"error_code": error_code, "message": error_msg},
                state=state,
                outcome=outcome,
                api_error_code=error_code,
                skill_ctx=ctx,
                tape_slice=tape_slice,
            )
            return

        # 常见单次环境波动：记入波动计数，只有短时间反复撞同一目标才升级为震荡问题单
        variance_key = f"{skill.name}:{error_code or outcome}"
        self.env_variance_counts[variance_key] = self.env_variance_counts.get(variance_key, 0) + 1
        now_m = time.monotonic()
        q = self._goal_failures.setdefault(skill.goal, deque(maxlen=10))
        q.append(now_m)
        recent_n = sum(1 for t in q if now_m - t <= 180.0)
        if recent_n >= self.replan_oscillation_threshold:
            self._report_incident(
                code="REPLAN_OSCILLATION",
                severity="P2",
                category="strategy",
                title=f"目标 {skill.goal} 在 180s 内连续失败 {recent_n} 次 ({error_code or error_msg[:60]})",
                detail={"goal": skill.goal, "recent_failures": recent_n, "last_error": error_msg},
                state=state,
                outcome=outcome,
                api_error_code=error_code,
                skill_ctx=ctx,
                tape_slice=tape_slice,
            )

    def on_death_recovered(
        self,
        state: State,
        *,
        respawn_ms: float,
        cause: str,
        death_pos: Any,
        before_inv_fp: dict[str, int],
        after_inv_fp: dict[str, int],
        triggered_death_loop: bool = False,
    ) -> None:
        self.deaths_count += 1
        self.respawn_ms_samples.append(round(respawn_ms, 1))
        if before_inv_fp:
            kept = all(after_inv_fp.get(k, 0) >= v for k, v in before_inv_fp.items())
            self.keep_inventory_inferred = kept

        cause_low = (cause or "").lower()
        if triggered_death_loop:
            code, sev, cat = "DEATH_LOOP_TRIGGERED", "P0", "bug"
        elif any(k in cause_low for k in ("world", "void", "lava", "drown", "suffocat", "fell from a high")):
            code, sev, cat = "FATAL_ENV_DEATH", "P0", "bug"
        else:
            code, sev, cat = "COMBAT_OR_MOB_DEATH", "P1", "strategy"

        self._append_jsonl(self.timeline_path, {
            "event": "death_recovered",
            "elapsed_s": self._elapsed_s(),
            "deaths_total": self.deaths_count,
            "respawn_ms": round(respawn_ms, 1),
            "cause": cause,
            "death_pos": death_pos,
            "respawn_pos": state.pos,
            "keep_inventory_inferred": self.keep_inventory_inferred,
            "in_safe_mode": state.in_safe_mode,
        })
        self._report_incident(
            code=code,
            severity=sev,
            category=cat,
            title=f"角色死亡并自愈重生: {cause or '原因未知'} (重生耗时 {respawn_ms:.0f}ms)",
            detail={
                "cause": cause,
                "death_pos": death_pos,
                "respawn_pos": state.pos,
                "respawn_ms": round(respawn_ms, 1),
                "keep_inventory_inferred": self.keep_inventory_inferred,
                "deaths_in_60s": state.deaths_in(60),
            },
            state=state,
            outcome="death",
        )
        self.write_summary()

    def on_llm_call(self, telemetry: dict) -> None:
        ms = float(telemetry.get("ms", 0.0))
        p_tok = _nonneg_int(telemetry.get("prompt_tokens"))
        c_tok = _nonneg_int(telemetry.get("completion_tokens"))
        self.prompt_tokens += p_tok
        self.completion_tokens += c_tok
        self.llm_latencies_ms.append(ms)
        if not telemetry.get("ok"):
            self.llm_fallbacks += 1
            self._report_incident(
                code="LLM_INVALID_OR_TIMEOUT",
                severity="P3",
                category="strategy",
                title=f"LLM 未产出合法技能选择 ({ms:.0f}ms): {'; '.join(telemetry.get('errors') or [])[:100]}",
                detail=telemetry,
                state=State(),
                outcome="llm_fallback",
            )
        self._append_jsonl(self.timeline_path, {
            "event": "llm_call",
            "elapsed_s": self._elapsed_s(),
            **telemetry,
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "total_tokens": p_tok + c_tok,
            "cumulative_prompt_tokens": self.prompt_tokens,
            "cumulative_completion_tokens": self.completion_tokens,
            "cumulative_total_tokens": self.prompt_tokens + self.completion_tokens,
        })

    def update_goals_reached(self, state: State) -> dict[str, float]:
        elapsed = self._elapsed_s()
        for g in self.ledger:
            if g.id not in self.goals_reached and is_goal_achieved(g, state, self.server_advancements):
                self.goals_reached[g.id] = elapsed
        return self.goals_reached

    def on_progress_tick(self, state: State, client=None, *, force: bool = False) -> dict:
        self.update_goals_reached(state)
        elapsed = self._elapsed_s()
        if not force and self.curve_points and elapsed - self.curve_points[-1]["elapsed_s"] < 25.0:
            return self.curve_points[-1]

        if client is not None:
            try:
                adv = client.advancements(status="completed", include_recipes=False)
                for item in (adv.get("advancements") or []):
                    aid = item.get("id") if isinstance(item, dict) else None
                    if aid and aid not in self.server_advancements:
                        self.server_advancements[aid] = elapsed
            except Exception:
                pass

        self.update_goals_reached(state)

        for mid, items, _pts in _ITEM_MILESTONES:
            if mid not in self.inferred_milestones and any(state.count(it) >= 1 for it in items):
                self.inferred_milestones[mid] = elapsed

        srv_score = sum(ADVANCEMENT_SCORES_V2.get(aid, 0) for aid in self.server_advancements)
        inf_score = sum(pts for mid, _, pts in _ITEM_MILESTONES if mid in self.inferred_milestones)
        point = {
            "elapsed_s": elapsed,
            "deaths": self.deaths_count,
            "server_advancements": len(self.server_advancements),
            "server_score_v2": srv_score,
            "inferred_milestones": sorted(self.inferred_milestones),
            "inferred_score_v2": inf_score,
            "goals_reached": sorted(self.goals_reached),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }
        self.curve_points.append(point)
        self._append_jsonl(self.curve_path, point)
        return point

    def on_run_end(self, state: State, client=None, *, stop_reason: str = "") -> None:
        self.on_progress_tick(state, client=client, force=True)
        self.update_goals_reached(state)
        reached_map = {gid: gid in self.goals_reached for gid in self.target_goals}
        target_reached = bool(self.target_goals) and all(reached_map.values())

        if stop_reason != "keyboard_interrupt":
            for gid in self.target_goals:
                if not reached_map[gid]:
                    self._report_goal_miss(gid, state, stop_reason)

        self._append_jsonl(self.timeline_path, {
            "event": "run_end",
            "elapsed_s": self._elapsed_s(),
            "stop_reason": stop_reason,
            "source": self.source,
            "deaths": self.deaths_count,
            "target_goal": self.target_goal,
            "target_goals": list(self.target_goals),
            "target_goals_reached": reached_map,
            "target_goal_reached": target_reached,
            "target_goal_elapsed_s": (
                max(self.goals_reached[g] for g in self.target_goals) if target_reached else None
            ),
            "goals_reached": dict(self.goals_reached),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "decision_sources": dict(self.decision_sources),
            "step_sources": dict(self.step_sources),
            "llm_calls": len(self.llm_latencies_ms),
            "llm_fallbacks": self.llm_fallbacks,
        })
        self.write_summary()

    def _report_goal_miss(self, gid: str, state: State, stop_reason: str) -> None:
        g = self._ledger_map.get(gid)
        gap = diagnose_goal_gap(g, state, self.server_advancements, self.ledger) if g is not None else None
        if g is not None and g.skill in self._last_skill_snapshots:
            saved_ctx, saved_tape = copy.deepcopy(self._last_skill_snapshots[g.skill])
        else:
            saved_ctx = {
                "skill": g.skill if g is not None else "session",
                "args": g.args_dict if g is not None else {},
                "initial_status": state.snapshot(),
                "initial_inventory": copy.deepcopy(state.inventory),
                "before_counts": _inv_fingerprint(state.inventory) or {},
            }
            saved_tape = []

        editable = list(g.editable_files) if g is not None else []
        co_targets = [x for x in self.target_goals if x != gid]
        budget_sum = sum(
            self._ledger_map[x].budget_s for x in self.target_goals if x in self._ledger_map
        )
        self._report_incident(
            code="GOAL_MISS",
            severity="P1",
            category="strategy",
            custom_fingerprint=f"GOAL_MISS:{gid}",
            title=f"目标 {gid}（{g.title if g else gid}）未在本轮预算内达成"
            + (f"（与 {', '.join(co_targets)} 同轮）" if co_targets else ""),
            detail={
                "goal_id": gid,
                "goal_title": g.title if g else gid,
                "tier": g.tier if g else "UNKNOWN",
                "score": g.score if g else 0,
                "budget_s": g.budget_s if g else 0.0,
                "elapsed_s": self._elapsed_s(),
                "stop_reason": stop_reason,
                # 同轮一起追的其他目标：缺口可能是互相抢材料/共享状态，而不是本目标技能本身
                "co_targets": co_targets,
                "co_targets_reached": [x for x in co_targets if x in self.goals_reached],
                "round_budget_sum_s": budget_sum,
                "completed_prereqs": gap["completed_prereqs"] if gap else [],
                "missing_prereqs": gap["missing_prereqs"] if gap else [],
                "bottleneck_goal_id": gap["bottleneck_goal_id"] if gap else gid,
                "bottleneck_skill": gap["bottleneck_skill"] if gap else (g.skill if g else None),
                "missing_items": gap["missing_items"] if gap else [],
                "summary": gap["summary"] if gap else f"未知目标 {gid}",
                "target_skill_started": bool(g and g.skill in self._last_skill_snapshots),
                "last_replan": copy.deepcopy(self._last_replan),
                "skill_stats": copy.deepcopy(self.skill_stats),
                "editable_files": editable,
            },
            extra_top_fields={
                "goal_id": gid,
                "editable_files": editable,
                "co_targets": co_targets,
            },
            state=state,
            outcome="goal_miss",
            skill_ctx=saved_ctx,
            tape_slice=saved_tape,
        )

    # ---- 去重立档与汇总看板 --------------------------------------------
    def _report_incident(
        self,
        *,
        code: str,
        severity: str,
        category: str,
        title: str,
        detail: dict,
        state: State,
        outcome: str,
        api_error_code: str = "",
        skill_ctx: Optional[dict] = None,
        tape_slice: Optional[list[dict]] = None,
        custom_fingerprint: Optional[str] = None,
        extra_top_fields: Optional[dict[str, Any]] = None,
    ) -> dict:
        ctx = skill_ctx or self._skill_ctx or {}
        skill_name = ctx.get("skill") or "session"
        if custom_fingerprint:
            fp = custom_fingerprint
        else:
            raw_fp = f"{code}|{category}|{skill_name}|{api_error_code}"
            fp = hashlib.sha1(raw_fp.encode("utf-8")).hexdigest()[:10]
        now_elapsed = self._elapsed_s()

        if fp in self._incidents:
            inc = self._incidents[fp]
            inc["count"] += 1
            inc["last_seen_s"] = now_elapsed
            inc["last_detail"] = detail
            if extra_top_fields:
                inc.update(extra_top_fields)
            Path(inc["path"]).write_text(json.dumps(inc, ensure_ascii=False, indent=2), encoding="utf-8")
            return inc

        self._incident_seq += 1
        inc_id = f"INC-{self._incident_seq:03d}"
        fname = f"{inc_id}-{severity}-{code.lower()}-{skill_name}.json"
        fpath = self.incidents_dir / fname
        tape_data = tape_slice
        if tape_data is None and self.tape is not None:
            tape_data = self.tape.current_skill_slice() or self.tape.recent(30)

        bundle: dict[str, Any] = {
            "id": inc_id,
            "incident_id": inc_id,
            "fingerprint": fp,
            "severity": severity,
            "category": category,  # "bug" | "strategy"
            "code": code,
            "skill": skill_name,
            "title": title,
            "count": 1,
            "first_seen_s": now_elapsed,
            "last_seen_s": now_elapsed,
            "source": self.source,
            "git": self.git,
            "world_snapshot_id": self.world_snapshot_id,
            "run_meta": self.run_meta,
            "detail": detail,
            "last_detail": detail,
            "skill_context": {
                "skill": ctx.get("skill"),
                "args": ctx.get("args"),
                "outcome": outcome,
                "initial_status": ctx.get("initial_status"),
                "initial_inventory": ctx.get("initial_inventory"),
                "before_counts": ctx.get("before_counts"),
            },
            "state_snapshot": state.snapshot(),
            "inventory_fingerprint": _inv_fingerprint(state.inventory) or {},
            "recent_steps": list(self._recent_steps)[-15:],
            "recent_events": list(state.events)[-20:],
            "http_tape": tape_data or [],
            "path": str(fpath),
        }
        if extra_top_fields:
            bundle.update(extra_top_fields)
        fpath.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        self._incidents[fp] = bundle
        return bundle

    def _load_baseline_last_point(self) -> Optional[dict]:
        if not self.baseline_dir:
            return None
        p = self.baseline_dir / "progress_curve.jsonl"
        if not p.exists():
            return None
        lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None

    def write_summary(self) -> str:
        last_pt = self.curve_points[-1] if self.curve_points else {}
        bugs = [x for x in self._incidents.values() if x["category"] == "bug"]
        strats = [x for x in self._incidents.values() if x["category"] == "strategy"]
        bugs.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 9), -x["count"]))
        strats.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 9), -x["count"]))

        reached_str = (
            "、".join(f"`{gid}` (`{t:.1f}s`)" for gid, t in self.goals_reached.items())
            if self.goals_reached
            else "暂无"
        )
        lines = [
            f"# 运行诊断与进度汇总（`{self.source}`）",
            "",
            f"- **Git 版本**：`{self.git['commit']}` (branch `{self.git['branch']}`, dirty=`{self.git['dirty']}`)",
            f"- **世界快照编号**：`{self.world_snapshot_id}` · **运行时长**：`{self._elapsed_s()}s`",
            f"- **死亡/重生**：`{self.deaths_count}` 次"
            + (f"（p50 重生耗时 `{statistics.median(self.respawn_ms_samples):.0f}ms`，推断 `keepInventory={self.keep_inventory_inferred}`）"
               if self.respawn_ms_samples else ""),
            f"- **服务端确认进度**：`{last_pt.get('server_advancements', 0)}` 项（v2 分值 `{last_pt.get('server_score_v2', 0)}`）",
            f"- **背包里程碑代理**：`{len(self.inferred_milestones)}` 项 `{sorted(self.inferred_milestones)}`（代理分值 `{last_pt.get('inferred_score_v2', 0)}`）",
            f"- **台账目标达成**：`{len(self.goals_reached)}` 项（{reached_str}）",
        ]
        for gid in self.target_goals:
            label = "本轮聚焦目标" if len(self.target_goals) == 1 else f"本轮聚焦目标（同轮 {len(self.target_goals)} 个）"
            if gid in self.goals_reached:
                lines.append(f"- 🎯 **{label}**：`{gid}` -> ✅ 已达成 (`{self.goals_reached[gid]}s`)")
            else:
                lines.append(f"- 🎯 **{label}**：`{gid}` -> ❌ 未达成")
        base_pt = self._load_baseline_last_point()
        if base_pt is not None:
            lines.append(
                f"- **基线对比（vs `{self.baseline_dir.name}`）**：服务端分 `{last_pt.get('server_score_v2', 0)} vs {base_pt.get('server_score_v2', 0)}`，"
                f"里程碑代理分 `{last_pt.get('inferred_score_v2', 0)} vs {base_pt.get('inferred_score_v2', 0)}`"
            )
        if self.llm_latencies_ms:
            s_lat = sorted(self.llm_latencies_ms)
            total_calls = len(s_lat)
            ok_calls = total_calls - self.llm_fallbacks
            ok_pct = ok_calls / total_calls * 100.0
            p95 = s_lat[min(len(s_lat) - 1, int(len(s_lat) * 0.95))]
            lines.append(
                f"- **LLM 决策遥测**：调用 `{total_calls}` 次，成功率 `{ok_calls}/{total_calls}` (`{ok_pct:.1f}%`)，"
                f"p50=`{statistics.median(s_lat):.0f}ms`，p95=`{p95:.0f}ms`，max=`{max(s_lat):.0f}ms`，"
                f"回退规则次数=`{self.llm_fallbacks}`"
            )
        if self.decision_sources or self.step_sources:
            lines.append(
                f"- **决策来源占比**：选技能 {_format_source_breakdown(self.decision_sources, '次')}；"
                f"动作步 {_format_source_breakdown(self.step_sources, '步')}"
            )

        elapsed_now = self._elapsed_s()
        val = compute_token_valuation(self.prompt_tokens, self.completion_tokens, elapsed_now)
        budget_cfg = _nonneg_int(os.environ.get("LLM_TOKEN_BUDGET", "0"))
        lines += [
            "",
            "## Token 花费",
            f"- **累计用量**：输入 `{val['prompt_tokens']:,}` · 输出 `{val['completion_tokens']:,}` · "
            f"合计 `{val['total_tokens']:,}` tokens（`llm_call` `{len(self.llm_latencies_ms)}` 次）",
            f"- **初赛公告额度占比**（输入 ÷ 800 万 / 输出 ÷ 150 万）："
            f"输入 `{val['in_quota_pct']:.3f}%` · 输出 `{val['out_quota_pct']:.3f}%`",
        ]
        if val["has_8h"] and val["total_tokens"] > 0:
            lines.append(
                f"- **8 小时满场推算**（按当前 `{elapsed_now:.1f}s` 外推 28,800s）："
                f"预计输入 `{val['proj_8h_in']:,}`（占 800 万额度的 `{val['proj_8h_in_pct']:.1f}%`）· "
                f"预计输出 `{val['proj_8h_out']:,}`（占 150 万额度的 `{val['proj_8h_out_pct']:.1f}%`）"
            )
        else:
            lines.append("- **8 小时满场推算**：暂无非零速率样本（纯规则运行或时长为 0s）")
        if budget_cfg > 0:
            lines.append(
                f"- **预算上限（`LLM_TOKEN_BUDGET`）**：`{val['total_tokens']:,} / {budget_cfg:,}` tokens "
                f"(`{val['total_tokens'] / budget_cfg * 100.0:.1f}%`)"
            )
        else:
            lines.append("- **预算上限（`LLM_TOKEN_BUDGET`）**：`0`（仅记账、不熔断）")
        if val["has_price"]:
            price_line = (
                f"- **估算金额**（输入 `{val['price_in_per_m']:g}`/M · 输出 `{val['price_out_per_m']:g}`/M）："
                f"本轮 `¥{val['cost']:.4f}`"
            )
            if val["has_8h"] and val["total_tokens"] > 0:
                price_line += f" · 8 小时推算 `¥{val['proj_8h_cost']:.2f}`"
            lines.append(price_line)

        lines += ["", "## 一、代码与安全缺陷榜（`category = bug`，优先修）"]
        if not bugs:
            lines.append("- 暂无捕获的代码或安全契约缺陷。")
        for b in bugs:
            lines.append(f"- **[{b['severity']}] `{b['id']}` `{b['code']}`**（×{b['count']}）：{b['title']} → `{Path(b['path']).name}`")

        lines += ["", "## 二、策略瓶颈与震荡榜（`category = strategy`）"]
        if not strats:
            lines.append("- 暂无捕获的策略卡死或重复震荡。")
        for s in strats:
            lines.append(f"- **[{s['severity']}] `{s['id']}` `{s['code']}`**（×{s['count']}）：{s['title']} → `{Path(s['path']).name}`")

        lines += ["", "## 三、各技能执行统计", "", "| 技能 | 启动 | 完成 | Replan | 预算中止 | Bug |", "|---|---:|---:|---:|---:|---:|"]
        for sname, st in sorted(self.skill_stats.items()):
            lines.append(f"| `{sname}` | {st['started']} | {st['ok']} | {st['replan']} | {st['budget_stopped']} | {st['bug']} |")

        if self.env_variance_counts:
            lines += ["", "## 四、已知环境波动计数（仅统计，不入缺陷榜）"]
            for k, cnt in sorted(self.env_variance_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"- `{k}`: {cnt} 次")

        text = "\n".join(lines) + "\n"
        self.summary_path.write_text(text, encoding="utf-8")
        return text
