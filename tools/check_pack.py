#!/usr/bin/env python3
"""资源包发布门禁：4 条判据全过才能发布，任一不过退出码 1。

判据（对目录内每个 webp）：
  1. blend=0 的帧数 == 总帧数（或局部帧占比符合 allow_mixed 预期，本链路应为 0 局部帧）
  2. 拖影：每帧差异 < 画布 × 0.5%（干净基线 0.15%，脏包 2.51%）
  3. alpha==0 像素占比 > 50%（抽样首/中/尾帧；干净基线 81.8%，脏包 2.2%）
  4. ANIM 背景色 alpha == 0（防 dispose=TO_BG 闪白框）

用法：
  python tools/check_pack.py build/dsh-pet-300p15            # 包目录（找 webp/）
  python tools/check_pack.py build/dsh-pet-300p15/webp      # 或直接 webp 目录

依赖：Pillow（判据 2/3 需要）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS))

from inspect_webp_anim import fmt_bg, parse as container_parse  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

GHOST_RATIO = 0.005
ALPHA_MIN = 0.50
SAMPLE_FRAMES = (0, None, -1)  # 首/中/尾（None = 中间帧）


def alpha_check(path: Path) -> tuple[bool, str]:
    if Image is None:
        return True, "跳过（无 Pillow）"
    im = Image.open(path)
    n = getattr(im, "n_frames", 1)
    picks = []
    for spec in SAMPLE_FRAMES:
        idx = n // 2 if spec is None else (n + spec) % n
        if idx not in picks:
            picks.append(idx)
    im.close()
    for idx in picks:
        im = Image.open(path)
        im.seek(idx)
        hist = im.convert("RGBA").split()[3].histogram()
        im.close()
        total = sum(hist)
        share = hist[0] / total
        if share <= ALPHA_MIN:
            return False, f"帧{idx} alpha==0 仅 {share:.1%}（阈值 >{ALPHA_MIN:.0%}）"
    return True, f"alpha==0 最低 {share:.1%}（抽样 {picks}）"


def check_file(path: Path, visual: bool = True) -> tuple[bool, list[str]]:
    problems: list[str] = []
    # 判据 1 + 4：容器标志（纯字节解析，快，全量跑）
    data = path.read_bytes()
    info = container_parse(data)
    n = len(info["frames"])
    blended = sum(1 for f in info["frames"] if f["blend"])
    partial = sum(1 for f in info["frames"]
                  if f["rect"] != (0, 0, info["canvas"][0], info["canvas"][1]))
    if blended:
        problems.append(f"判据1 FAIL: {blended}/{n} 帧 blend=1")
    elif partial:
        problems.append(f"判据1 WARN: {partial}/{n} 局部帧（allow_mixed 型，需人工确认）")
    r = info["bgcolor"]
    if r is None or r[3] != 0:
        problems.append(f"判据4 FAIL: ANIM bg={fmt_bg(r)}（要求 alpha=0）")
    if not visual:
        return (not problems), problems
    # 判据 2：拖影（PIL 逐帧解码，慢，CI 里按抽样跑）
    if Image is not None:
        from check_webp_ghost import THRESHOLD_RATIO, check as ghost_check
        ok, msg = ghost_check(path)
        if not ok:
            problems.append(f"判据2 FAIL: 拖影 {msg}")
        else:
            print(f"    拖影 {msg}")
    else:
        print("    拖影检查跳过（无 Pillow）")
    # 判据 3：alpha
    ok, msg = alpha_check(path)
    if not ok:
        problems.append(f"判据3 {msg}")
    else:
        print(f"    {msg}")
    return (not problems), problems


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--sample")]
    sample = 0
    for a in argv:
        if a.startswith("--sample="):
            sample = int(a.split("=", 1)[1])
    roots = [Path(a) for a in args if not a.startswith("-")]
    if not roots:
        print(__doc__)
        return 2
    files: list[Path] = []
    for root in roots:
        webp_dir = root / "webp" if (root / "webp").is_dir() else root
        files.extend(sorted(webp_dir.glob("*.webp")))
    if not files:
        print(f"没找到 webp：{roots}", file=sys.stderr)
        return 2
    # --sample N：只对 N 个文件跑耗时的拖影/alpha 检查（容器判据仍全量）
    visual_set = set(files) if not sample else set(files[:: max(1, len(files) // sample)][:sample])
    failed = 0
    for p in files:
        ok, problems = check_file(p, visual=p in visual_set)
        mark = "PASS" if ok else "FAIL"
        print(f"{p.name}: {mark}")
        for q in problems:
            print(f"    {q}")
        failed += 0 if ok else 1
    total = len(files)
    print(f"\n门禁结果：{total - failed}/{total} 通过"
          f"（拖影/alpha 抽查 {len(visual_set)} 个）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
