#!/usr/bin/env python3
"""dump 动画 WebP 容器的每帧信息：rect / duration / blend / dispose + ANIM 背景色。

用法：
  python tools/inspect_webp_anim.py <file|dir> [<file|dir> ...]

纯 stdlib。dir 会递归收集 *.webp。
输出示例：
  a001.webp: canvas=300x169 frames=151 partial=0 bg=#00000000
    #000 rect=0,0 300x169 dur=66ms blend=NO_BLEND dispose=NONE
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BLEND_BIT = 0x02       # ANMF flags bit1: 1 = NO_BLEND（不与前一帧合成），0 = BLEND
DISPOSE_BG_BIT = 0x01  # ANMF flags bit0: 1 = 渲染后恢复背景色，0 = 保留帧（NONE）


def u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def parse(data: bytes) -> dict:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("不是 WebP 文件")
    info: dict = {"frames": [], "bgcolor": None, "canvas": None, "loop": None}
    canvas = None
    off = 12
    while off + 8 <= len(data):
        fourcc = bytes(data[off:off + 4])
        size = struct.unpack("<I", data[off + 4:off + 8])[0]
        body = off + 8
        if fourcc == b"VP8X":
            canvas = (u24(data[body + 4:body + 7]) + 1, u24(data[body + 7:body + 10]) + 1)
        elif fourcc == b"ANIM":
            info["bgcolor"] = data[body:body + 4]
            info["loop"] = u24(data[body + 4:body + 7])
        elif fourcc == b"ANMF":
            x = u24(data[body:body + 3]) * 2
            y = u24(data[body + 3:body + 6]) * 2
            w = u24(data[body + 6:body + 9]) + 1
            h = u24(data[body + 9:body + 12]) + 1
            dur = u24(data[body + 12:body + 15])
            flags = data[body + 15]
            info["frames"].append({
                "rect": (x, y, w, h),
                "duration": dur,
                "blend": not flags & BLEND_BIT,          # bit 置 1 = NO_BLEND
                "dispose_bg": bool(flags & DISPOSE_BG_BIT),
            })
        off += 8 + size + (size & 1)
    info["canvas"] = canvas
    return info


def fmt_bg(bg: bytes | None) -> str:
    if bg is None:
        return "?"
    r, g, b, a = bg
    return f"#{r:02X}{g:02X}{b:02X}{a:02X}"


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.webp")))
        else:
            targets.append(p)
    rc = 0
    for p in targets:
        info = parse(p.read_bytes())
        n = len(info["frames"])
        blended = sum(1 for f in info["frames"] if f["blend"])
        partial = sum(1 for f in info["frames"]
                      if f["rect"] != (0, 0, info["canvas"][0], info["canvas"][1]))
        print(f"{p}: canvas={info['canvas'][0]}x{info['canvas'][1]} frames={n} "
              f"blend=1:{blended} partial={partial} loop={info['loop']} bg={fmt_bg(info['bgcolor'])}")
        limit = int(__import__("os").environ.get("INSPECT_FRAMES", "0"))
        for i, f in enumerate(info["frames"]):
            if limit and i >= limit:
                print(f"  …（其余 {n - limit} 帧省略）")
                break
            x, y, w, h = f["rect"]
            print(f"  #{i:03d} rect={x},{y} {w}x{h} dur={f['duration']}ms "
                  f"blend={'BLEND' if f['blend'] else 'NO_BLEND'} "
                  f"dispose={'BACKGROUND' if f['dispose_bg'] else 'NONE'}")
        if blended:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
