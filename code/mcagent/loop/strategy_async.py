"""战略层异步预取与技能级模型选择实现（纯搬移自 policy.py，与主控制流解耦）。

包含两部分：
1. `--strategy llm` 下的非阻塞后台派发与边界轮询（`dispatch_async_objective`、`dispatch_async_skill_fallback`、
   `poll_async_scheduler`、`invalidate_pending_async`）；
2. 兼容 `--strategy rule --llm` 的同步技能菜单选择（`select_legacy_skill_llm`）。
"""
from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Any, Optional

from ..planner import objective, skill_choice
from ..planner.llm import LLMError
from ..planner.objective import Objective
from ..runtime import State
from ..skills import Skill
from .goals import is_goal_achieved

if TYPE_CHECKING:
    from .policy import CampaignPlanner, PlanDecision
    from .strategy import StrategyOrchestrator


def clone_state_for_worker(state: State) -> State:
    """为后台预取线程复制一份只读世界状态快照，避免与主线程技能步并发读写竞争。"""
    snap = State(state.config)
    snap.st = state.snapshot()
    snap.inventory = copy.deepcopy(state.inventory)
    snap.inventory_dirty = state.inventory_dirty
    snap.safe_mode_until = state.safe_mode_until
    snap.status_at = state.status_at
    snap.deaths = state.deaths.copy()
    return snap


def invalidate_pending_async(orch: "StrategyOrchestrator", reason: str) -> None:
    if orch.scheduler is None:
        return
    old = orch.scheduler.invalidate()
    if old is not None and (old.get("meta") or {}).get("kind") == "objective":
        meta = old.get("meta") or {}
        orch.emit_objective_telemetry(
            trigger=str(meta.get("trigger") or "invalidated"),
            source="llm_fallback_rule",
            obj=orch.current_objective,
            ms=0.0,
            errors=[f"异步结果因状态突变作废: {reason}"],
            adopted=False,
            replaced_goal_id=None,
        )


def dispatch_async_objective(
    orch: "StrategyOrchestrator",
    state: State,
    *,
    trigger: str,
    prev_objective: Optional[Objective],
    prev_elapsed_s: float,
    now: float,
) -> bool:
    p = orch.planner
    if orch.scheduler is None or p.llm is None:
        return False
    orch.scheduler.config = state.config
    ctx = orch.build_context(
        state, current_objective=prev_objective, current_objective_elapsed_s=prev_elapsed_s, now=now
    )
    if not ctx.eligible_goals:
        return False
    rendered_ctx = ctx.render()
    state_snap = clone_state_for_worker(state)
    reservation_snap = copy.deepcopy(p.reservations)
    completed_snap = set(p.rules.completed_advancements)
    fails_snap = dict(p.rules.ep_goal_fails)
    elapsed_snap = dict(p.rules.ep_goal_elapsed)
    blocks_snap = {
        k: type(v)(v.error, v.observation) for k, v in p.rules._environment_blocks.items()
    }
    timeout_budget = max(0.05, float(state.config.model_timeout_s))
    llm_client = p.llm

    def _worker_ask(prompt_text: str) -> dict[str, Any]:
        t0 = orch.clock()
        usage: dict[str, int] = {}
        chosen_obj, errs, used = objective.choose_objective(
            llm_client,
            prompt_text,
            state_snap,
            ledger=p.rules.ledger,
            registry=p.registry,
            min_verified=p.min_verified,
            include_stages=p.rules.include_stages,
            focus_goals=p.target_goal,
            completed_advs=completed_snap,
            ep_goal_fails=fails_snap,
            ep_goal_elapsed=elapsed_snap,
            telemetry=p.rules.telemetry,
            environment_blocks=blocks_snap,
            reservation_book=reservation_snap,
            attempts=2,
            thinking=False,
            total_timeout=timeout_budget,
            usage_out=usage,
        )
        dt_ms = round(max(0.0, orch.clock() - t0) * 1000.0, 1)
        return {
            "raw_choice": (
                {
                    "goal_id": chosen_obj.goal_id,
                    "budget_s": chosen_obj.budget_s,
                    "why": chosen_obj.why,
                    **({"route_branch": chosen_obj.route_branch} if chosen_obj.route_branch else {}),
                }
                if chosen_obj is not None
                else None
            ),
            "errors": list(errs),
            "attempts": used,
            "ms": dt_ms,
            "usage": usage,
        }

    return orch.scheduler.notify(
        "objective",
        rendered_ctx,
        iron_complete=orch.is_iron_complete(state),
        meta={"kind": "objective", "trigger": trigger},
        ask_override=_worker_ask,
    )


def dispatch_async_skill_fallback(orch: "StrategyOrchestrator", state: State, recent_failures: list[str]) -> bool:
    from .policy import build_state_summary

    p = orch.planner
    if orch.scheduler is None or p.llm is None:
        return False
    ready_registry = {
        name: cls for name, cls in p.registry.items()
        if p.rules.environment_block(cls.goal) is None
    }
    if not skill_choice.has_ready_skill(state, ready_registry, p.min_verified):
        return False
    orch.scheduler.config = state.config
    state_snap = clone_state_for_worker(state)
    summary = build_state_summary(state_snap)
    fail_str = "\n".join(f"- {x}" for x in recent_failures[-5:])
    timeout_budget = max(0.05, float(state.config.model_timeout_s))
    llm_client = p.llm

    def _worker_skill(_prompt: str) -> dict[str, Any]:
        t0 = orch.clock()
        usage: dict[str, int] = {}
        errors: list[str] = []
        used = 0
        chosen: Optional[Skill] = None
        try:
            chosen, errors, used = skill_choice.choose(
                llm_client, summary, state_snap, recent_failures=fail_str,
                registry=ready_registry, min_verified=p.min_verified,
                attempts=2, thinking=False, total_timeout=timeout_budget, usage_out=usage,
            )
        except LLMError as exc:
            errors = [f"LLMError: {exc}"]
            used = max(1, used)
        dt_ms = round(max(0.0, orch.clock() - t0) * 1000.0, 1)
        return {
            "choice": {"skill": chosen.name, "args": dict(chosen.args), "why": "战术兜底"} if chosen else None,
            "errors": list(errors),
            "attempts": used,
            "ms": dt_ms,
            "usage": usage,
        }

    return orch.scheduler.notify(
        "uncovered",
        summary,
        iron_complete=orch.is_iron_complete(state),
        meta={"kind": "skill_fallback", "trigger": "no_tactical_skill"},
        ask_override=_worker_skill,
    )


def poll_async_scheduler(orch: "StrategyOrchestrator", state: State, now: float) -> Optional["PlanDecision"]:
    """在技能边界非阻塞检查 `DecisionScheduler` 是否已有完成的战略目标或兜底技能。"""
    from .policy import PlanDecision

    p = orch.planner
    if orch.scheduler is None:
        return None
    polled = orch.scheduler.poll_detailed()
    if polled is None or polled["status"] == "running":
        return None

    meta = polled.get("meta") or {}
    kind = meta.get("kind")
    trigger = str(meta.get("trigger") or "async")
    status = polled["status"]

    if kind == "objective":
        if status != "ready" or not isinstance(polled["value"], dict):
            err_msg = polled.get("error") or f"异步目标决策未就绪 ({status})"
            orch.emit_objective_telemetry(
                trigger=trigger, source="llm_fallback_rule", obj=orch.current_objective,
                ms=float(polled.get("elapsed_ms") or 0.0), errors=[str(err_msg)], adopted=False,
            )
            return None

        payload = polled["value"]
        usage = payload.get("usage") or {}
        p_tok = int(usage.get("prompt_tokens", 0))
        c_tok = int(usage.get("completion_tokens", 0))
        dt_ms = float(payload.get("ms") or polled.get("elapsed_ms") or 0.0)
        raw_choice = payload.get("raw_choice")
        if raw_choice is None:
            orch.emit_objective_telemetry(
                trigger=trigger, source="llm_fallback_rule", obj=orch.current_objective,
                ms=dt_ms, prompt_tokens=p_tok, completion_tokens=c_tok,
                errors=payload.get("errors") or ["模型未产出合法 Objective"], adopted=False,
            )
            return None

        validated, boundary_errs = objective.resolve_objective(
            raw_choice, state, ledger=p.rules.ledger, registry=p.registry,
            min_verified=p.min_verified, include_stages=p.rules.include_stages,
            focus_goals=p.target_goal, completed_advs=p.rules.completed_advancements,
            ep_goal_fails=p.rules.ep_goal_fails, ep_goal_elapsed=p.rules.ep_goal_elapsed,
            telemetry=p.rules.telemetry, environment_blocks=p.rules._environment_blocks,
            reservation_book=p.reservations,
            source="llm", now=now,
        )
        if validated is None:
            orch.emit_objective_telemetry(
                trigger=trigger, source="llm_fallback_rule", obj=orch.current_objective,
                ms=dt_ms, prompt_tokens=p_tok, completion_tokens=c_tok, errors=boundary_errs, adopted=False,
            )
            return None

        old_gid = orch.current_objective.goal_id if orch.current_objective is not None else None
        if orch.current_objective is not None and old_gid != validated.goal_id:
            orch.record_objective_history(
                orch.current_objective, outcome="switched", failure_kind="none", now=now, detail="采纳模型目标"
            )
        orch.apply_objective(validated, now=now)
        orch.emit_objective_telemetry(
            trigger=trigger, source="llm", obj=validated, ms=dt_ms,
            prompt_tokens=p_tok, completion_tokens=c_tok, adopted=True, replaced_goal_id=old_gid,
        )
        return None

    if kind == "skill_fallback" and status == "ready" and isinstance(polled["value"], dict):
        payload = polled["value"]
        usage = payload.get("usage") or {}
        p_tok = int(usage.get("prompt_tokens", 0))
        c_tok = int(usage.get("completion_tokens", 0))
        dt_ms = float(payload.get("ms") or polled.get("elapsed_ms") or 0.0)
        choice_obj = payload.get("choice")
        ready_registry = {
            name: cls for name, cls in p.registry.items()
            if p.rules.environment_block(cls.goal) is None
        }
        chosen_skill: Optional[Skill] = None
        errs: list[str] = list(payload.get("errors") or [])
        if isinstance(choice_obj, dict):
            ok_choice, v_errs = skill_choice.validate_choice(choice_obj, state, ready_registry, p.min_verified)
            if ok_choice:
                chosen_skill = skill_choice.from_choice(choice_obj, state, ready_registry, p.min_verified)
            else:
                errs.extend(v_errs)
        if p.on_llm_telemetry is not None:
            cum_p = int(p.llm.stats.get("prompt_tokens", 0)) if p.llm else 0
            cum_c = int(p.llm.stats.get("completion_tokens", 0)) if p.llm else 0
            p.on_llm_telemetry({
                "ms": dt_ms, "ok": chosen_skill is not None,
                "skill": chosen_skill.name if chosen_skill else None,
                "args": chosen_skill.args if chosen_skill else None,
                "attempts": int(payload.get("attempts") or 0), "errors": errs,
                "prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok,
                "cumulative_prompt_tokens": cum_p, "cumulative_completion_tokens": cum_c,
                "cumulative_total_tokens": cum_p + cum_c,
            })
        if chosen_skill is not None:
            return PlanDecision(
                chosen_skill, "llm", f"战术层无技能，LLM 兜底选择 {chosen_skill}",
                dt_ms, int(payload.get("attempts") or 1), tuple(errs), p_tok, c_tok,
                objective=orch.current_objective,
                objective_source=orch.current_objective.source if orch.current_objective else None,
            )
    return None


def select_legacy_skill_llm(planner: "CampaignPlanner", state: State, recent_failures: list[str]) -> "PlanDecision":
    """`--strategy rule --llm` 保留原有同步技能菜单路径。"""
    from .goals import check_item_expr, normalize_goal_ids
    from .policy import PlanDecision, build_state_summary
    from .starter_skills import total_cobble, total_logs, total_planks

    if planner.llm is None or state.in_safe_mode:
        dec = planner.rules.choose(state)
        planner._last_selected_goal_id = planner.rules._last_selected_goal_id
        return dec
    if (urgent := planner.rules.choose_survival_or_safe_mode(state)) is not None:
        planner._last_selected_goal_id = None
        return urgent
    planner.rules.refresh_environment_blocks(state)
    if planner.rules.targets_exhausted(state):
        planner._last_selected_goal_id = None
        return PlanDecision(
            None, "idle", f"聚焦目标 {planner.target_goal} 已触及本轮止损，结束无效循环", stop_episode=True,
        )
    ready_registry = {
        name: cls for name, cls in planner.registry.items()
        if planner.rules.environment_block(cls.goal) is None
    }
    if planner.target_goal:
        ranked = planner.rules._rank(state)
        if ranked:
            top_g = ranked[0].step_goal
            explicit_target_skills = {
                planner.rules.ledger_map[gid].skill
                for gid in normalize_goal_ids(planner.target_goal)
                if gid in planner.rules.ledger_map
            }
            wants_wood = (
                "gather_wood" in explicit_target_skills
                or top_g.skill == "gather_wood"
                or any(
                    fb_s == "gather_wood" and not check_item_expr(state, expr)
                    for expr, fb_s, _ in top_g.gather_fallbacks
                )
            )
            wants_stone = (
                "mine_stone" in explicit_target_skills
                or top_g.skill == "mine_stone"
                or any(
                    fb_s == "mine_stone" and not check_item_expr(state, expr)
                    for expr, fb_s, _ in top_g.gather_fallbacks
                )
            )
            saturated_skills: list[str] = []
            if not wants_wood and (total_logs(state) * 4 + total_planks(state)) >= 64:
                saturated_skills.append("gather_wood")
            if not wants_stone and total_cobble(state) >= 48:
                saturated_skills.append("mine_stone")
            for skill_name in saturated_skills:
                if skill_name in ready_registry:
                    candidate_registry = {
                        name: cls for name, cls in ready_registry.items()
                        if name != skill_name
                    }
                    if skill_choice.has_ready_skill(state, candidate_registry, planner.min_verified):
                        ready_registry = candidate_registry
    if not skill_choice.has_ready_skill(state, ready_registry, planner.min_verified):
        dec = planner.rules.choose(state)
        planner._last_selected_goal_id = planner.rules._last_selected_goal_id
        return dec

    goal_hint = planner.rules.current_goal_hint(state)
    summary = build_state_summary(state, goal_hint=goal_hint)
    fail_str = "\n".join(f"- {x}" for x in recent_failures[-5:])
    t0 = time.monotonic()
    errors: list[str] = []
    used = 0
    chosen: Optional[Skill] = None
    usage: dict[str, int] = {}
    try:
        chosen, errors, used = skill_choice.choose(
            planner.llm, summary, state, recent_failures=fail_str,
            registry=ready_registry, min_verified=planner.min_verified,
            attempts=2, thinking=False, total_timeout=state.config.model_timeout_s * 2, usage_out=usage,
        )
    except LLMError as e:
        errors = [f"LLMError: {e}"]
        used = max(1, used)
    if chosen is not None and planner.rules.environment_block(chosen.goal):
        errors.append(f"{chosen.goal} 环境条件尚未变化")
        chosen = None
    dt_ms = round((time.monotonic() - t0) * 1000.0, 1)
    prompt_tok = int(usage.get("prompt_tokens", 0))
    comp_tok = int(usage.get("completion_tokens", 0))
    total_tok = int(usage.get("total_tokens", prompt_tok + comp_tok))
    cum_prompt = int(usage.get("cumulative_prompt_tokens", planner.llm.stats.get("prompt_tokens", 0)))
    cum_comp = int(usage.get("cumulative_completion_tokens", planner.llm.stats.get("completion_tokens", 0)))
    cum_total = int(usage.get("cumulative_total_tokens", cum_prompt + cum_comp))

    if planner.on_llm_telemetry is not None:
        planner.on_llm_telemetry({
            "ms": dt_ms, "ok": chosen is not None,
            "skill": chosen.name if chosen else None, "args": chosen.args if chosen else None,
            "attempts": used, "errors": list(errors),
            "prompt_tokens": prompt_tok, "completion_tokens": comp_tok, "total_tokens": total_tok,
            "cumulative_prompt_tokens": cum_prompt, "cumulative_completion_tokens": cum_comp,
            "cumulative_total_tokens": cum_total,
        })

    if chosen is not None:
        matched_gid: Optional[str] = None
        ranked = planner.rules._rank(state)
        for step in ranked:
            g = step.step_goal
            if g.skill == chosen.name and (not g.args_dict or g.args_dict == chosen.args):
                matched_gid = g.id
                break
            if any(fb_s == chosen.name for _, fb_s, _ in g.gather_fallbacks):
                matched_gid = g.id
                break
        if matched_gid is None:
            for g in planner.rules.active_goals:
                if g.skill == chosen.name and not is_goal_achieved(g, state, planner.rules.completed_advancements):
                    matched_gid = g.id
                    break
        planner._last_selected_goal_id = matched_gid
        planner.rules._last_selected_goal_id = matched_gid
        return PlanDecision(
            chosen, "llm", f"LLM 选择 {chosen}", dt_ms, used, tuple(errors), prompt_tok, comp_tok,
            environment_recovery=(
                chosen.name == "scout_island" and not ranked
                and bool(planner.rules.blocked_targets(state, planner.target_goal, refresh=False))
            ),
        )

    fallback = planner.rules.choose(state)
    planner._last_selected_goal_id = planner.rules._last_selected_goal_id
    return PlanDecision(
        fallback.skill, "llm_fallback_rule" if fallback.skill else "idle",
        f"LLM 未产出合法选择（{'; '.join(errors[:2])}）-> 规则兜底: {fallback.reason}",
        dt_ms, used, tuple(errors), prompt_tok, comp_tok,
        environment_recovery=fallback.environment_recovery,
        stop_episode=fallback.stop_episode,
    )
