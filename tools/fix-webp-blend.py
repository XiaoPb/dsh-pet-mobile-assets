#!/usr/bin/env python3
"""把动画 WebP 的帧标志统一修成 NO_BLEND，并可选把 ANIM 背景色改成全透明。

为什么需要：libwebp 的 WebPAnimEncoder（ffmpeg -c:v libwebp 走的就是它）在写
动画时，每帧都置 blend=1(与前一帧合成)、dispose=0(不清回背景)，并且帧矩形是
整张画布。按 WebP 容器规范，播放器（Chrome / Skia / Android 的
AnimatedImageDrawable）会把每一帧**叠在上一帧之上**——帧里透明的区域就保留
上一帧的像素。角色一移动，旧姿势就留在原地，并逐帧累积，这就是「拖影」。

硬约束：只对「帧矩形 == 整张画布」的帧改 NO_BLEND 才安全；局部帧依赖与上一帧
合成，改了会缺肉，遇到就跳过并计数。

用法：
  python tools/fix-webp-blend.py <dir-or-file> ...            # 只报告，不改
  python tools/fix-webp-blend.py <dir-or-file> ... --apply    # 就地修改
  --bg      同时把 ANIM 背景色 #FFFFFFFF 改成 #00000000（防 dispose=TO_BG 闪白框）
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BLEND_BIT = 0x02


def u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def patch(data: bytearray, apply: bool = False, fix_bg: bool = False) -> dict:
    """扫描容器。apply=True 时就地改 blend；fix_bg=True 时改背景色。

    返回 dict(frames, changed, partial_blended, bg_fixed, bgcolor)
    partial_blended = 「局部帧且 blend=1」的数量，这些帧**不改**（改了会缺肉）。
    """
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("不是 WebP")
    canvas = None
    off = 12
    frames = changed = partial_blended = bg_fixed = 0
    bgcolor = None
    while off + 8 <= len(data):
        fourcc = bytes(data[off:off + 4])
        size = struct.unpack("<I", data[off + 4:off + 8])[0]
        body = off + 8
        if fourcc == b"VP8X":
            canvas = (u24(data[body + 4:body + 7]) + 1, u24(data[body + 7:body + 10]) + 1)
        elif fourcc == b"ANIM":
            bgcolor = data[body:body + 4]
            if fix_bg and apply and bgcolor != b"\x00\x00\x00\x00":
                data[body:body + 4] = b"\x00\x00\x00\x00"
                bg_fixed += 1
        elif fourcc == b"ANMF":
            frames += 1
            flags_at = body + 15
            flags = data[flags_at]
            full = canvas is not None and (
                u24(data[body:body + 3]) * 2 == 0
                and u24(data[body + 3:body + 6]) * 2 == 0
                and u24(data[body + 6:body + 9]) + 1 == canvas[0]
                and u24(data[body + 9:body + 12]) + 1 == canvas[1]
            )
            if flags & BLEND_BIT:
                if full:
                    if apply:
                        data[flags_at] = flags | BLEND_BIT  # 幂等：置位
                    changed += 1
                else:
                    partial_blended += 1
        off += 8 + size + (size & 1)
    return {"frames": frames, "changed": changed, "partial_blended": partial_blended,
            "bg_fixed": bg_fixed, "bgcolor": bgcolor, "canvas": canvas}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="webp 文件或目录（目录递归）")
    ap.add_argument("--apply", action="store_true", help="就地修改（默认只报告）")
    ap.add_argument("--bg", action="store_true", help="同时把 ANIM 背景色改成全透明")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for a in args.targets:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.webp")))
        else:
            files.append(p)

    total_frames = total_changed = total_partial = total_bg = 0
    for p in files:
        data = bytearray(p.read_bytes())
        try:
            r = patch(data, apply=args.apply, fix_bg=args.bg)
        except ValueError as e:
            print(f"{p}: {e}", file=sys.stderr)
            continue
        if args.apply and (r["changed"] or r["bg_fixed"]):
            p.write_bytes(data)
        note = ""
        if r["partial_blended"]:
            note = f"  ! 局部帧 blend=1 有 {r['partial_blended']} 帧，未改（改了会缺肉）"
        print(f"{p}: frames={r['frames']} blend→NO_BLEND={r['changed']} "
              f"bg={r['bgcolor'].hex()}{note}")
        total_frames += r["frames"]
        total_changed += r["changed"]
        total_partial += r["partial_blended"]
        total_bg += r["bg_fixed"]

    mode = "已改" if args.apply else "（报告模式，加 --apply 生效）"
    print(f"\n共计 {len(files)} 文件 / {total_frames} 帧，blend=1 共 {total_changed} 帧 {mode}"
          + (f"，背景色已修 {total_bg} 处" if args.bg else ""))
    if total_partial:
        print("⚠ 存在局部帧 blend=1：这些帧需要重新编码解决，patch 救不了")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
