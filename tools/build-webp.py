#!/usr/bin/env python3
"""把桌面版 dsh-pet 的 assets/webm 转成安卓可用的动画 WebP（保留 alpha），
并为每个「分辨率×帧率」档生成一份自包含的资源包目录。

素材来源：**不依赖本地环境** —— CI 里 `npm pack dsh-pet@<ver>` 拿到发布包即含 assets/webm，
本仓库因此不入库任何二进制（见 .gitignore）。

转换链路：
  源 assets/webm/*.webm
    → ffmpeg colorkey 抠掉纯黑底（源为 VP9 yuv420p，alpha 读不出来）
    → 动画 WebP（VP8X + alpha）
    → build/<pack-id>/webp/a001.webp … aNNN（编号由 pet-config 的出场顺序决定）

用法：
  python tools/build-webp.py \
    --src dsh-pet-src/package \
    --out build \
    --specs 300x15,480x24,640x24 \
    --jobs 4

产出（每档一个子目录，各自 self-contained）：
  build/dsh-pet-300p15/{pet-config.json,manifest.json,webp/,fonts/,memes/,pic/}
  build/dsh-pet-480p24/…
  build/dsh-pet-640p24/…

许可：素材来自 PC2005-cloud/dsh-pet，允许开源使用、禁止商用（见 NOTICE.md）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 与桌面版 src/shared/constants.ts 一致
PET_REF_WIDTH = 462
HIT_BOX = {"x0": 200, "y0": 50, "x1": 440, "y1": 335}  # 640×360 坐标系里的身体矩形

# 抠像参数：素材是**纯黑底**，similarity 收紧到只吃纯黑，
# 避免误伤角色里的深色细节（实测：与黑底互斥的内部黑像素约 295/230400，肉眼不可见）。
# blend 控制边缘 alpha 渐变带宽度：0.02 时近乎二值硬边（锯齿严重，2026-10-27 实测），
# 0.10 配合 lanczos 缩放边缘最平滑；0.14 起边际收益趋零还可能让细发丝半透明。
KEY_FILTER = "colorkey=0x000000:{sim}:{blend}"

# 静态资源目录：每档各复制一份，保证 zip 自包含
STATIC_DIRS = ("fonts", "memes", "pic")

DEFAULT_SPECS = "300x15,480x24,640x24"


def parse_specs(text: str) -> list[tuple[int, int]]:
    """解析 '300x15,480x24' → [(300, 15), (480, 24)]。"""
    specs: list[tuple[int, int]] = []
    for raw in text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)", raw)
        if not m:
            raise SystemExit(f"无法解析档位 {raw!r}，应为 <宽度>x<帧率>，如 300x15")
        width, fps = int(m.group(1)), int(m.group(2))
        if width < 16 or fps < 1:
            raise SystemExit(f"档位不合法: {raw!r}")
        specs.append((width, fps))
    if not specs:
        raise SystemExit("没有有效档位")
    return specs


def pack_id(width: int, fps: int) -> str:
    return f"dsh-pet-{width}p{fps}"


def even(n: int) -> int:
    return n - (n % 2)


def out_height(src_w: int, src_h: int, width: int) -> int:
    """按源宽高比算输出高度，并保证偶数（yuva420p 要求）。"""
    return even(round(width * src_h / src_w))


def strip_jsonc(text: str) -> str:
    """去掉 // 与 /* */ 注释（不能处理字符串里的 //，dsh-pet 的配置不会出现）。"""
    out: list[str] = []
    i, in_str, quote = 0, False, ""
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str, quote = True, c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if c == "/" and i + 1 < len(text) and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = i + 2 if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_src_config(src: Path) -> dict:
    cfg = src / "assets" / "config.jsonc"
    return json.loads(strip_jsonc(cfg.read_text(encoding="utf-8")))


def ordered_animations(cfg: dict) -> tuple[list[str], dict]:
    """按语义顺序收集动画名，返回 (有序列表, 分类信息)。"""
    a = cfg.get("animations", {})
    names: list[str] = []

    def add(item):
        if isinstance(item, str) and item not in names:
            names.append(item)

    for s in a.get("idle", []):
        add(s)
    for s in a.get("turn", []):
        add(s)
    for s in a.get("drag", []):
        add(s)
    for s in a.get("clicks", []):
        add(s)
    for m in a.get("moves", {}).get("actions", []):
        add(m.get("name"))
    for cat in a.get("categories", []):
        for s in cat.get("actions", []):
            add(s)
    ev = a.get("events", {})
    for key in ("balance", "whisper", "workStatus"):
        val = ev.get(key) or []
        flat = val if all(isinstance(v, str) for v in val) else [v for slot in val for v in slot]
        for s in flat:
            add(s)
    return names, a


def probe(ffprobe: str, path: Path) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    data = json.loads(subprocess.run(cmd, capture_output=True, check=True).stdout)
    st = (data.get("streams") or [{}])[0]
    fps_s = st.get("r_frame_rate", "24/1")
    n, d = fps_s.split("/") if "/" in fps_s else (fps_s, "1")
    fps = round(int(n) / max(int(d), 1))
    frames = int(st.get("nb_frames", 0) or 0)
    duration = float((data.get("format") or {}).get("duration", 0.0) or 0.0)
    if not frames:
        frames = max(1, round(duration * fps))
    return {
        "width": int(st.get("width", 640)),
        "height": int(st.get("height", 360)),
        "fps": fps,
        "frames": frames,
        "durationMs": int(round((frames / fps) * 1000)) if fps else 0,
    }


def convert(
    ffmpeg: str, webm: Path, dest: Path,
    width: int, fps: int, quality: int, sim: str, blend: str,
    scale_flags: str = "lanczos",
) -> None:
    """黑幕抠像 → 动画 WebP(alpha)。fps 用于抽帧降速：体积几乎与总帧数成正比。

    **必须用 bgra 而不是 yuva420p 喂给 libwebp**：ffmpeg 的 YUVA 导入路径会把
    alpha=0 的像素错写成 alpha=1（实测 300×169 画布约 80% 像素中招，无论
    blend 是否为 0、是否 lossless 都会出现）。这些 alpha=1 的黑色像素在
    Chrome/桌面上肉眼不可见，但 Android 的 AnimatedImageDrawable 合成时会
    被放大成一块盖住整个画布的灰黑色底框。

    缩放用 lanczos + colorkey blend≥0.08：300p 是 2 倍级降采样，bicubic 硬边
    会保留 colorkey 的锯齿台阶（2026-10-27 六倍放大实测，lanczos+blend.10 最平滑）。
    编码用 good/cpu-used 2：realtime 档纹理损耗明显，四段动画全量重编也就慢几十秒。

    编码完再跑两道容器 patch（毫秒级、幂等）：
      1. [patch_noblend]  帧标志 BLEND → NO_BLEND，消除逐帧叠加导致的拖影
         （libwebp 的 WebPAnimEncoder 给每帧写「BLEND + 不清回背景 + 整画布
         矩形」，而每一帧自己又是**完整画面**，叠加就是拖影）
      2. [patch_bgcolor]  ANIM 背景色不透明白 → 全透明，防 dispose=TO_BG 闪白框
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = KEY_FILTER.format(sim=sim, blend=blend)
    vf += f",fps={fps},format=bgra,scale={width}:-1:flags={scale_flags}"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(webm), "-vf", vf,
            "-c:v", "libwebp", "-loop", "0",
            "-cpu-used", "2", "-deadline", "good", "-compression_level", "4",
            "-q:v", str(quality), str(dest),
        ],
        check=True,
    )
    patched = patch_noblend(dest)
    if patch_bgcolor(dest):
        patched += 1
    if patched:
        print(f"    {dest.name}: 容器 patch ×{patched}")


def patch_noblend(path: Path) -> int:
    """把每帧的 ANMF 标志从 BLEND 改成 NO_BLEND（覆盖而非叠在上一帧上）。

    只对「帧矩形 == 整张画布」的帧安全；遇到子矩形帧（说明编码器做了增量
    优化、依赖上一帧）就原样保留并交回计数，由调用方决定是否告警。
    返回改写的帧数。
    """
    data = bytearray(path.read_bytes())
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return 0
    canvas = None
    off = 12
    changed = 0
    while off + 8 <= len(data):
        fourcc = bytes(data[off:off + 4])
        size = struct.unpack("<I", bytes(data[off + 4:off + 8]))[0]
        body = off + 8
        if fourcc == b"VP8X":
            canvas = (u24(data[body + 4:body + 7]) + 1, u24(data[body + 7:body + 10]) + 1)
        elif fourcc == b"ANMF":
            flags_at = body + 15
            flags = data[flags_at]
            full = canvas is not None and (
                u24(data[body:body + 3]) == 0 and u24(data[body + 3:body + 6]) == 0
                and u24(data[body + 6:body + 9]) + 1 == canvas[0]
                and u24(data[body + 9:body + 12]) + 1 == canvas[1]
            )
            if full and not flags & 0x02:
                data[flags_at] = flags | 0x02
                changed += 1
        off += 8 + size + (size & 1)
    if changed:
        path.write_bytes(data)
    return changed


def u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def patch_bgcolor(path: Path) -> bool:
    """把 ANIM 背景色从不透明白（#FFFFFFFF）改成全透明（#00000000）。

    dispose=NONE 时背景色无害，但一旦有人把 dispose 改成 TO_BG 就会闪白框；
    预先改成透明零风险。返回是否修改。
    """
    data = bytearray(path.read_bytes())
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    off = 12
    changed = False
    while off + 8 <= len(data):
        fourcc = bytes(data[off:off + 4])
        size = struct.unpack("<I", data[off + 4:off + 8])[0]
        body = off + 8
        if fourcc == b"ANIM" and bytes(data[body:body + 4]) != b"\x00\x00\x00\x00":
            data[body:body + 4] = b"\x00\x00\x00\x00"
            changed = True
        off += 8 + size + (size & 1)
    if changed:
        path.write_bytes(data)
    return changed


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_static(src: Path, dest_root: Path) -> dict[str, list[str]]:
    """复制 fonts/ memes/ pic/，返回各目录的文件名清单。"""
    assets: dict[str, list[str]] = {}
    for d in STATIC_DIRS:
        s = src / "assets" / d
        names: list[str] = []
        if s.is_dir():
            t = dest_root / d
            t.mkdir(parents=True, exist_ok=True)
            for f in sorted(s.iterdir()):
                if f.is_file():
                    shutil.copy2(f, t / f.name)
                    names.append(f.name)
        assets[d] = names
    return assets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path, help="解包后的 dsh-pet 目录（含 assets/webm）")
    ap.add_argument("--out", required=True, type=Path, help="构建根目录，每档生成 <out>/<pack-id>/")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default=None)
    ap.add_argument("--specs", default=DEFAULT_SPECS,
                    help="输出档位，逗号分隔：<宽度>x<帧率>，默认 " + DEFAULT_SPECS)
    ap.add_argument("--quality", type=int, default=70, help="webp 质量 0~100")
    ap.add_argument("--sim", default="0.06", help="colorkey similarity（0~1，越小越只吃纯黑）")
    ap.add_argument("--blend", default="0.10",
                    help="colorkey blend（边缘 alpha 渐变带宽度，0.02=硬边锯齿，0.10=平滑）")
    ap.add_argument("--scale-flags", default="lanczos",
                    help="缩放算法（降采样推荐 lanczos/area，bicubic 会保留锯齿）")
    ap.add_argument("--jobs", type=int, default=4, help="并行转换进程数")
    ap.add_argument("--only", type=int, default=0, help="只处理前 N 段（调试用）")
    ap.add_argument("--names", default="",
                    help="只处理指定动画（逗号分隔的动画名，保持配置顺序）；用于增量重编既有包")
    ap.add_argument("--pet-name", default="蓝色大肥鱼")
    args = ap.parse_args()

    ffprobe = args.ffprobe or (
        str(Path(args.ffmpeg).with_name("ffprobe.exe"))
        if args.ffmpeg.endswith(".exe") else str(Path(args.ffmpeg).parent / "ffprobe")
    )
    src: Path = args.src
    webm_dir = src / "assets" / "webm"
    if not webm_dir.is_dir():
        print(f"webm 目录不存在: {webm_dir}", file=sys.stderr)
        return 1

    specs = parse_specs(args.specs)
    cfg = load_src_config(src)
    all_names, anim_cfg = ordered_animations(cfg)
    slugs = {name: f"a{i + 1:03d}" for i, name in enumerate(all_names)}
    if args.only:
        keep = all_names[: args.only]
        slugs = {n: slugs[n] for n in keep}
    if args.names:
        wanted = [n.strip() for n in args.names.split(",") if n.strip()]
        missing = [n for n in wanted if n not in slugs]
        if missing:
            raise SystemExit(f"配置里没有这些动画: {missing}")
        slugs = {n: slugs[n] for n in wanted}
    print(f"{len(slugs)}/{len(all_names)} 段动画，档位 {len(specs)} 个："
          f"{', '.join(f'{w}x{f}' for w, f in specs)}")

    # ---- 探测源素材元数据（各档共用） ----
    print("探测素材元数据…")
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(lambda it: (it[0], probe(ffprobe, it[1])),
                              [(n, webm_dir / f"{n}.webm") for n in slugs]))
    metas = {slugs[n]: m for n, m in results}
    for n in slugs:
        if not (webm_dir / f"{n}.webm").is_file():
            print(f"缺少素材: {n}.webm", file=sys.stderr)
            return 1
    base_meta = metas[list(metas)[0]]
    total_ms = sum(m["durationMs"] for m in metas.values())

    for width, fps in specs:
        pid = pack_id(width, fps)
        root = args.out / pid
        height = out_height(base_meta["width"], base_meta["height"], width)
        webp_out = root / "webp"
        webp_out.mkdir(parents=True, exist_ok=True)
        print(f"\n[{pid}] {width}x{height} @{fps}fps  q{args.quality}")

        jobs = [(name, slug) for name, slug in slugs.items()]
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(
                lambda it: convert(args.ffmpeg, webm_dir / f"{it[0]}.webm",
                                   webp_out / f"{it[1]}.webp",
                                   width, fps, args.quality, args.sim, args.blend,
                                   args.scale_flags),
                jobs))

        total_bytes = 0
        for _, slug in jobs:
            f = webp_out / f"{slug}.webp"
            if not f.is_file():
                print(f"转换失败: {f.name}", file=sys.stderr)
                return 1
            total_bytes += f.stat().st_size

        assets = copy_static(src, root)

        # ---- pet-config.json（业务逻辑） ----
        a = anim_cfg
        moves = a.get("moves", {})
        defaults = moves.get("default", {})
        move_actions = [{
            "slug": slugs[m["name"]],
            "name": m["name"],
            "minDist": (m.get("params", {}) or {}).get("minDist", defaults.get("minDist", 60)),
            "maxDist": (m.get("params", {}) or {}).get("maxDist", defaults.get("maxDist", 240)),
            "leadSec": (m.get("params", {}) or {}).get("leadSec", defaults.get("leadSec", 2)),
            "tailSec": (m.get("params", {}) or {}).get("tailSec", defaults.get("tailSec", 2)),
        } for m in moves.get("actions", []) if m.get("name") in slugs]

        events = a.get("events", {})

        def slots(key):
            val = events.get(key) or []
            if val and all(isinstance(v, str) for v in val):
                return [slugs.get(v) for v in val if v in slugs]
            out_list = []
            for slot in val:
                picked = [slugs.get(v) for v in (slot if isinstance(slot, list) else [slot])]
                out_list.append([p for p in picked if p])
            return out_list

        pet_config = {
            "pets": [{
                "id": "main",
                "name": args.pet_name,
                "size": width,
                "corner": "top-right",
                "marginX": 24,
                "marginY": 100,
            }],
            "names": {slug: name for name, slug in slugs.items()},
            "durations": {slug: metas[slug]["durationMs"] for slug in slugs.values()},
            "animations": {
                "idle": [slugs[s] for s in a.get("idle", []) if s in slugs],
                "turn": [slugs[s] for s in a.get("turn", []) if s in slugs],
                "drag": [slugs[s] for s in a.get("drag", []) if s in slugs],
                "clicks": [slugs[s] for s in a.get("clicks", []) if s in slugs],
                "moves": {"actions": move_actions},
                "categories": [{
                    "id": c.get("id", "actions"),
                    "weight": c.get("weight", 10),
                    "noMirror": bool(c.get("noMirror", False)),
                    "actions": [slugs[s] for s in c.get("actions", []) if s in slugs],
                } for c in a.get("categories", [])],
                "events": {
                    "workStatus": slots("workStatus"),
                    "balance": slots("balance"),
                    "whisper": slots("whisper"),
                },
            },
            "animationWeights": cfg.get("animationWeights", {"idle": 10, "turn": 5, "move": 5}),
            "font": "上首软糖体.ttf",
        }
        (root / "pet-config.json").write_text(
            json.dumps(pet_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        # ---- manifest.json（资源清单 + 渲染元数据） ----
        manifest = {
            "version": 1,
            "id": pid,
            "name": f"{args.pet_name} {width}p",
            "source": "PC2005-cloud/dsh-pet (webm -> animated webp)",
            # 给 index/UI 看的规格串：App 设置页会拼在包名后面
            "resolution": f"{width}x{height} @{fps}fps",
            "width": width,
            "height": height,
            "fps": fps,
            "refWidth": PET_REF_WIDTH,
            "hitBox": HIT_BOX,
            # 自描述帧标志：本包已通过容器 patch 门禁（见 tools/check_pack.py）
            "webpFlags": {"blend": False, "dispose": "none"},
            "animations": [{
                "slug": slug,
                "file": f"webp/{slug}.webp",
                "name": name,
                "frames": metas[slug]["frames"],
                "durationMs": metas[slug]["durationMs"],
                "bytes": (webp_out / f"{slug}.webp").stat().st_size,
                "sha256": sha256(webp_out / f"{slug}.webp"),
            } for name, slug in slugs.items()],
            "assets": assets,
            "totalBytes": total_bytes,
            "totalDurationMs": total_ms,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  完成 {len(slugs)} 段，{total_bytes / 1024 / 1024:.1f} MB → {root}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
