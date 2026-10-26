#!/usr/bin/env python3
"""把 build/<pack-id>/ 打成可分发的 zip，并生成 index.json。

要点：
  - **不分卷**：产物只通过 GitHub Release 发布，附件单文件上限 2GB，
    我们最大档约 0.5GB，远低于上限，因此整包单文件直传（App 侧用 HTTP Range 断点续传兜底）。
  - zip 用 store 模式：webp 自身已压缩，再压几乎不减体积却很慢。
  - index.json 描述每个包：id / name / resolution / fps / size / sha256 / url，
    App 侧 PetPackDownloader 按 index 的基地址解析 url。

用法：
  # 打包 build/ 下的全部档位
  python tools/build-index.py --build-root build --out packs --index index.json

  # 只打包某一档，并合并进已有 index
  python tools/build-index.py --build-root build --out packs --index index.json \
      --filter dsh-pet-300p15
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

GITHUB_LIMIT = 2 * 1024 * 1024 * 1024  # Release 附件单文件上限 2GB


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_dir(src: Path, dest: Path) -> int:
    """store 模式打包，返回 webp 动画数量。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    files = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, f.relative_to(src).as_posix())
            if f.parent.name == "webp" and f.suffix == ".webp":
                count += 1
    return count


def load_index(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "updatedAt": None, "packs": []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-root", required=True, type=Path, help="build-webp.py 的产物根目录")
    ap.add_argument("--out", required=True, type=Path, help="zip 输出目录")
    ap.add_argument("--index", default="index.json", type=Path)
    ap.add_argument("--filter", default="", help="只处理这些包（逗号分隔的目录名）")
    ap.add_argument(
        "--release-tag",
        default="",
        help="兼容旧参数，已忽略：索引统一写附件文件名（Release 附件平铺），由 App 按 index 基址拼接",
    )
    args = ap.parse_args()

    build_root: Path = args.build_root
    if not build_root.is_dir():
        print(f"构建目录不存在: {build_root}", file=sys.stderr)
        return 1

    wanted = {s.strip() for s in args.filter.split(",") if s.strip()}
    dirs = sorted(d for d in build_root.iterdir() if (d / "manifest.json").is_file())
    if wanted:
        dirs = [d for d in dirs if d.name in wanted]
    if not dirs:
        print(f"{build_root} 下没有含 manifest.json 的包目录", file=sys.stderr)
        return 1

    index = load_index(args.index)
    by_id = {p.get("id"): p for p in index.get("packs", [])}

    for d in dirs:
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        pid = manifest.get("id") or d.name
        zip_path: Path = args.out / f"{pid}.zip"
        animations = zip_dir(d, zip_path)
        size = zip_path.stat().st_size
        sha = sha256_of(zip_path)
        # Release 附件是平铺的（所有 zip 都在同一层），索引里只写文件名，
        # 由 App 用 index.json 自身的基址拼接。写成 "packs/xxx.zip" 会 404。
        url = zip_path.name

        entry = {
            "id": pid,
            "name": manifest.get("name", pid),
            "resolution": manifest.get("resolution", ""),
            "fps": int(manifest.get("fps", 24)),
            "width": int(manifest.get("width", 0)),
            "height": int(manifest.get("height", 0)),
            "animations": animations,
            "size": size,
            "sha256": sha,
            "font": (manifest.get("assets", {}).get("fonts") or [None])[0],
            "url": url,
            "parts": [],  # Release 单文件直传，不做分卷
        }
        by_id[pid] = entry
        mb = size / 1024 / 1024
        over = "  ⚠ 超过 GitHub 2GB 附件上限" if size > GITHUB_LIMIT else ""
        print(f"{pid}: {animations} 段动画，{mb:.1f} MB，{zip_path.name}{over}")

    kept = [d for d in dirs if (d / "manifest.json").is_file()]
    order = {name: i for i, name in enumerate(sorted(by_id))}
    index["packs"] = sorted(by_id.values(), key=lambda p: order.get(p.get("id", ""), 0))
    index["version"] = 1
    index["updatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.index.parent.mkdir(parents=True, exist_ok=True)
    args.index.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    total = sum(p["size"] for p in index["packs"]) / 1024 / 1024
    print(f"\nindex → {args.index}（{len(index['packs'])} 个包，合计 {total:.1f} MB）"
          f" {len(kept)} 个目录已打包")
    return 0


if __name__ == "__main__":
    sys.exit(main())
