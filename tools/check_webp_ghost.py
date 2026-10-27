#!/usr/bin/env python3
"""量化动画 WebP 的「拖影」：容器按原标志合成 vs 强制 NO_BLEND 合成 的逐帧差异。

原理：本项目每帧都是完整画面，正确播放时画布 == 本帧像素。若容器写着 BLEND，
播放器会把帧叠在上一帧上，旧姿势透出来——「按原标志合成」与「强制 NO_BLEND
合成」的差异像素就是拖影。干净文件两者逐帧一致，差异为 0。

用法：
  python tools/check_webp_ghost.py <file|dir> [<file|dir> ...]

输出：
  a001.webp: avg=0 px (0.00%) worst=#0 0 px  → PASS
退出码：全部 PASS=0，任一超阈值=1（阈值：每帧差异 > 画布 0.5%）。

依赖：Pillow（pip install pillow）
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageChops

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BLEND_BIT = 0x02  # ANMF flags bit1：1 = NO_BLEND（不与前一帧合成）
THRESHOLD_RATIO = 0.005  # 每帧差异 > 画布 0.5% 判 FAIL（干净基线 0.15% 以内）


def u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def force_noblend(data: bytes) -> bytes:
    """把所有整画布帧强制成 NO_BLEND（flags bit1 置 1），返回新字节串（不改输入）。"""
    buf = bytearray(data)
    canvas = None
    off = 12
    while off + 8 <= len(buf):
        fourcc = bytes(buf[off:off + 4])
        size = struct.unpack("<I", buf[off + 4:off + 8])[0]
        body = off + 8
        if fourcc == b"VP8X":
            canvas = (u24(buf[body + 4:body + 7]) + 1, u24(buf[body + 7:body + 10]) + 1)
        elif fourcc == b"ANMF" and canvas:
            full = (u24(buf[body:body + 3]) * 2 == 0
                    and u24(buf[body + 3:body + 6]) * 2 == 0
                    and u24(buf[body + 6:body + 9]) + 1 == canvas[0]
                    and u24(buf[body + 9:body + 12]) + 1 == canvas[1])
            if full:
                buf[body + 15] |= BLEND_BIT
        off += 8 + size + (size & 1)
    return bytes(buf)


def diff_pixels(a: Image.Image, b: Image.Image) -> int:
    """两帧 RGBA 的差异像素数（任一通道不同即算），纯 C 实现。"""
    d = ImageChops.difference(a, b)
    bands = d.split()
    mask = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]),
                              ImageChops.lighter(bands[2], bands[3]))
    total = a.width * a.height
    return total - mask.histogram()[0]


def frames(path_or_bytes):
    im = Image.open(path_or_bytes)
    n = getattr(im, "n_frames", 1)
    for i in range(n):
        im.seek(i)
        yield im.convert("RGBA")


def check(path: Path) -> tuple[bool, str]:
    data = path.read_bytes()
    fixed = force_noblend(data)
    sizes = Image.open(io.BytesIO(data)).size
    canvas_px = sizes[0] * sizes[1]
    limit = canvas_px * THRESHOLD_RATIO
    worst_i, worst_px = -1, -1
    total_px = n = 0
    for i, (fa, fb) in enumerate(zip(frames(io.BytesIO(data)), frames(io.BytesIO(fixed)))):
        px = diff_pixels(fa, fb)
        total_px += px
        n += 1
        if px > worst_px:
            worst_i, worst_px = i, px
    avg = total_px / max(n, 1)
    ok = worst_px <= limit
    return ok, (f"avg={avg:.0f}px ({avg / canvas_px:.2%}) "
                f"worst=#{worst_i} {worst_px}px ({worst_px / canvas_px:.2%}，阈值 {limit:.0f}px)")


def main(argv: list[str]) -> int:
    files: list[Path] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.webp")))
        else:
            files.append(p)
    rc = 0
    for p in files:
        ok, msg = check(p)
        print(f"{p}: {msg}  → {'PASS' if ok else 'FAIL'}")
        if not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
