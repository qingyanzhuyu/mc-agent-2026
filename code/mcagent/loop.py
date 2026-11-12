"""管道演示（不是策略）：木 -> 木板 -> 木棍 -> 工作台 -> 石镐前置。

它存在的唯一目的，是证明"观察 -> 决策 -> 执行 -> 打点"这条链在真实环境里跑得通，
并且把 Safety 闸、事件增量、错误分类、动作互斥都接上。跑通之后再往里换策略。

    python -m mcagent.loop --max-steps 40
"""
from __future__ import annotations

import sys
import time

from .client import ApiError, Client, Config, FATAL
from .runtime import Executor, Fatal, Replan, Safety, State

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_TTL = 60  # 秒：同一目标挖失败后多久不再试


class Pipeline:
    def __init__(self, c: Client, s: State, ex: Executor):
        self.c, self.s, self.ex = c, s, ex
        self.failed: dict[tuple, float] = {}
        self.step = 0

    # ---------------- 安全闸 ----------------
    def safety(self) -> bool:
        """返回 True 表示刚做了紧急处置，本轮跳过原计划。"""
        d = Safety().check(self.s)
        if not d:
            return False
        kind = d["kind"]
        print(f"  [safety] {kind}: {d.get('why')}")
        try:
            if kind in ("no_status",):
                self.s.set_status(self.c.status())
            elif kind == "wait_respawn":
                time.sleep(3)
            elif kind in ("heal", "safe_mode"):
                self.ex.stop()
                # 先吃：背包里有什么食物就吃
                for it in (self.s.inventory.get("items") or []):
                    if it["name"] in ("bread", "cooked_beef", "cooked_porkchop", "apple", "carrot"):
                        self.ex.act("use_item", lambda: self.c.use_item(it["name"]))
                        break
                else:
                    self.ex.stop()
            elif kind == "surface":
                p = self.s.pos
                self.ex.move_to(p[0], p[2], y=p[1] + 5, rng=1)
            elif kind == "flee":
                h = self.s.st.get("nearest_hostile") or {}
                p = self.s.pos
                if h.get("pos"):
                    dx = p[0] - h["pos"][0]
                    dz = p[2] - h["pos"][2]
                    n = max((dx * dx + dz * dz) ** 0.5, 0.1)
                    self.ex.move_to(p[0] + dx / n * 12, p[2] + dz / n * 12, rng=2)
            elif kind == "douse":
                self.ex.stop()
            elif kind == "escape_lava":
                self.ex.stop()
                p = self.s.pos
                self.ex.move_to(p[0] + 3, p[2] + 3, rng=1)
        except (Replan, Fatal) as e:
            print(f"  [safety] 处置失败: {e}")
        return True

    # ---------------- 小工具 ----------------
    def refresh(self) -> None:
        self.s.set_status(self.c.status())
        self.s.set_inventory(self.c.inventory())
        self.s.drain(self.c)

    def ensure_ready(self) -> None:
        if not self.s.ready:
            self.s.set_status(self.c.wait_ready(timeout=120))

    def pick_target(self, types, radius=24):
        r = self.c.find_blocks(types=list(types), radius=radius, limit=8)
        blocks = r.get("blocks") or []
        if not isinstance(blocks, list):
            return None
        for b in blocks:
            key = (b["name"], tuple(b["pos"]))
            if time.time() - self.failed.get(key, 0) > LOG_TTL:
                return b
        return None

    def acquire(self, log_types, need: int, have_item: str) -> None:
        for i in range(need):
            if self.s.count(have_item) >= need - i:
                return
            self.refresh()
            tgt = self.pick_target(log_types)
            if not tgt:
                raise Replan(f"附近找不到 {log_types}")
            print(f"  -> {tgt['name']} @ {tgt['pos']} dist={tgt.get('dist')}")
            key = (tgt["name"], tuple(tgt["pos"]))
            try:
                self.ex.move_to(tgt["pos"][0], tgt["pos"][2], rng=2)
                self.ex.dig(tgt["pos"])
            except (Replan, Fatal):
                self.failed[key] = time.time()
                raise
            self.s.set_inventory(self.c.inventory())

    # ---------------- 主流程 ----------------
    def run(self, max_steps: int = 40) -> None:
        steps = [
            ("砍 4 个原木", lambda: self.acquire(["oak_log", "birch_log", "spruce_log"], 4, "oak_log")),
            ("合木板", lambda: self.ex.craft("oak_planks", 16)),
            ("合木棍", lambda: self.ex.craft("stick", 8)),
            ("合工作台", lambda: self.ex.craft("crafting_table", 1)),
            ("放工作台", lambda: self.ex.place("crafting_table")),
            ("合木镐", lambda: self.ex.craft("wooden_pickaxe", 1)),
        ]
        for name, fn in steps:
            if self.step >= max_steps:
                print("达到 max-steps，停")
                return
            self.step += 1
            print(f"\n[{self.step}] {name}")
            self.ensure_ready()
            if self.safety():
                continue
            try:
                fn()
                print("  ok")
            except Replan as e:
                print(f"  replan: {e}")
            except Fatal as e:
                print(f"  fatal: {e}")
                return
            except ApiError as e:
                if e.kind == FATAL:
                    print(f"  fatal api: {e}")
                    return
                print(f"  api: {e}")

        self.refresh()
        print("\n===== 结束状态 =====")
        print(self.s.summary())
        for it in (self.s.inventory.get("items") or [])[:15]:
            print(f"  {it['name']} x{it['count']}")
        print(f"动作 {self.ex.n_actions} 次，错误 {self.ex.n_errors} 次")


def main(argv: list[str]) -> int:
    max_steps = 40
    for i, a in enumerate(argv):
        if a == "--max-steps" and i + 1 < len(argv):
            max_steps = int(argv[i + 1])

    cfg = Config.load()
    if not cfg.has_credentials:
        print("没有凭据，先填 mcagent/.env")
        return 2
    c = Client(cfg)
    s = State()
    c.login()
    s.set_status(c.wait_ready(timeout=120))
    s.set_inventory(c.inventory())

    log_path = f"mcagent/logs/run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    ex = Executor(c, s, log_path=log_path)
    Pipeline(c, s, ex).run(max_steps=max_steps)
    print(f"\n日志: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
