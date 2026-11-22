"""T1 obsidian acquisition and Nether portal entry."""
from __future__ import annotations

from ..runtime import Replan
from .base import Param, Skill, rec
from .interaction import use_item_on
from .nether_mining import _durable_pick, _gather_flint, _harvest_obsidian, _take_available
from .nether_portal import (
    Entry, _choose_build_plan, _find_activated_portal, _material_scaffold, build_pattern,
    nether_preflight_missing, traverse_portal, verify_portal_landing,
)
from .nether_site import (
    OBSIDIAN, PORTAL, _cell, _dimension, _ground, _name, _near, _pos, _standing, _walk,
)
from .observations import completed_advancements
from .observations import refresh_inventory as _inventory
from .registry import register
from .spawn_shelter import _blast_block_count, build_nether_bunker_flow
from .world_memo import memo_for


def cast_obsidian_cell(ex, state, pos):
    """Reserved for P8 coverage of observed water and lava casting."""
    raise Replan("浇铸门框等待靶场验证")


def _ignite_with_lava(ex, state, pos):
    """Reserved for P8 coverage of ignition by lava."""
    raise Replan("岩浆引燃门框等待靶场验证")


@register
class MineObsidian(Skill):
    name = "mine_obsidian"
    description = "先找现成黑曜石，再复核液体和安全站位后采收"
    params = {"count": Param(int, "本轮目标持有的普通黑曜石数量", default=1, min=1, max=16)}
    goal = "acq:obsidian"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if state.count(OBSIDIAN):
            return ""
        if not state.ready or _dimension(state.st) not in {"overworld", "nether"}:
            return "需先确认当前维度和状态，才能找可及容器或安全矿面"
        return ""

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if state.count(OBSIDIAN) >= args.get("count", 1) or not (pick := _durable_pick(state)):
            return ()
        water = next((item for item in ("water_bucket", "bucket") if state.count(item)), None)
        if water and _dimension(state.st) == "overworld":
            return ((pick, 1, "borrow"), (water, 1, "borrow"))
        return ((pick, 1, "borrow"),)

    def run(self, ex, state):
        _inventory(ex, state)
        count = self.args["count"]
        if state.count(OBSIDIAN) >= count:
            return
        status = state.refresh_status(ex.client, max_age_s=0)
        dimension = _dimension(status)
        if dimension not in {"overworld", "nether"} or not _standing(ex, status):
            raise Replan("先确认主世界或下界安全站位，再查黑曜石来源")
        yield from _take_available(ex, state, OBSIDIAN, count, dimension)
        if state.count(OBSIDIAN) < count:
            yield from _harvest_obsidian(ex, state, count, dimension)


@register
class BuildAndEnterPortal(Skill):
    name = "build_and_enter_portal"
    description = "复用活动门或补齐最小门框，进入下界后停步核验"
    params = {
        "prepare_flint": Param(bool, "仅补一份燧石供打火石制作", default=False),
        "allow_casting": Param(bool, "浇铸备选开关，待实测", default=False),
        "allow_lava_ignition": Param(bool, "岩浆引火备选开关，待实测", default=False),
        "strict_preflight": Param(bool, "跨门进下界前是否强制执行七项行前检查清单", default=False),
        "auto_bunker": Param(bool, "落地后是否立即建 3x3 圆石防恶魂碉堡", default=False),
    }
    goal = "route:nether:prepare_flint={prepare_flint}"
    verified = "offline"

    @classmethod
    def blocked(cls, state):
        if not state.ready:
            return "需先等待世界区块和实体就绪（ready=true）"
        if _dimension(state.st) == "nether":
            return ""
        if _dimension(state.st) != "overworld":
            return "需先确认主世界站位，才能查可用下界门"
        return ""

    @classmethod
    def material_plan(cls, args: dict, state) -> tuple[tuple[str, int, str], ...]:
        if args.get("prepare_flint") or _dimension(state.st) == "nether":
            return ()
        # Pure State has no frame snapshot. Reserve the stock needed by the default new-frame
        # route up to its limit, and let the scheduler reconcile the actually observed route.
        count = min(10, state.count(OBSIDIAN))
        materials = [(OBSIDIAN, count, "consume")] if count else []
        if scaffold := _material_scaffold(state, count):
            materials.append((scaffold, 1, "borrow"))
        if state.count("flint_and_steel"):
            materials.append(("flint_and_steel", 1, "borrow"))
        return tuple(materials)

    def run(self, ex, state):
        _inventory(ex, state)
        if self.args["prepare_flint"] and state.count("flint"):
            return
        status = state.refresh_status(ex.client, max_age_s=0)
        if status.get("ready") is not True:
            raise Replan("世界加载尚未就绪（ready=false），请等新世界区块和实体到齐后再重试")
        if _dimension(status) == "nether" and not self.args["prepare_flint"]:
            landing = verify_portal_landing(ex, state)
            if isinstance(landing, dict) and landing.get("pos"):
                memo_for(state).record_active_portal(landing["pos"], dimension="the_nether")
            return
        if _dimension(status) != "overworld" or not _standing(ex, status, through_portal=True):
            raise Replan("未确认主世界的安全站位，请先离开流体或危险边缘")
        if self.args["prepare_flint"]:
            yield from _gather_flint(ex, state, "overworld")
            return
        if self.args.get("strict_preflight"):
            missing = nether_preflight_missing(state)
            if missing:
                raise Replan(f"下界远征行前检查未通过，缺少：{', '.join(missing)}")
        start = _cell(_pos(status))
        entry = _find_activated_portal(ex, state, start)
        if entry is None:
            entry = yield from self._prepare_portal(ex, state, start)
        landing = yield from traverse_portal(ex, state, entry)
        memo = memo_for(state)
        if isinstance(landing, dict) and landing.get("pos"):
            memo.record_active_portal(landing["pos"], dimension="the_nether")
        yield rec("portal_landing", landing, source_dimension="overworld", source_portal=list(entry.foot),
                  advancement_confirmed="story/enter_the_nether" in completed_advancements(ex, require_ready=False))
        if self.args.get("auto_bunker") and _blast_block_count(state) >= 20:
            yield from build_nether_bunker_flow(ex, state, memo, skill_name=self.name)

    def _prepare_portal(self, ex, state, start):
        if self.args["allow_casting"]:
            cast_obsidian_cell(ex, state, start)
        plan = _choose_build_plan(ex, state, start)
        if not state.count("flint_and_steel"):
            if self.args["allow_lava_ignition"]:
                _ignite_with_lava(ex, state, plan.entry.foot)
            if not state.count("flint"):
                yield from _gather_flint(ex, state, "overworld")
            raise Replan('需要打火石，请用 craft_iron_gear(item="flint_and_steel")，配方需 1 铁锭与 1 燧石')
        yield from _walk(ex, state, plan.entry.route, "overworld")
        stand = plan.entry.route[-1] if plan.entry.route else start
        yield from build_pattern(ex, state, plan.frame, stand=stand)
        status = state.refresh_status(ex.client, max_age_s=0)
        sill = _ground(plan.entry.foot)
        if (_dimension(status) != "overworld" or not _standing(ex, status)
                or _cell(_pos(status)) != stand or _name(ex, sill) != OBSIDIAN
                or not _near(_pos(status), sill)):
            raise Replan("点火前未确认可安全触达门内底部黑曜石")
        # §4.4 does not specify which face the backend chooses. Confirm the actual portal block.
        yield from use_item_on(ex, state, list(sill), "flint_and_steel")
        if _name(ex, plan.entry.foot) != PORTAL:
            raise Replan("点火回执未产生可观察的 nether_portal，停止进门")
        return Entry(plan.entry.foot, ())
