#!/usr/bin/env python3
"""抽样估算各档分辨率/帧率组合下全套资源包的总体积。

做法：先 ffprobe 全部源素材拿总帧数，再按帧数分层抽样若干段做真实转换，
求出「每帧字节数」，最后乘以全集总帧数外推。

用法：
  python tools/estimate-size.py --src <桌面版目录> --ffmpeg <ffmpeg路径> \
      --samples 10 --tmp .tmp-estimate
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# (宽度, 帧率, quality) —— 与 build-webp.py 的转换参数保持一致
SPECS = [
    (300, 15, 60),
    (480, 24, 60),
    (640, 24, 60),
]


def probe(ffprobe: str, path: Path) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
        "-show_entries", "format=duration", "-of", "json", str(path),
    ]
    data = json.loads(subprocess.run(cmd, capture_output=True, check=True).stdout)
    st = (data.get("streams") or [{}])[0]
    fps_s = st.get("r_frame_rate", "24/1")
    n, d = fps_s.split("/") if "/" in fps_s else (fps_s, "1")
    fps = round(int(n) / max(int(d), 1))
    frames = int(st.get("nb_frames", 0) or 0)
    if not frames:
        frames = max(1, round(float((data.get("format") or {}).get("duration", 0) or 0) * fps))
    return {"width": int(st.get("width", 640)), "height": int(st.get("height", 360)),
            "fps": fps, "frames": frames}


def convert(ffmpeg: str, webm: Path, out: Path, width: int, fps: int, quality: int) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"colorkey=0x000000:0.06:0.02,fps={fps},format=yuva420p,scale={width}:-1"
          if fps else f"colorkey=0x000000:0.06:0.02,format=yuva420p,scale={width}:-1")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(webm), "-vf", vf,
         "-c:v", "libwebp", "-loop", "0", "-cpu-used", "5",
         "-deadline", "realtime", "-compression_level", "0",
         "-q:v", str(quality), str(out)],
        check=True, capture_output=True)
    return out.stat().st_size if out.is_file() else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default=None)
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--tmp", type=Path, default=Path(".tmp-estimate"))
    args = ap.parse_args()

    ffprobe = args.ffprobe or str(Path(args.ffmpeg).parent / "ffprobe.exe")
    webm_dir: Path = args.src / "assets" / "webm"
    files = sorted(webm_dir.glob("*.webm"))
    if not files:
        print(f"没有素材: {webm_dir}", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=8) as ex:
        metas = list(ex.map(lambda p: probe(ffprobe, p), files))

    total_frames_all = sum(m["frames"] for m in metas)
    print(f"全集 {len(files)} 段，源总帧数 {total_frames_all}，"
          f"源分辨率 {metas[0]['width']}x{metas[0]['height']}@{metas[0]['fps']}fps")

    # 按帧数分层抽样：覆盖短/中/长，避免只取样到某一档长度
    order = sorted(range(len(files)), key=lambda i: metas[i]["frames"])
    idxs = [order[int(i * (len(order) - 1) / max(args.samples - 1, 1))]
            for i in range(args.samples)]
    idxs = sorted(set(idxs))

    results = []
    for width, fps, quality in SPECS:
        sample_frames = 0
        sample_bytes = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            jobs = [(i, files[i]) for i in idxs]
            sizes = list(ex.map(
                lambda j: convert(args.ffmpeg, j[1],
                                  args.tmp / f"{width}_{fps}" / f"{j[1].stem}.webp",
                                  width, fps, quality), jobs))
        for (i, _), size in zip(jobs, sizes):
            # 转换后的实际帧数按输出帧率折算
            out_frames = max(1, round(metas[i]["frames"] / metas[i]["fps"] * fps))
            sample_frames += out_frames
            sample_bytes += size
        per_frame = sample_bytes / max(sample_frames, 1)
        est_total_frames = sum(max(1, round(m["frames"] / m["fps"] * fps)) for m in metas)
        est = per_frame * est_total_frames
        results.append({
            "width": width, "height": round(width * 360 / 640), "fps": fps,
            "est_mb": round(est / 1024 / 1024, 1),
            "per_frame_kb": round(per_frame / 1024, 2),
            "out_frames": est_total_frames,
        })
        print(f"{width}x{round(width*360/640)} @{fps}fps  q{quality}: "
              f"每帧 {per_frame/1024:.2f} KB → 全量约 {est/1024/1024:.1f} MB")

    print("\n=== 汇总（未压缩 zip）===")
    for r in results:
        flag = "需分卷(>95MB)" if r["est_mb"] > 95 else "单文件 OK"
        print(f"{r['width']}x{r['height']} @{r['fps']}fps : {r['est_mb']} MB  ({flag})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
