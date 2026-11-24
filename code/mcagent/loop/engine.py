"""统一比赛主循环（兼顾 8 小时正式赛与赛前持续挂机压测）。

设计原则（评审修订 #1 / #4）：
- 测试循环就是正式赛主循环：`登录就绪 -> 选技能 -> 一步一动作推进 -> 死亡/断线自愈 -> 继续`，不维护两套代码。
- 死亡重生后不主动发 `POST /posture`（规范 §4.11 服务端自动复位，`Client.status()` 同步本地缓存）。
- 重生后的阶段衔接完全交给 `CampaignPlanner` 从刷新后的真实背包与环境推导。
- `SoakObserver` 通过观察者接口挂接，关闭时零额外开销，开启时实时产出流水、异常包与进度曲线。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from ..client import FATAL, ApiError, Client
from ..runtime import EnvBlocked, Executor, Fatal, Replan, Safety, SafetyMonitor, State, _inv_fingerprint, has_equipped_armor
from ..skills.base import Skill, failure
from ..skills.items import bare
from ..skills.ore_ledger import ores_for_dimension, record_ore_observations
from ..skills.world_memo import STRUCTURE_FINGERPRINTS, entity_landmark_tags, memo_for, structure_tags_for
from .auditor import SoakObserver
from .goals import GOAL_MAP, GoalSpec, is_goal_achieved, normalize_goal_ids
from .policy import CampaignPlanner, PlanDecision


@dataclass
class LoopConfig:
    mode: str = "campaign"
    max_steps: int = 0            # 0 = 不限步数（按时间或信号停止）
    max_seconds: float = 0.0      # 0 = 不限时（正式赛可设 28800 = 8h）
    max_deaths: int = 0           # 0 = 不限死亡次数（持续自愈重来）
    stop_on_death: bool = False   # True = 检测到真实死亡时记录死亡上下文并立即干净退出
    idle_sleep_s: float = 0.2
    respawn_poll_s: float = 0.1
    respawn_timeout_s: float = 90.0
    enable_monitor: bool = True
    disconnect_on_exit: bool = False
    target_goal: Optional[str] = None
    stop_on_goal: bool = False
    # 读状态遇到网关抖动（5xx / 网络 / 超时 / 服务端 internal 等非致命错误）时的自愈参数
    transient_retries: int = 3         # 单拍内重试次数，用尽则跳过这一拍
    transient_backoff_s: float = 1.0   # 首次退避秒数，此后翻倍
    transient_backoff_max_s: float = 15.0
    outage_limit_s: float = 600.0      # 连续故障超过此时长才停机；0 = 永不因此停机
    continuous_observe: bool = True
    async_observe_for_llm: bool = True
    checkpoint_path: Optional[str] = None
    failure_cooldown_cap_s: Optional[float] = None
    enable_roadside_scan: bool = True
    roadside_scan_interval_s: float = 5.0
    roadside_scan_radius: int = 48
    roadside_scan_limit: int = 64
    roadside_scan_move_dist: float = 8.0


# 会话级错误：交给 `_recover_session_or_respawn` 处理，而不是按瞬时故障退避
SESSION_CODES: frozenset[str] = frozenset({"dead", "not_connected", "wrong_server", "death_loop_cooldown"})

# 沿途低频增量扫描：按维度分组的结构指纹标签与高价值机会实体白名单
_ROADSIDE_DIM_STRUCTURES: dict[str, tuple[str, ...]] = {
    "overworld": (
        "village", "stronghold", "ruined_portal", "bee_nest", "trial_chambers",
        "deep_dark", "archaeology", "powder_snow", "lush_caves",
        "amethyst_geode", "pale_garden", "pillager_outpost",
        "meadow", "swamp", "mangrove_swamp",
    ),
    "the_nether": (
        "nether_fortress", "bastion_remnant", "ruined_portal", "dried_ghast",
    ),
    "the_end": (
        "end_city",
    ),
}
_ROADSIDE_OPPORTUNITY_ENTITIES: tuple[str, ...] = (
    "wandering_trader",
    "zombie_villager",
    "villager",
    "pillager",
    "allay",
    "breeze",
    "axolotl",
    "goat",
    "tadpole",
    "frog",
    "armadillo",
    "shulker",
    "parrot",
    "ghast",
    "blaze",
    "wither_skeleton",
    "piglin_brute",
)
_ROADSIDE_OPPORTUNITY_ENTITY_SET: frozenset[str] = frozenset(_ROADSIDE_OPPORTUNITY_ENTITIES)


_NON_BLOCK_ROADSIDE_NAMES: dict[str, str] = {
    "glow_berries": "cave_vines_plant",
}


def _roadside_structure_blocks(dimension: str) -> list[str]:
    dim = bare(dimension).strip().lower() or "overworld"
    if "nether" in dim:
        bucket = "the_nether"
    elif "end" in dim:
        bucket = "the_end"
    else:
        bucket = "overworld"
    blocks: list[str] = []
    seen: set[str] = set()
    for tag in _ROADSIDE_DIM_STRUCTURES.get(bucket, ()):
        for bname in STRUCTURE_FINGERPRINTS.get(tag, ()):
            eff = _NON_BLOCK_ROADSIDE_NAMES.get(bname, bname)
            if eff and eff not in seen:
                seen.add(eff)
                blocks.append(eff)
    for bname in ores_for_dimension(dim):
        if bname and bname not in seen:
            seen.add(bname)
            blocks.append(bname)
    return blocks


_NO_PROGRESS_MIN_STEPS = 1          # 一次"完成"里最多走了几步仍算零进度
_NO_PROGRESS_MIN_MOVE = 1.0         # 位移小于该值仍算零进度
_NO_PROGRESS_STREAK_BEFORE_COOLDOWN = 2   # 连续几次才开始冷却（避免误伤首轮）
_NO_PROGRESS_BASE_COOLDOWN_S = 30.0
_NO_PROGRESS_COOLDOWN_CAP_S = 300.0


_RESCUE_OFFSETS: tuple[tuple[int, int, int], ...] = (
    (0, 2, 0),                                    # 头顶两格：夜间"挖三填一"的封口块
    (1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1),  # 脚部四向：自封墙
    (1, 1, 0), (-1, 1, 0), (0, 1, 1), (0, 1, -1),  # 头部四向
)
_AIR_NAMES = frozenset({"", "air", "cave_air", "void_air"})


def rescue_breach_cell(read_block: Callable[[tuple[int, int, int]], Optional[str]], pos: Any) -> Optional[tuple[int, int, int]]:
    """被困自救：返回最该挖掉的那一格（优先头顶封口块，其次脚/头四向），无可挖则 None。

    真机事故（2026-11-24）：bot 夜间挖洞封口后，逃敌技能每次冲撞还能挤出 0.4~0.57 格，
    于是"零位移"判定永不成立、破墙分支永不执行，17 次冲撞都困在原地。
    """
    try:
        fx, fy, fz = (math.floor(float(v)) for v in list(pos)[:3])
    except Exception:
        return None
    for dx, dy, dz in _RESCUE_OFFSETS:
        cell = (fx + dx, fy + dy, fz + dz)
        name = read_block(cell)
        if name is None:
            continue
        lowered = bare(name).strip().lower()
        if lowered in _AIR_NAMES or lowered in ("bedrock", "obsidian", "crying_obsidian", "water", "lava", "flowing_water", "flowing_lava"):
            continue
        return cell
    return None


def note_no_progress(
    book: dict[tuple[str, str], int],
    skill: Any,
    *,
    steps: int,
    moved: Optional[float],
    cap_s: float = _NO_PROGRESS_COOLDOWN_CAP_S,
) -> int:
    """记录"完成但零进度"，返回当前连续次数；达到阈值就给该技能打冷却。

    真机事故（2026-11-24）：`discover_structure` 把"已知地标"当成本轮成果，
    每次启动只走 1 步就自称完成，目标 `follow_ender_eye` 永远不推进，
    20 分钟内被策略重选 1879 次，整轮空转。这里做通用护栏：
    同一 (技能, 目标) 连续零进度完成 >= 2 次后，按 30s→60s→120s… 指数退避（上限 cap）。
    """
    key = (getattr(skill, "name", ""), getattr(skill, "goal", ""))
    if steps <= _NO_PROGRESS_MIN_STEPS and (moved is None or moved < _NO_PROGRESS_MIN_MOVE):
        streak = int(book.get(key, 0)) + 1
        book[key] = streak
        if streak >= _NO_PROGRESS_STREAK_BEFORE_COOLDOWN:
            delay = min(
                float(cap_s),
                _NO_PROGRESS_BASE_COOLDOWN_S * (2 ** (streak - _NO_PROGRESS_STREAK_BEFORE_COOLDOWN)),
            )
            marker = getattr(skill, "mark_failed", None)
            if callable(marker):
                marker(cooldown_s=delay)
        return streak
    book.pop(key, None)
    return 0


@dataclass
class RunResult:
    steps: int
    skills_completed: int
    skills_replan: int
    deaths_recovered: int
    elapsed_s: float
    stop_reason: str
    transient_faults: int = 0          # 因读状态瞬时故障而跳过的拍数


class MainLoop:
    """比赛与持续测试共用的核心控制循环。"""

    def __init__(
        self,
        client: Client,
        state: State,
        executor: Executor,
        planner: CampaignPlanner,
        *,
        config: Optional[LoopConfig] = None,
        observer: Optional[SoakObserver] = None,
        safety: Optional[Safety] = None,
        log=print,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.state = state
        self.executor = executor
        self.planner = planner
        self.config = config or LoopConfig(mode=planner.mode)
        self.observer = observer
        self.safety = safety or Safety(state.config)
        self.log = log
        self.recent_failures: deque[str] = deque(maxlen=12)
        self._path_failures: dict[str, int] = {}
        # 零进度完成护栏：同一 (技能, 目标) 反复"报告完成但没走一步、也没挪窝"的计数。
        # 真机事故（2026-11-24）：discover_structure 在 20 分钟里被启动 1879 次，
        # 每次只做 1 个动作就自称完成，策略立刻重选 —— 整轮空转。
        self._no_progress: dict[tuple[str, str], int] = {}
        # 连续"技能结束时净位移 < 1.5 格"的次数，达到阈值就自救破一格
        self._stuck_streak: int = 0
        self._seen_deaths_len = 0
        self._server_advancements: set[str] = set()
        self._custom_sleep = sleep
        self._sleep = self._do_sleep
        self._clock = clock
        self._outage_since: Optional[float] = None
        self.transient_faults = 0
        # 3 层同位冷却空转脱困状态机（normal -> local_retry -> relocate -> goal_abandoned）
        self._idle_spin_count: int = 0
        self._idle_spin_pos: Optional[tuple[float, float, float]] = None
        self._failure_tier: int = 0
        self._failure_tier_pos: Optional[tuple[float, float, float]] = None
        self.failure_tier_state: str = "normal"
        self.failure_escalations: list[str] = []
        self._relocated_cells: set[tuple[str, int, int]] = set()
        self._checkpoint_achieved_goals: set[str] = set()
        self._last_roadside_scan_at: float = float(self._clock())
        self._last_roadside_scan_pos: Optional[tuple[float, float, float]] = self._pos_tuple(self.state)
        self._last_roadside_scan_dim: Optional[str] = (
            str(self.state.st.get("dimension") or "overworld") if self.state.st else None
        )
        self._unreleased_busy_streak: int = 0

    def _recover_stuck_busy_session(self) -> bool:
        """连续多次 /stop 仍未释放服务端 busy 锁时，执行 /disconnect + 重连自愈，防整场僵死。"""
        disconnect_fn = getattr(self.client, "disconnect", None)
        connect_fn = getattr(self.client, "connect", None)
        wait_fn = getattr(self.client, "wait_ready", None)
        if not (callable(disconnect_fn) and callable(connect_fn) and callable(wait_fn)):
            return False
        self.log(
            f"[loop] 连续 {self._unreleased_busy_streak} 次 /stop 未释放服务端 busy 锁，执行 /disconnect + 重连自愈..."
        )
        try:
            disconnect_fn()
        except Exception as e:
            self.log(f"[loop] 自愈断开忽略异常: {e}")
        try:
            connect_fn(timeout=20)
            st = wait_fn(timeout=60)
            if isinstance(st, dict) and st:
                self.state.set_status(st)
            self.state.invalidate_inventory()
            self.state.refresh_inventory(self.client)
            self.state.drain(self.client)
            self._unreleased_busy_streak = 0
            self.log(f"[loop] busy 死锁重连自愈完成: pos={self.state.pos}")
            return True
        except ApiError as e:
            if e.kind == FATAL:
                raise
            self.log(f"[loop] busy 死锁重连失败，交由下一拍继续恢复: {e}")
            return False
        except Exception as e:
            self.log(f"[loop] busy 死锁重连异常，交由下一拍继续恢复: {e}")
            return False

    def _do_sleep(self, seconds: float) -> None:
        if self._custom_sleep is not None:
            self._custom_sleep(seconds)
        else:
            time.sleep(seconds)

    @staticmethod
    def _pos_tuple(state: State) -> Optional[tuple[float, float, float]]:
        pos = state.st.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) >= 3:
            try:
                vals = (float(pos[0]), float(pos[1]), float(pos[2]))
                if all(math.isfinite(v) for v in vals):
                    return vals
            except (TypeError, ValueError):
                return None
        return None

    def _try_self_rescue_breach(self) -> bool:
        """连续被困时主动挖一格脱困（头顶封口块优先），成功返回 True。"""
        pos = self._pos_tuple(self.state)
        if pos is None:
            return False

        def _read(cell: tuple[int, int, int]) -> Optional[str]:
            try:
                block = self.client.get_block(list(cell))
            except Exception:
                return None
            return bare((block or {}).get("name")) if isinstance(block, dict) else None

        cell = rescue_breach_cell(_read, pos)
        if cell is None:
            self._stuck_streak = 0
            return False
        try:
            self.executor.dig(list(cell))
        except Exception as exc:  # 挖不动就算了，下一拍再试
            self.log(f"[loop] 自救破格 {list(cell)} 失败：{exc}")
            self._stuck_streak = 0
            return False
        self.log(f"[loop] 连续 {self._stuck_streak} 次技能净位移不足，自救破 {list(cell)} 脱困")
        self._stuck_streak = 0
        return True

    @classmethod
    def _cell_key(cls, state: State) -> tuple[str, int, int]:
        pos = cls._pos_tuple(state)
        dim = str(state.st.get("dimension") or "overworld")
        if pos is None:
            return dim, 0, 0
        return dim, math.floor(pos[0] / 4.0), math.floor(pos[2] / 4.0)

    def _reset_failure_tier(self, pos: Optional[tuple[float, float, float]] = None) -> None:
        self._idle_spin_count = 0
        self._idle_spin_pos = pos
        self._failure_tier = 0
        self._failure_tier_pos = pos
        self.failure_tier_state = "normal"

    def _load_checkpoint_if_configured(self) -> None:
        path = self.config.checkpoint_path
        if not path:
            return
        try:
            from ..skills.world_memo import memo_for
            loader = getattr(memo_for(self.state), "load_checkpoint", None)
            if callable(loader):
                loader(path)
        except Exception as e:
            self.log(f"[loop] 加载检查点失败 ({path}): {e}")

    def _save_checkpoint_if_configured(self) -> None:
        path = self.config.checkpoint_path
        if not path:
            return
        try:
            from ..skills.world_memo import memo_for
            saver = getattr(memo_for(self.state), "save_checkpoint", None)
            if callable(saver):
                saver(path)
        except Exception as e:
            self.log(f"[loop] 保存检查点失败 ({path}): {e}")

    def _maybe_save_checkpoint_on_goal_progress(self, *, force: bool = False) -> None:
        if not self.config.checkpoint_path:
            return
        completed_advs = set(self._server_advancements)
        if self.observer is not None:
            completed_advs.update(self.observer.server_advancements)
        if hasattr(self.planner, "rules") and hasattr(self.planner.rules, "completed_advancements"):
            completed_advs.update(self.planner.rules.completed_advancements)
        active = getattr(getattr(self.planner, "rules", None), "active_goals", ()) or ()
        newly_achieved = False
        for g in active:
            if g.id not in self._checkpoint_achieved_goals and is_goal_achieved(g, self.state, completed_advs):
                self._checkpoint_achieved_goals.add(g.id)
                newly_achieved = True
        if force or newly_achieved:
            self._save_checkpoint_if_configured()

    def _record_current_biome(self) -> Optional[str]:
        """零额外 HTTP 成本：直接从当前 `/status` 快照将脚下群系及锚点坐标写入 `WorldResourceMemo`。"""
        if not self.state.ready or self._is_dead_now():
            return None
        st = self.state.st or {}
        raw_biome = st.get("biome")
        if not isinstance(raw_biome, str) or not raw_biome.strip():
            return None
        dim = str(st.get("dimension") or "overworld")
        pos = self._pos_tuple(self.state)
        try:
            if memo_for(self.state).record_biome(raw_biome, pos=pos, dimension=dim):
                return bare(raw_biome).strip().lower()
        except Exception:
            return None
        return None

    def _maybe_roadside_scan(self, force: bool = False) -> list[str]:
        """技能完成边界的沿途增量扫描：满足时间间隔或位移>=8格/切维度时探测附近结构指纹与高价值机会实体。"""
        if not self.config.enable_roadside_scan:
            return []
        if not self.state.ready or self._is_dead_now():
            return []
        now = float(self._clock())
        dim = str(self.state.st.get("dimension") or "overworld")
        cur_pos = self._pos_tuple(self.state)
        if self._last_roadside_scan_pos is None and cur_pos is not None:
            self._last_roadside_scan_pos = cur_pos
            self._last_roadside_scan_dim = dim
        move_dist = max(0.0, float(self.config.roadside_scan_move_dist))
        moved_trigger = False
        if move_dist > 0 and cur_pos is not None and self._last_roadside_scan_pos is not None:
            if self._last_roadside_scan_dim is not None and dim != self._last_roadside_scan_dim:
                moved_trigger = True
            elif math.dist(cur_pos, self._last_roadside_scan_pos) >= move_dist:
                moved_trigger = True
        interval_s = max(0.0, float(self.config.roadside_scan_interval_s))
        raw_rtt = getattr(self.client, "smoothed_rtt_s", 0.0)
        high_rtt = (float(raw_rtt) if isinstance(raw_rtt, (int, float)) and not isinstance(raw_rtt, bool) else 0.0) > 0.25
        if high_rtt:
            # 高 RTT 下原地未移动不靠 5s 定时白发 find_blocks + get_entities；放大静止间隔至 45s
            eff_interval_s = max(interval_s, 45.0)
        else:
            eff_interval_s = interval_s
        if not force and not moved_trigger and (now - self._last_roadside_scan_at) < eff_interval_s:
            return []
        self._last_roadside_scan_at = now
        self._last_roadside_scan_pos = cur_pos
        self._last_roadside_scan_dim = dim

        radius = max(1, int(self.config.roadside_scan_radius))
        limit = max(1, int(self.config.roadside_scan_limit))
        memo = memo_for(self.state)
        newly_recorded: list[str] = []
        if new_biome := self._record_current_biome():
            newly_recorded.append(new_biome)

        find_blocks = getattr(self.client, "find_blocks", None)
        if callable(find_blocks):
            block_types = _roadside_structure_blocks(dim)
            if block_types:
                resp = None
                try:
                    resp = find_blocks(types=block_types, radius=radius, limit=limit)
                except ApiError as e:
                    if e.kind == FATAL:
                        raise
                except Exception:
                    resp = None
                if isinstance(resp, dict):
                    payload = (
                        resp.get("result")
                        if "blocks" not in resp and isinstance(resp.get("result"), dict)
                        else resp
                    )
                    blocks = payload.get("blocks")
                    rows: list[tuple[object, object]] = []
                    if isinstance(blocks, list):
                        for item in blocks:
                            if isinstance(item, dict):
                                rows.append((item.get("name"), item.get("pos")))
                    elif isinstance(blocks, dict):
                        for bname, info in blocks.items():
                            if isinstance(info, dict):
                                rows.append((
                                    bname,
                                    info.get("nearest") if info.get("nearest") is not None else info.get("pos"),
                                ))
                    for raw_name, raw_pos in rows:
                        bname = bare(raw_name).strip().lower() if raw_name else ""
                        if not bname or raw_pos is None:
                            continue
                        for tag in structure_tags_for((bname,)):
                            try:
                                if memo.record_landmark(
                                    tag, raw_pos, dimension=dim,
                                    meta={"block": bname, "source": "roadside_scan"},
                                ):
                                    newly_recorded.append(tag)
                            except Exception:
                                continue
                    memo.ingest_find_blocks(payload, dimension=dim, origin=cur_pos)
                    ore_rows = [
                        {"name": raw_name, "pos": raw_pos}
                        for raw_name, raw_pos in rows
                        if raw_name and raw_pos is not None
                    ]
                    if ore_rows:
                        record_ore_observations(self.state, ore_rows, dimension=dim, origin=cur_pos)

        get_entities = getattr(self.client, "get_entities", None)
        if callable(get_entities):
            resp = None
            try:
                resp = get_entities(radius=radius, types=list(_ROADSIDE_OPPORTUNITY_ENTITIES))
            except ApiError as e:
                if e.kind == FATAL:
                    raise
            except Exception:
                resp = None
            if isinstance(resp, dict):
                payload = (
                    resp.get("result")
                    if "entities" not in resp and isinstance(resp.get("result"), dict)
                    else resp
                )
                entities = payload.get("entities")
                if isinstance(entities, list):
                    for ent in entities:
                        if not isinstance(ent, dict):
                            continue
                        pos = ent.get("pos")
                        if pos is None:
                            continue
                        name = bare(ent.get("name")).strip().lower() if ent.get("name") else ""
                        etype = bare(ent.get("type")).strip().lower() if ent.get("type") else ""
                        kind = (
                            name if name in _ROADSIDE_OPPORTUNITY_ENTITY_SET
                            else (etype if etype in _ROADSIDE_OPPORTUNITY_ENTITY_SET else "")
                        )
                        if not kind:
                            continue
                        try:
                            if memo.record_landmark(
                                kind, pos, dimension=dim,
                                meta={"entity": kind, "source": "roadside_scan"},
                            ):
                                newly_recorded.append(kind)
                        except Exception:
                            continue
                        for mapped_tag in entity_landmark_tags(kind):
                            if mapped_tag == kind:
                                continue
                            try:
                                if memo.record_landmark(
                                    mapped_tag, pos, dimension=dim,
                                    meta={"entity": kind, "source": "roadside_scan"},
                                ):
                                    newly_recorded.append(mapped_tag)
                            except Exception:
                                continue

        if newly_recorded:
            self.log(f"[loop] 沿途增量扫描发现新地标: {', '.join(newly_recorded)}")
        return newly_recorded

    def _acknowledge_safety_intent(self, *, completed: bool) -> None:
        raw = getattr(self.state, "safety_intent", None)
        if raw is None:
            return
        should_clear = completed
        if not should_clear:
            st = self.state.snapshot()
            kind = (
                str(raw.get("kind") or raw.get("intent") or "").strip().lower()
                if isinstance(raw, dict)
                else (raw.strip().lower() if isinstance(raw, str) else "")
            )
            if kind in ("escape_lava", "lava") and not st.get("in_lava"):
                should_clear = True
            elif kind == "douse" and not st.get("on_fire") and not st.get("in_lava"):
                should_clear = True
            elif kind == "surface" and not st.get("in_water") and not st.get("submerged"):
                should_clear = True
        if should_clear:
            setter = getattr(self.state, "set_safety_intent", None)
            if callable(setter):
                setter(None)
            else:
                self.state.safety_intent = None
            if completed:
                clear_hurt = getattr(self.state, "clear_recent_hurt", None)
                if callable(clear_hurt):
                    clear_hurt()

    def _maybe_set_hurt_flee_intent(self, failed_name: str, extra: dict) -> None:
        """非战斗技能受击中断且 18 格内有敌对生物（或刚遭远程箭矢命中）时，立即写入 flee 意图防原地/乱走被点死。"""
        if failed_name == "emergency_escape":
            return
        if extra.get("cause", extra.get("interrupted_by")) != "hurt":
            return
        st = self.state.snapshot()
        hostile = extra.get("nearest_hostile") or st.get("nearest_hostile")
        hname = ""
        hdist = 99.0
        if isinstance(hostile, dict) and hostile:
            try:
                raw_d = float(hostile.get("dist", 99.0))
                if math.isfinite(raw_d):
                    hdist = raw_d
            except (TypeError, ValueError):
                hdist = 99.0
            hname = bare(hostile.get("name") or "").strip().lower()
        if not math.isfinite(hdist) or hdist > 18.0:
            recent_hurt = self.state.get_recent_hurt(max_age_s=4.0) if hasattr(self.state, "get_recent_hurt") else None
            if isinstance(recent_hurt, dict):
                hurt_src = str(recent_hurt.get("source") or "").strip().lower()
                src_ent = recent_hurt.get("source_entity") if isinstance(recent_hurt.get("source_entity"), dict) else {}
                src_name = bare(src_ent.get("name") or "").strip().lower()
                if hurt_src == "arrow" or src_name:
                    hname = src_name or ("skeleton" if hurt_src == "arrow" else "hostile")
                    hdist = min(hdist, 16.0)
                else:
                    return
            else:
                return
        raw_hp = extra.get("health", st.get("health", 20.0))
        try:
            hp = float(raw_hp)
        except (TypeError, ValueError):
            hp = 20.0
        combat_skills = {"hunt_mob", "deflect_skeleton_arrow", "shoot_mob", "fire_crossbow", "sniper_duel"}
        if failed_name in combat_skills and hp > 12.0:
            return
        if has_equipped_armor(self.state) and hp > 16.0:
            return
        hname = hname or "hostile"
        now = time.monotonic()
        intent = {
            "kind": "flee",
            "why": f"受击应急脱险 {hname} 距离 {hdist:g}",
            "severity": "soft",
            "source": "hurt_interrupt",
            "hostile": hname,
            "dist": hdist,
            "at": now,
            "expires_at": now + 10.0,
        }
        setter = getattr(self.state, "set_safety_intent", None)
        if callable(setter):
            setter(intent)
        else:
            self.state.safety_intent = intent

    def _resolve_goal_spec(self, gid: str) -> Optional[GoalSpec]:
        if hasattr(self.planner, "rules") and hasattr(self.planner.rules, "ledger_map"):
            g = self.planner.rules.ledger_map.get(gid)
            if g is not None:
                return g
        if self.observer is not None and hasattr(self.observer, "_ledger_map"):
            g = self.observer._ledger_map.get(gid)
            if g is not None:
                return g
        return GOAL_MAP.get(gid)

    def _resolve_target_goal_specs(self) -> list[GoalSpec]:
        """本轮聚焦的全部目标（一个或多个，config.target_goal 可写成 "a,b"）。"""
        specs = [self._resolve_goal_spec(gid) for gid in normalize_goal_ids(self.config.target_goal)]
        return [g for g in specs if g is not None]

    def _sync_and_check_target_goal(self) -> bool:
        completed_advs = set(self._server_advancements)
        if self.observer is not None:
            completed_advs.update(self.observer.server_advancements)
            self._server_advancements.update(completed_advs)
        if hasattr(self.planner, "rules") and hasattr(self.planner.rules, "completed_advancements"):
            self.planner.rules.completed_advancements.update(completed_advs)
        if self.observer is not None:
            self.observer.update_goals_reached(self.state)
        if self.config.target_goal:
            specs = self._resolve_target_goal_specs()
            return bool(specs) and all(is_goal_achieved(g, self.state, completed_advs) for g in specs)
        return False

    def _budget_exceeded(self, started_mono: float, steps: int, deaths: int) -> Optional[str]:
        if self.config.max_steps > 0 and steps >= self.config.max_steps:
            return "max_steps_reached"
        if self.config.max_seconds > 0 and (time.monotonic() - started_mono) >= self.config.max_seconds:
            return "max_seconds_reached"
        if self.config.max_deaths > 0 and deaths >= self.config.max_deaths:
            return "max_deaths_reached"
        return None

    def _refresh_world_state(self, *, inventory_only: bool = False) -> None:
        if not inventory_only:
            self.state.refresh_status(self.client)
            self.state.drain(self.client)
        if self.state.inventory_dirty or not self.state.inventory:
            self.state.refresh_inventory(self.client)

    def _use_async_observation(self, monitor: Optional[SafetyMonitor]) -> bool:
        return bool(
            monitor is not None
            and self.config.async_observe_for_llm
            and self.state._status_booted
            and self.state._events_booted
            and not getattr(monitor, "session_error", None)
        )

    # ---- 瞬时故障自愈 -------------------------------------------------
    def _backoff_s(self, attempt: int) -> float:
        base = max(0.0, self.config.transient_backoff_s)
        return min(max(base, self.config.transient_backoff_max_s), base * (2 ** attempt))

    def _note_outage(self, e: ApiError) -> None:
        if self._outage_since is None:
            self._outage_since = self._clock()
        self.recent_failures.append(f"loop: 读状态瞬时故障 {e}")

    def _outage_exceeded(self) -> bool:
        limit = self.config.outage_limit_s
        return bool(limit > 0 and self._outage_since is not None
                    and self._clock() - self._outage_since >= limit)

    def _refresh_with_backoff(self, *, inventory_only: bool = False) -> bool:
        """刷新状态；非致命、非会话类错误按指数退避重试。

        返回 True 表示刷新成功；False 表示重试用尽、本拍应跳过。
        会话类错误（`SESSION_CODES`）与致命错误原样抛出，由调用方处理。
        """
        for attempt in range(max(0, self.config.transient_retries) + 1):
            try:
                self._refresh_world_state(inventory_only=inventory_only)
            except ApiError as e:
                if e.kind == FATAL or e.code in SESSION_CODES:
                    raise
                self._note_outage(e)
                if attempt >= self.config.transient_retries:
                    self.transient_faults += 1
                    self.log(f"[loop] 读状态连续失败 {attempt + 1} 次（{e}），跳过本拍")
                    return False
                wait_s = self._backoff_s(attempt)
                self.log(f"[loop] 读状态瞬时故障（{e}），{wait_s:.1f}s 后重试 ({attempt + 1}/{self.config.transient_retries})")
                self._sleep(wait_s)
                continue
            self._outage_since = None
            return True
        return False

    def _recover_guarded(self, pre_inv_fp: dict[str, int], initial_err: Optional[ApiError] = None) -> bool:
        """会话/重生恢复；恢复本身遇到非致命错误时不让进程退出，下一拍再试。"""
        try:
            self._recover_session_or_respawn(pre_inv_fp, initial_err=initial_err)
        except ApiError as e:
            if e.kind == FATAL:
                raise
            self._note_outage(e)
            self.transient_faults += 1
            wait_s = self._backoff_s(self.config.transient_retries)
            self.log(f"[loop] 会话恢复未完成（{e}），{wait_s:.1f}s 后下一拍重试")
            self._sleep(wait_s)
            return False
        self._outage_since = None
        return True

    def _wait_ready_guarded(self) -> Optional[dict]:
        """启动时等 ready；网关不可用时退避重试，超过停机上限返回 None。"""
        attempt = 0
        while True:
            try:
                st = self.client.wait_ready(timeout=120)
            except ApiError as e:
                if e.kind == FATAL:
                    raise
                self._note_outage(e)
                if self._outage_exceeded():
                    return None
                wait_s = self._backoff_s(attempt)
                attempt += 1
                self.log(f"[loop] 启动等待 ready 失败（{e}），{wait_s:.1f}s 后重试")
                self._sleep(wait_s)
                continue
            self._outage_since = None
            return st

    def _is_dead_now(self) -> bool:
        if not self.state.st.get("alive", True):
            return True
        if len(self.state.deaths) > self._seen_deaths_len:
            return True
        return False

    def _abort_fatal_skill(self, skill: Optional[Skill], err: Exception) -> None:
        """中止遭遇致命错误的技能并记录收尾切片与 fatal 事件。"""
        if skill is None:
            return
        if self.observer is not None and self.observer.tape is not None:
            self.observer.tape.end_skill_slice()
        skill.abort()
        code, _ = failure(err)
        if self.observer is not None:
            self.observer.on_skill_end(skill, "fatal", self.state, error_code=code, error_msg=str(err))

    def _recover_session_or_respawn(
        self,
        pre_death_inv: dict[str, int],
        *,
        initial_err: Optional[ApiError] = None,
    ) -> bool:
        """等待角色重生并回到比赛服 `ready=True`，刷新真实背包，不发多余 `/posture`。"""
        t0 = time.monotonic()
        deadline = t0 + self.config.respawn_timeout_s
        triggered_death_loop = bool(initial_err and initial_err.code == "death_loop_cooldown")
        last_st = self.state.snapshot()

        if initial_err is not None and initial_err.code == "death_loop_cooldown":
            rem = float(initial_err.extra.get("cooldown_remaining_seconds", 60) or 60)
            wait_s = min(310.0, max(1.0, rem + 2.0))
            self.log(f"[loop] 触发 death_loop_cooldown，冷却 {wait_s:.1f}s 后重连...")
            time.sleep(wait_s)
            deadline = max(deadline, time.monotonic() + max(60.0, self.config.respawn_timeout_s))
            self.client.connect(timeout=20)

        while time.monotonic() < deadline:
            try:
                st = self.client.status()
                self.state.set_status(st)
                last_st = st
                if st.get("alive", True) and st.get("ready", False):
                    break
                if not st.get("ready", False) and st.get("alive", True):
                    self.client.wait_ready(timeout=min(45.0, max(5.0, deadline - time.monotonic())))
                    st = self.client.status()
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
            except ApiError as e:
                if e.code == "not_connected":
                    self.log("[loop] 断线恢复：按 ≥30s 间隔重连...")
                    try:
                        self.client.ensure_connected()
                        st = self.client.wait_ready(timeout=60)
                        self.state.set_status(st)
                        last_st = st
                        if st.get("alive", True) and st.get("ready", False):
                            break
                    except ApiError as e2:
                        if e2.code == "death_loop_cooldown":
                            e = e2
                        else:
                            time.sleep(max(0.05, self.config.respawn_poll_s))
                            continue
                if e.code == "death_loop_cooldown":
                    triggered_death_loop = True
                    rem = float(e.extra.get("cooldown_remaining_seconds", 60) or 60)
                    wait_s = min(310.0, max(1.0, rem + 2.0))
                    self.log(f"[loop] 触发 death_loop_cooldown，冷却 {wait_s:.1f}s 后重连...")
                    time.sleep(wait_s)
                    deadline = max(deadline, time.monotonic() + max(60.0, self.config.respawn_timeout_s))
                    self.client.connect(timeout=20)
                    st = self.client.wait_ready(timeout=60)
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
                elif e.code == "wrong_server":
                    st = self.client.wait_ready(timeout=45)
                    self.state.set_status(st)
                    last_st = st
                    if st.get("alive", True) and st.get("ready", False):
                        break
            time.sleep(max(0.01, self.config.respawn_poll_s))
        else:
            raise ApiError("not_ready", f"重生/会话恢复超时 ({self.config.respawn_timeout_s}s)")

        respawn_ms = (time.monotonic() - t0) * 1000.0
        self.state.invalidate_inventory()
        inv = self.state.refresh_inventory(self.client)
        self.state.drain(self.client)
        self._seen_deaths_len = len(self.state.deaths)

        ld = (last_st.get("last_death") or {}) if isinstance(last_st, dict) else {}
        cause = str(ld.get("cause") or "")
        death_pos = ld.get("pos")
        after_fp = _inv_fingerprint(inv) or {}
        lost_items = {
            k: int(v) - int(after_fp.get(k, 0))
            for k, v in (pre_death_inv or {}).items()
            if int(v) > int(after_fp.get(k, 0))
        }
        if isinstance(death_pos, (list, tuple)) and len(death_pos) >= 3:
            try:
                memo_for(self.state).record_death(
                    death_pos,
                    cause=cause,
                    lost_items=lost_items,
                    dimension=str(self.state.st.get("dimension") or "overworld"),
                )
            except Exception:
                pass
        self.log(f"[loop] 重生就绪 ({respawn_ms:.0f}ms) pos={self.state.pos} cause={cause!r}")

        if self.observer is not None:
            self.observer.on_death_recovered(
                self.state,
                respawn_ms=respawn_ms,
                cause=cause,
                death_pos=death_pos,
                before_inv_fp=pre_death_inv,
                after_inv_fp=after_fp,
                triggered_death_loop=triggered_death_loop,
            )
        return True

    def _record_death_and_stop(
        self,
        pre_death_inv: dict[str, int],
        *,
        initial_err: Optional[ApiError] = None,
        cause_hint: str = "",
    ) -> None:
        """在 `--stop-on-death` 模式下记录完整死亡上下文（不等待重生），随后交给主循环干净退出。"""
        last_st = self.state.snapshot()
        try:
            st_now = self.client.status()
            if isinstance(st_now, dict) and st_now:
                self.state.set_status(st_now)
                last_st = st_now
        except Exception:
            pass
        try:
            self.state.drain(self.client)
        except Exception:
            pass
        self._seen_deaths_len = len(self.state.deaths)

        ld = (last_st.get("last_death") or {}) if isinstance(last_st, dict) else {}
        ev_death = self.state.deaths[-1] if self.state.deaths else {}
        cause = str(
            ld.get("cause")
            or (ev_death.get("cause") if isinstance(ev_death, dict) else "")
            or cause_hint
            or (str(initial_err) if initial_err is not None else "")
        )
        death_pos = (
            ld.get("pos")
            or (ev_death.get("pos") if isinstance(ev_death, dict) else None)
            or last_st.get("pos")
            or self.state.pos
        )
        dim = str(last_st.get("dimension") or "overworld")
        triggered_death_loop = bool(initial_err is not None and initial_err.code == "death_loop_cooldown")

        if isinstance(death_pos, (list, tuple)) and len(death_pos) >= 3:
            try:
                memo_for(self.state).record_death(
                    death_pos,
                    cause=cause,
                    lost_items=dict(pre_death_inv or {}),
                    dimension=dim,
                )
            except Exception:
                pass
        self.log(
            f"[loop] 触发 --stop-on-death：检测到真实死亡，立即记录上下文并退出 "
            f"(pos={death_pos}, dim={dim}, hp={last_st.get('health')}, food={last_st.get('food')}, cause={cause!r})"
        )
        if self.observer is not None:
            self.observer.on_death_recovered(
                self.state,
                respawn_ms=0.0,
                cause=cause,
                death_pos=death_pos,
                before_inv_fp=pre_death_inv,
                after_inv_fp={},
                triggered_death_loop=triggered_death_loop,
            )

    def run(self) -> RunResult:
        started_mono = time.monotonic()
        steps = 0
        skills_ok = 0
        skills_replan = 0
        deaths_recovered = 0
        stop_reason = "stopped"
        active_skill: Optional[Skill] = None

        monitor = (
            SafetyMonitor(
                self.client,
                self.state,
                self.executor,
                safety=self.safety,
                continuous=self.config.continuous_observe,
                refresh_inventory=self.config.continuous_observe,
            )
            if self.config.enable_monitor
            else None
        )
        try:
            st = self._wait_ready_guarded()
            if st is None:
                self.log(f"[loop] 启动后连续 {self.config.outage_limit_s:.0f}s 等不到 ready，停机")
                stop_reason = "transient_outage"
                return RunResult(0, 0, 0, 0, round(time.monotonic() - started_mono, 2),
                                 stop_reason, self.transient_faults)
            self.state.set_status(st)
            if self._last_roadside_scan_pos is None:
                self._last_roadside_scan_pos = self._pos_tuple(self.state)
                self._last_roadside_scan_dim = str(self.state.st.get("dimension") or "overworld")
            try:
                self.state.refresh_inventory(self.client)
                self.state.drain(self.client)
            except ApiError as e:
                if e.kind == FATAL:
                    raise
                self.state.invalidate_inventory()  # 留给第一拍的刷新补读
                self._note_outage(e)
            self._seen_deaths_len = len(self.state.deaths)
            self._load_checkpoint_if_configured()
            if st.get("ready") and self._pos_tuple(self.state) is not None:
                try:
                    memo_for(self.state).set_spawn(
                        self.state.st["pos"],
                        dimension=str(self.state.st.get("dimension") or "overworld"),
                        overwrite=False,
                    )
                except Exception:
                    pass
            self.log(f"[loop] 启动完成: {self.state.summary()}")

            if monitor is not None:
                monitor.start()
            if self.observer is not None:
                self.observer.on_run_start(self.state)
                if self.config.target_goal:
                    self.observer.on_progress_tick(self.state, client=self.client, force=True)

            if self._sync_and_check_target_goal():
                self._maybe_save_checkpoint_on_goal_progress(force=True)
                if self.config.stop_on_goal:
                    stop_reason = "goal_reached"

            while stop_reason != "goal_reached":
                budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                if budget:
                    stop_reason = budget
                    break

                pre_inv_fp = _inv_fingerprint(self.state.inventory) or {}
                try:
                    if self._use_async_observation(monitor):
                        if self.state.inventory_dirty or not self.state.inventory:
                            refreshed = self._refresh_with_backoff(inventory_only=True)
                        else:
                            refreshed = True
                    else:
                        refreshed = self._refresh_with_backoff()
                except ApiError as e:
                    if e.code not in SESSION_CODES:
                        raise  # 只剩致命错误
                    if self.config.stop_on_death and (e.code in ("dead", "death_loop_cooldown") or self._is_dead_now()):
                        self._record_death_and_stop(pre_inv_fp, initial_err=e, cause_hint=str(e))
                        stop_reason = "death_stopped"
                        break
                    if self._recover_guarded(pre_inv_fp, initial_err=e):
                        deaths_recovered += 1
                    elif self._outage_exceeded():
                        stop_reason = "transient_outage"
                        break
                    continue
                if not refreshed:
                    if self._outage_exceeded():
                        self.log(f"[loop] 连续 {self.config.outage_limit_s:.0f}s 读不到状态，停机")
                        stop_reason = "transient_outage"
                        break
                    continue

                if self._is_dead_now():
                    if self.config.stop_on_death:
                        self._record_death_and_stop(pre_inv_fp, cause_hint="status_dead")
                        stop_reason = "death_stopped"
                        break
                    if self._recover_guarded(pre_inv_fp):
                        deaths_recovered += 1
                    elif self._outage_exceeded():
                        stop_reason = "transient_outage"
                        break
                    continue

                self._record_current_biome()

                if self._sync_and_check_target_goal():
                    self._maybe_save_checkpoint_on_goal_progress(force=True)
                    if self.config.stop_on_goal:
                        stop_reason = "goal_reached"
                        break

                decision = self.planner.select(self.state, list(self.recent_failures))
                if decision.skill is None:
                    if getattr(decision, "stop_episode", False):
                        if self.observer is not None:
                            self.observer.on_progress_tick(self.state, client=self.client)
                        self.log(f"[loop] 单目标止损退轮: {decision.reason}")
                        stop_reason = "goal_stopped_out"
                        break

                    cur_pos = self._pos_tuple(self.state)
                    is_cooldown_idle = (
                        not self.state.in_safe_mode
                        and (
                            getattr(decision, "cooldown_blocked", False)
                            or "冷却" in (decision.reason or "")
                        )
                    )
                    if is_cooldown_idle:
                        if (
                            self._idle_spin_pos is not None
                            and cur_pos is not None
                            and math.dist(cur_pos, self._idle_spin_pos) < 2.0
                        ):
                            self._idle_spin_count += 1
                        else:
                            if (
                                self._failure_tier_pos is not None
                                and cur_pos is not None
                                and math.dist(cur_pos, self._failure_tier_pos) >= 2.0
                            ):
                                self._reset_failure_tier(cur_pos)
                            self._idle_spin_pos = cur_pos
                            self._idle_spin_count = 1

                        if self._idle_spin_count >= 2:
                            # 连续 >=2 拍同位冷却空转：严禁继续长睡，直接按 3 层失败状态机升级
                            blocked_gid = getattr(decision, "blocked_goal_id", None)
                            if self._failure_tier == 0:
                                self._failure_tier = 1
                                self.failure_tier_state = "local_retry"
                                self.failure_escalations.append("local_retry")
                                self._failure_tier_pos = cur_pos
                                clear_fn = getattr(self.planner, "clear_tactical_cooldowns", None)
                                cleared = clear_fn(self.state, goal_id=blocked_gid, include_relocate=False) if callable(clear_fn) else []
                                self.log(f"[loop] 同位冷却空转 -> Tier 1 local_retry: 清除战术冷却 {cleared}")
                                self._idle_spin_count = 0
                                continue
                            if self._failure_tier == 1:
                                self._failure_tier = 2
                                self.failure_tier_state = "relocate"
                                self.failure_escalations.append("relocate")
                                self._failure_tier_pos = cur_pos
                                cell = self._cell_key(self.state)
                                reloc_skill = None
                                if cell not in self._relocated_cells:
                                    self._relocated_cells.add(cell)
                                    make_reloc = getattr(self.planner, "make_relocate_skill", None)
                                    reloc_skill = make_reloc(self.state) if callable(make_reloc) else None
                                if reloc_skill is not None:
                                    self.log(f"[loop] 同位冷却空转 -> Tier 2 relocate: 派发单次脱困/外迁 {reloc_skill}")
                                    self._idle_spin_count = 0
                                    decision = PlanDecision(
                                        reloc_skill,
                                        "rule",
                                        f"同位冷却空转 Tier 2 外迁脱困: {decision.reason}",
                                    )
                                else:
                                    self._failure_tier = 2
                            if decision.skill is None and self._failure_tier >= 2:
                                self._failure_tier = 3
                                self.failure_tier_state = "goal_abandoned"
                                self.failure_escalations.append("goal_abandoned")
                                self._failure_tier_pos = cur_pos
                                target_gid = blocked_gid
                                if not target_gid:
                                    cur_obj = getattr(self.planner, "current_objective", None)
                                    if cur_obj is not None:
                                        target_gid = cur_obj.goal_id
                                if not target_gid and hasattr(self.planner, "rules"):
                                    ranked = self.planner.rules._rank(self.state)
                                    if ranked:
                                        target_gid = ranked[0].step_goal.id
                                abandon_fn = getattr(self.planner, "abandon_goal", None)
                                abandoned = abandon_fn(target_gid) if (callable(abandon_fn) and target_gid) else False
                                clear_fn = getattr(self.planner, "clear_tactical_cooldowns", None)
                                if callable(clear_fn):
                                    clear_fn(self.state, include_relocate=False)
                                self.log(f"[loop] 同位冷却空转 -> Tier 3 goal_abandoned: target={target_gid} abandoned={abandoned}")
                                self._idle_spin_count = 0
                                self._failure_tier = 0
                                if abandoned:
                                    continue
                                exhausted_fn = getattr(getattr(self.planner, "rules", None), "targets_exhausted", None)
                                if callable(exhausted_fn) and exhausted_fn(self.state):
                                    stop_reason = "goal_stopped_out"
                                    break
                                # 无目标可放弃（target=None）：落入下方空闲退避睡眠，禁止 0ms 紧循环空转
                    else:
                        self._idle_spin_count = 0

                    if decision.skill is None:
                        if self.config.idle_sleep_s > 0:
                            self._do_sleep(self.config.idle_sleep_s)
                        if self.observer is not None:
                            self.observer.on_progress_tick(self.state, client=self.client)
                        # 若在单次零等待测试中无技能可跑，防止紧凑忙等
                        if self.config.idle_sleep_s == 0:
                            stop_reason = f"idle:{decision.reason}"
                            break
                        continue

                active_skill = decision.skill
                skill_start_pos = self._pos_tuple(self.state)
                steps_at_skill_start = steps
                self.log(f"[loop] -> {active_skill} ({decision.source}: {decision.reason})")
                if self.observer is not None:
                    self.observer.on_skill_start(active_skill, decision.source, decision.reason, self.state)

                died_during_skill = False
                skill_t0 = time.monotonic()
                goal_reached_now = False
                skill_done = False
                try:
                    while True:
                        budget = self._budget_exceeded(started_mono, steps, deaths_recovered)
                        if budget:
                            if self.observer is not None and self.observer.tape is not None:
                                self.observer.tape.end_skill_slice()
                            active_skill.abort()
                            if self.observer is not None:
                                if self.state.st.get("sneaking") or self.state.st.get("blocking"):
                                    try:
                                        self.state.refresh_status(self.client, max_age_s=0)
                                    except ApiError as refresh_err:
                                        if refresh_err.kind == FATAL:
                                            raise
                                self.observer.on_skill_end(
                                    active_skill, "budget_stopped", self.state, died_or_disconnected=False
                                )
                            active_skill = None
                            stop_reason = budget
                            return RunResult(
                                steps, skills_ok, skills_replan, deaths_recovered,
                                round(time.monotonic() - started_mono, 2), stop_reason,
                                self.transient_faults,
                            )

                        if self._is_dead_now():
                            died_during_skill = True
                            raise Replan("角色已死亡，中断当前技能等待重生")

                        done = active_skill.step(self.executor, self.state)
                        if done:
                            skill_done = True
                            break
                        steps += 1
                        if self.observer is not None:
                            self.observer.on_step(active_skill, active_skill.last, self.state, client=self.client)
                except (Replan, ApiError) as e:
                    if isinstance(e, ApiError) and e.kind == FATAL:
                        self._abort_fatal_skill(active_skill, e)
                        active_skill = None
                        raise
                    if active_skill is not None:
                        skill_dt = max(0.0, time.monotonic() - skill_t0)
                        failed_name = active_skill.name
                        if self.observer is not None and self.observer.tape is not None:
                            self.observer.tape.end_skill_slice()
                        active_skill.abort()
                        code, extra = failure(e)
                        is_death_err = (
                            died_during_skill
                            or code in ("dead", "not_connected", "death_loop_cooldown")
                            or (
                                code == "interrupted"
                                and extra.get("cause", extra.get("interrupted_by")) == "death"
                            )
                            or self._is_dead_now()
                        )
                        self.recent_failures.append(f"{active_skill.name}: {e}")
                        self.log(f"[loop] 技能中断 {active_skill}: {e}")
                        real_death_from_refresh = False
                        if not is_death_err:
                            try:
                                self.state.refresh_status(self.client, max_age_s=0)
                            except ApiError as refresh_err:
                                if refresh_err.kind == FATAL:
                                    raise
                                # 会话类错误按死亡/断线恢复；网关抖动不算死亡，留给下一拍退避刷新
                                if refresh_err.code in SESSION_CODES:
                                    is_death_err = True
                                    real_death_from_refresh = refresh_err.code in ("dead", "death_loop_cooldown")
                                else:
                                    self._note_outage(refresh_err)
                        if not is_death_err and code in ("interrupted", "busy"):
                            stop_fn = getattr(self.executor, "stop", None)
                            if code == "busy":
                                busy_unreleased = False
                                if callable(stop_fn):
                                    try:
                                        stop_fn(raise_on_error=True)
                                    except TypeError:
                                        stop_fn()
                                    except ApiError as stop_err:
                                        if stop_err.code == "busy":
                                            busy_unreleased = True
                                    except Exception:
                                        pass
                                if busy_unreleased:
                                    self._unreleased_busy_streak += 1
                                    if self._unreleased_busy_streak >= 3:
                                        self._recover_stuck_busy_session()
                                else:
                                    self._unreleased_busy_streak = 0
                            else:
                                self._unreleased_busy_streak = 0
                                if callable(stop_fn):
                                    stop_fn(raise_on_error=False)
                                if code == "interrupted":
                                    self._maybe_set_hurt_flee_intent(failed_name, extra)
                        elif not is_death_err:
                            self._unreleased_busy_streak = 0
                        if failed_name == "emergency_escape" or getattr(decision, "safety_escape", False):
                            self._acknowledge_safety_intent(completed=False)
                        environment = e if isinstance(e, EnvBlocked) and not is_death_err else None
                        if environment is not None:
                            self._path_failures.pop(active_skill.goal, None)
                            self.planner.record_environment_block(active_skill, environment, self.state)
                        else:
                            skills_replan += 1
                            if code in ("path_not_found", "stalled") and not is_death_err:
                                streak = self._path_failures.get(active_skill.goal, 0) + 1
                                self._path_failures[active_skill.goal] = streak
                            else:
                                streak = 0
                                self._path_failures.pop(active_skill.goal, None)
                            cap = self.config.failure_cooldown_cap_s
                            if failed_name == "emergency_escape" and code == "busy":
                                pass
                            elif streak >= 3:
                                if cap is not None:
                                    active_skill.mark_failed(cooldown_s=min(cap, active_skill.cooldown_s * 1.5))
                                else:
                                    active_skill.mark_failed(cooldown_s=3 * active_skill.cooldown_s)
                            elif cap is not None:
                                active_skill.mark_failed(cooldown_s=min(cap, active_skill.cooldown_s))
                            else:
                                active_skill.mark_failed()
                        if self.observer is not None:
                            if not is_death_err and (self.state.st.get("sneaking") or self.state.st.get("blocking")):
                                try:
                                    self.state.refresh_status(self.client, max_age_s=0)
                                except ApiError as refresh_err:
                                    if refresh_err.kind == FATAL:
                                        raise
                            self.observer.on_skill_end(
                                active_skill,
                                "env_blocked" if environment is not None else "replan",
                                self.state,
                                error_code="env_blocked" if environment is not None else code,
                                error_msg=str(e),
                                died_or_disconnected=is_death_err,
                                **({"environment": environment} if environment is not None else {}),
                                **({"environment_recovery": True} if decision.environment_recovery and not is_death_err else {}),
                            )
                        if environment is None and hasattr(self.planner, "record_skill_outcome"):
                            self.planner.record_skill_outcome(
                                failed_name,
                                ok=False,
                                elapsed_s=skill_dt,
                                completed_advs=(
                                    set(self.observer.server_advancements)
                                    if self.observer is not None
                                    else set(self._server_advancements)
                                ),
                            )
                        active_skill = None
                        if is_death_err:
                            init_api_err = e if isinstance(e, ApiError) else None
                            is_real_death = (
                                died_during_skill
                                or real_death_from_refresh
                                or code in ("dead", "death_loop_cooldown")
                                or (
                                    code == "interrupted"
                                    and extra.get("cause", extra.get("interrupted_by")) == "death"
                                )
                                or self._is_dead_now()
                            )
                            if self.config.stop_on_death and is_real_death:
                                self._record_death_and_stop(pre_inv_fp, initial_err=init_api_err, cause_hint=str(e))
                                stop_reason = "death_stopped"
                                break
                            if self._recover_guarded(pre_inv_fp, initial_err=init_api_err):
                                deaths_recovered += 1
                except Fatal as e:
                    self._abort_fatal_skill(active_skill, e)
                    active_skill = None
                    raise
                except KeyboardInterrupt:
                    stop_reason = "keyboard_interrupt"
                    break

                if skill_done and active_skill is not None:
                    self._unreleased_busy_streak = 0
                    skill_dt = max(0.0, time.monotonic() - skill_t0)
                    finished_skill = active_skill
                    active_skill = None
                    finished_name = finished_skill.name
                    self._path_failures.pop(finished_skill.goal, None)
                    if self.observer is not None and self.observer.tape is not None:
                        self.observer.tape.end_skill_slice()
                    try:
                        if monitor is not None and self.config.async_observe_for_llm:
                            if self.state.inventory_dirty:
                                self.state.refresh_inventory(self.client)
                            if self.state.st.get("sneaking") or self.state.st.get("blocking"):
                                self.state.refresh_status(self.client, max_age_s=0)
                        else:
                            # 技能已完成；这里读不到状态不应把它记成失败，留给下一拍刷新与恢复
                            self._refresh_with_backoff()
                    except ApiError as refresh_err:
                        if refresh_err.kind == FATAL:
                            raise
                    if finished_name == "emergency_escape" or getattr(decision, "safety_escape", False):
                        self._acknowledge_safety_intent(completed=True)
                    end_pos = self._pos_tuple(self.state)
                    moved_away = bool(
                        skill_start_pos is not None
                        and end_pos is not None
                        and math.dist(skill_start_pos, end_pos) >= 2.0
                    )
                    if moved_away or finished_name not in ("scout_island", "emergency_escape"):
                        self._reset_failure_tier(end_pos)
                    # 零进度完成护栏：完成但没走一步、也没挪窝 → 指数退避，防止空转死循环
                    moved_dist = (
                        math.dist(skill_start_pos, end_pos)
                        if (skill_start_pos is not None and end_pos is not None)
                        else None
                    )
                    streak = note_no_progress(
                        self._no_progress,
                        finished_skill,
                        steps=steps - steps_at_skill_start,
                        moved=moved_dist,
                    )
                    if streak >= _NO_PROGRESS_STREAK_BEFORE_COOLDOWN:
                        self.log(
                            f"[loop] {finished_skill} 连续 {streak} 次零进度完成，已冷却抑制空转"
                        )
                    # 被困自救：连续两次技能跑完都没挪出 1.5 格 → 主动挖一格（头顶封口块优先）
                    if moved_dist is not None and moved_dist < 1.5:
                        self._stuck_streak += 1
                    else:
                        self._stuck_streak = 0
                    if self._stuck_streak >= 2:
                        self._try_self_rescue_breach()
                    skills_ok += 1
                    goal_specs = list(self._resolve_target_goal_specs())
                    active_rules_goals = getattr(getattr(self.planner, "rules", None), "active_goals", ()) or ()
                    force_tick = any(
                        g.skill == finished_name and g.done_advancements
                        for g in (*goal_specs, *active_rules_goals, *GOAL_MAP.values())
                    )
                    if self.observer is not None:
                        self.observer.on_skill_end(
                            finished_skill, "completed", self.state, died_or_disconnected=False
                        )
                        self.observer.on_progress_tick(
                            self.state, client=self.client, force=force_tick
                        )
                    elif force_tick:
                        try:
                            adv = self.client.advancements(status="completed", include_recipes=False)
                            for item in (adv.get("advancements") or []):
                                aid = item.get("id") if isinstance(item, dict) else None
                                if aid:
                                    self._server_advancements.add(aid)
                        except Exception:
                            pass
                    completed_advs = set(self._server_advancements)
                    if self.observer is not None:
                        completed_advs.update(self.observer.server_advancements)
                    if hasattr(self.planner, "record_skill_outcome"):
                        self.planner.record_skill_outcome(
                            finished_name,
                            ok=True,
                            elapsed_s=skill_dt,
                            completed_advs=completed_advs,
                        )
                    target_reached = self._sync_and_check_target_goal()
                    self._maybe_save_checkpoint_on_goal_progress(force=target_reached)
                    if target_reached and self.config.stop_on_goal:
                        stop_reason = "goal_reached"
                        goal_reached_now = True
                    if not goal_reached_now:
                        self._maybe_roadside_scan()

                if goal_reached_now:
                    break

        finally:
            if active_skill is not None:
                active_skill.abort()
            release_materials = getattr(self.planner, "release_skill_materials", None)
            if release_materials is not None:
                release_materials()
            self._save_checkpoint_if_configured()
            if monitor is not None:
                monitor.close()
            if self.observer is not None:
                # 外迁最后一步可能耗尽预算。还未进入下一规划拍时，保留挂起证据。
                blocked = self.planner.blocked_targets(self.state, self.observer.target_goals, refresh=False)
                self.observer.on_run_end(
                    self.state, client=self.client, stop_reason=stop_reason,
                    **({"environment_blocked": blocked} if blocked else {}),
                )
            if self.config.disconnect_on_exit:
                try:
                    self.client.disconnect()
                except Exception:
                    pass

        return RunResult(
            steps=steps,
            skills_completed=skills_ok,
            skills_replan=skills_replan,
            deaths_recovered=deaths_recovered,
            elapsed_s=round(time.monotonic() - started_mono, 2),
            stop_reason=stop_reason,
            transient_faults=self.transient_faults,
        )
