# dsh-pet-mobile-assets

DSH Mobile（Deepseek-Harness-Local-Android）桌宠的**资源仓库**。
APK 里不带任何桌宠素材，需要桌宠的用户按需从本仓库 Release 下载资源包安装。

> **本仓库只存脚本与文档，不入库任何二进制。**
> 素材（webm → 动画 WebP）、打包（zip）、索引（index.json）全部由 CI 完成，产物只落在 Release 附件上。
> CI 不依赖本地环境：`dsh-pet@<ver>` 的 npm 发布包里就含 `assets/webm`，直接 curl 下载即可。

## 目录结构

```
tools/build-webp.py                    # webm → 动画 WebP + 每档生成 pet-config.json / manifest.json
tools/build-index.py                   # build/<id>/ → zip(store) + index.json
tools/estimate-size.py                 # 抽样转码估算各档全量体积（调档位时用）
tools/inspect_webp_anim.py             # 体检：dump 每帧 rect/duration/blend/dispose + ANIM 背景色
tools/fix-webp-blend.py                # 修容器：帧标志改 NO_BLEND + 背景色改全透明（默认只报告）
tools/check_webp_ghost.py              # 量化拖影：原标志合成 vs 强制 NO_BLEND 合成的逐帧差异
tools/check_pack.py                    # 发布门禁：blend / 拖影 / alpha / 背景色 四判据
.github/workflows/build-packs.yml      # 拉源 → 三档转码 → 门禁 → 打包 → 上传 Release
NOTICE.md                              # 素材许可说明
```

以下由 CI 生成，**已 gitignore**：

```
build/<pack-id>/                       # 每档一个自包含目录
  ├─ pet-config.json                   # 业务逻辑：pets/names/durations/animations/animationWeights/font
  ├─ manifest.json                     # 渲染元数据：resolution/fps/hitBox/refWidth + animations[]{slug,file,
  │                                    #   frames,durationMs,bytes,sha256} + assets{fonts,memes,pic}
  ├─ webp/a001.webp … a106.webp        # 动画 WebP（VP8X + alpha）
  └─ fonts/ memes/ pic/
packs/<pack-id>.zip                    # CI 本地暂存目录（打好的 zip，随后上传 Release，不入库）
index.json                             # App 拉取入口：包列表/规格/体积/sha256/下载地址
```

## 发布的三档

| 资源包 id | 规格 | 全量体积 |
| --- | --- | --- |
| `dsh-pet-300p15` | 300×168 @15fps | ~132 MB |
| `dsh-pet-480p24` | 480×270 @24fps | ~424 MB |
| `dsh-pet-640p24` | 640×360 @24fps | ~513 MB |

（源素材 106 段 / 25423 帧；体积按抽样 10 段真实转码外推，webp 已压缩故 zip 用 store 模式。**不分卷**：Release 附件单文件上限 2GB，最大档仅约一半。）

用户可以按手机性能与流量自己选：**轻量档**省内存和流量，**高清档**给高 DPI 屏。

## 触发方式

```bash
# 打 tag 自动构建并发布 Release
git tag v1.0.0 && git push origin v1.0.0

# 或 Actions 手动触发：可调 specs / quality，勾选 dry_run 只构建不上传
```

## 转换链路

桌面版 webm 是 **VP9 yuv420p、纯黑背景**（ffmpeg 读不出 WebM 的 BlockAdditional alpha，
只有浏览器/Electron 能播 alpha），所以转码时用 `colorkey` 抠掉黑幕再转成带 alpha 的动画 WebP：

```bash
ffmpeg -i 待机呼吸休闲.webm \
  -vf "colorkey=0x000000:0.06:0.10,fps=15,format=bgra,scale=300:-1:flags=lanczos" \
  -c:v libwebp -loop 0 -cpu-used 2 -deadline good -compression_level 4 -q:v 70 \
  build/dsh-pet-300p15/webp/a001.webp
python tools/fix-webp-blend.py build/dsh-pet-300p15/webp --apply --bg   # 关键：容器后处理
```

三处参数都是踩过坑的，别随手改回去：

| 参数 | 为什么 |
| --- | --- |
| `format=bgra`（不能是 yuva420p） | YUVA 导入路径会把 alpha=0 错写成 alpha=1（约 80% 画布），Android 合成时变成盖住角色的灰黑底框 |
| `blend=0.10` + `flags=lanczos` | `blend=0.02` + `bicubic` 是近乎二值的硬边，300p 下降采样后锯齿非常明显；0.10 才有 alpha 渐变带（0.14 起收益趋零、细发丝反而发虚） |
| `deadline good -cpu-used 2 -q:v 70` | realtime/cpu-used 5 的纹理损耗肉眼可见，四段全量重编也就几十秒 |

容器后处理（`fix-webp-blend.py`）是**必须**的一步：libwebp 的 `WebPAnimEncoder` 给每帧写
`BLEND + 不清回背景 + 整画布矩形`，而每帧本身是完整画面，播放器逐帧叠加 → **越播越脏、身后黑雾**。
只改 ANMF 的一个标志字节即可修掉，不重编码、画质零损、幂等；同时把 ANIM 背景色从不透明白改成
全透明，防止将来有人改 `dispose=TO_BG` 时闪白框。

> 体积由**总帧数**主导，画质参数影响很小：降帧率几乎线性降体积，降 `-q:v` 收益有限。

本地想预跑一遍（只需 ffmpeg + Python）：

```bash
python tools/build-webp.py \
  --src  "C:/Users/lzh17/.dsh/profiles/desktop/node_modules/dsh-pet" \
  --out  build --specs 300x15,480x24,640x24 --jobs 4 \
  --ffmpeg "D:/Tools/ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe"
python tools/build-index.py --build-root build --out packs --index index.json
```

## App 侧约定

- 下载地址：`https://github.com/XiaoPb/dsh-pet-mobile-assets/releases/latest/download/index.json`；
- `index.json` 中 `url` 是**裸附件名**（`<id>.zip`）：Release 附件平铺在同一层，
  App 按 index.json 自身所在目录拼接。带 `packs/` 之类的目录前缀会 404；
- `parts` 恒为空数组 —— Release 单文件直传，App 侧用 HTTP Range 断点续传兜底大文件；
- 每个包条目带 `version`（整数，取发布 tag 末段，如 v1.0.3 → 3）：**不递增的话，
  已安装的设备不会重下**；同时带 `webpFlags: {blend: false, dispose: "none"}` 做自描述体检；
- 包内必须含 `pet-config.json` 与 `webp/<slug>.webp`，可选 `fonts/`、`memes/`、`pic/`；
- App（`PetPackInstaller`）只解这些内容，装到 `files/pet/packs/<id>/`，
  多个包可共存、设置页可切换删除。

## 许可

动画素材与字体来自 PC2005-cloud/dsh-pet（含桌面版自带资源）：允许开源使用、**禁止商用**，详见 `NOTICE.md`。
