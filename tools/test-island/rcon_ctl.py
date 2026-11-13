#!/usr/bin/env python3
"""自建测试服专用 RCON 场景搭建与快照恢复工具（物理隔离于 `code/mcagent/`）。

合规边界声明（AGENTS.md §2B）：
- 本脚本仅用于队内自建 `1.21.11` 测试服的赛前测试环境准备（裁切近似虚空岛、切换
  `keepInventory`、快速布置黑夜/残血/深坑测试场景、轮间清理与快照恢复）。
- `code/mcagent/` 严禁导入本文件；正式比赛服无 RCON 权限且严禁任何人工/OP 干预。
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
import time
from typing import Iterable

RCON_AUTH = 3
RCON_EXEC = 2


class RconError(RuntimeError):
    pass


class RconClient:
    """零第三方依赖的 Minecraft RCON 二进制协议客户端（Python 标准库实现）。"""

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._req_id = 0

    def __enter__(self) -> "RconClient":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        resp_id, _ = self._exchange(RCON_AUTH, self.password)
        if resp_id == -1:
            self.close()
            raise RconError("RCON 认证失败：请检查 rcon.password")
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("RCON 连接意外断开")
            buf.extend(chunk)
        return bytes(buf)

    def _exchange(self, pkt_type: int, body: str) -> tuple[int, str]:
        assert self._sock is not None
        self._req_id += 1
        req_id = self._req_id
        payload = body.encode("utf-8") + b"\x00\x00"
        header = struct.pack("<iii", 8 + len(payload), req_id, pkt_type)
        self._sock.sendall(header + payload)

        raw_len = self._recv_exact(4)
        (length,) = struct.unpack("<i", raw_len)
        rest = self._recv_exact(length)
        resp_id, _resp_type = struct.unpack("<ii", rest[:8])
        text = rest[8:-2].decode("utf-8", errors="replace")
        return resp_id, text

    def command(self, cmd: str) -> str:
        _, out = self._exchange(RCON_EXEC, cmd.lstrip("/"))
        return out


def build_scene_commands(scene: str, player: str, cx: int = 0, cy: int = 70, cz: int = 0) -> list[str]:
    """生成指定测试场景的 RCON 指令序列（纯函数，便于离线测试审查）。"""
    base_reset = [
        f"clear {player}",
        f"effect clear {player}",
        f"xp set {player} 0 levels",
        f"gamemode survival {player}",
    ]
    if scene == "empty_day":
        return [
            *base_reset,
            "time set day",
            "weather clear",
            f"tp {player} {cx} {cy} {cz}",
        ]
    if scene == "night_combat":
        return [
            *base_reset,
            "time set midnight",
            "weather clear",
            f"tp {player} {cx} {cy} {cz}",
            f"give {player} minecraft:stone_sword 1",
            f"give {player} minecraft:shield 1",
            f"give {player} minecraft:bread 8",
            f"summon minecraft:zombie {cx + 8} {cy} {cz}",
            f"summon minecraft:skeleton {cx - 10} {cy} {cz}",
        ]
    if scene == "low_hp_hunger":
        return [
            *base_reset,
            "time set day",
            f"tp {player} {cx} {cy} {cz}",
            f"give {player} minecraft:cooked_beef 4",
            f"damage {player} 10 minecraft:generic",
            f"effect give {player} minecraft:hunger 15 20 true",
        ]
    if scene == "stone_tier":
        return [
            *base_reset,
            "time set day",
            f"tp {player} {cx} {cy} {cz}",
            f"give {player} minecraft:wooden_pickaxe 1",
            f"give {player} minecraft:oak_log 4",
        ]
    raise ValueError(f"未知场景: {scene}")


def build_void_perimeter_commands(cx: int, cz: int, radius: int, ring_width: int = 16, y_min: int = -64, y_max: int = 128) -> list[str]:
    """在方形近似岛外围挖出一圈到底的虚空沟壑（分片 fill 防止单次超过 32768 方块上限）。"""
    cmds: list[str] = []
    r = radius
    w = ring_width
    strips = [
        (cx - r - w, cz - r - w, cx + r + w, cz - r - 1),  # 北侧虚空带
        (cx - r - w, cz + r + 1, cx + r + w, cz + r + w),  # 南侧虚空带
        (cx - r - w, cz - r, cx - r - 1, cz + r),          # 西侧虚空带
        (cx + r + 1, cz - r, cx + r + w, cz + r),          # 东侧虚空带
    ]
    step_xz = 16
    step_y = 32
    for x1, z1, x2, z2 in strips:
        for xs in range(x1, x2 + 1, step_xz):
            xe = min(x2, xs + step_xz - 1)
            for zs in range(z1, z2 + 1, step_xz):
                ze = min(z2, zs + step_xz - 1)
                for ys in range(y_min, y_max + 1, step_y):
                    ye = min(y_max, ys + step_y - 1)
                    cmds.append(f"fill {xs} {ys} {zs} {xe} {ye} {ze} minecraft:air")
    return cmds


def _exec_batch(rcon: RconClient, cmds: Iterable[str], dry: bool = False) -> None:
    for c in cmds:
        if dry:
            print(f"[dry-rcon] /{c}")
        else:
            out = rcon.command(c)
            print(f"/{c} -> {out or 'ok'}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="自建测试服 RCON 虚空岛/场景/快照控制工具")
    p.add_argument("--host", default=os.environ.get("RCON_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("RCON_PORT", "25575")))
    p.add_argument("--password", default=os.environ.get("RCON_PASS", ""))
    p.add_argument("--dry", action="store_true", help="只打印将发送的 RCON 指令，不连服务器")

    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gamerule", help="切换 keepInventory 等测试服规则")
    g.add_argument("--keep-inventory", choices=("true", "false"), required=True)

    s = sub.add_parser("scene", help="一键布置测试场景")
    s.add_argument("name", choices=("empty_day", "night_combat", "low_hp_hunger", "stone_tier"))
    s.add_argument("--player", required=True)
    s.add_argument("--origin", default="0,70,0", help="x,y,z 整数坐标")

    v = sub.add_parser("carve-void", help="在指定中心四周清出到底的虚空边界带")
    v.add_argument("--center", default="0,0", help="cx,cz")
    v.add_argument("--radius", type=int, default=40)
    v.add_argument("--ring-width", type=int, default=12)

    r = sub.add_parser("reset-episode", help="清理掉落物、重置玩家状态或触发外部世界快照脚本")
    r.add_argument("--player", required=True)
    r.add_argument("--origin", default="0,70,0")
    r.add_argument("--restore-hook", default="", help="可选：停服恢复世界快照的本地 shell 脚本路径")

    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    if args.cmd == "reset-episode" and args.restore_hook:
        snap_id = time.strftime("snap-%Y%m%d-%H%M%S")
        if args.dry:
            print(f"[dry-hook] {args.restore_hook} -> {snap_id}")
            return 0
        subprocess.run(args.restore_hook, shell=True, check=True)
        print(snap_id)
        return 0

    cmds: list[str] = []
    if args.cmd == "gamerule":
        cmds = [f"gamerule keepInventory {args.keep_inventory}"]
    elif args.cmd == "scene":
        cx, cy, cz = (int(x) for x in args.origin.split(","))
        cmds = build_scene_commands(args.name, args.player, cx, cy, cz)
    elif args.cmd == "carve-void":
        cx, cz = (int(x) for x in args.center.split(","))
        cmds = build_void_perimeter_commands(cx, cz, args.radius, args.ring_width)
    elif args.cmd == "reset-episode":
        cx, cy, cz = (int(x) for x in args.origin.split(","))
        cmds = [
            "kill @e[type=item]",
            "advancement revoke " + args.player + " everything",
            *build_scene_commands("empty_day", args.player, cx, cy, cz),
        ]

    if args.dry:
        for c in cmds[:20]:
            print(f"[dry-rcon] /{c}")
        if len(cmds) > 20:
            print(f"... 共 {len(cmds)} 条分片指令")
        return 0

    if not args.password:
        print("缺少 RCON 密码（--password 或环境变量 RCON_PASS）", file=sys.stderr)
        return 2

    with RconClient(args.host, args.port, args.password) as rcon:
        _exec_batch(rcon, cmds, dry=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
