# Third-Party Notices

本项目（scene-cut）自身代码以 **MIT** 许可发布，见 `LICENSE`。
下列第三方组件随本项目使用或分发，其许可证与义务如下。分发二进制时必须随包提供
本文件与对应许可证全文（或链接）。

| 组件 | 版本 | 许可证 | 义务 / 备注 |
|---|---|---|---|
| PySceneDetect | 0.7.2 | BSD-3-Clause | 保留版权声明与许可文本；https://www.scenedetect.com/copyright/ |
| TransNetV2（PyTorch 实现，allenday） | 1.0.5 | MIT | 保留版权声明 |
| TransNetV2 原实现与权重（Souček & Lokoč, 2020） | — | MIT | 保留版权声明；注明模型来源与用途限制 |
| opencv-python | 4.6.0.66 | Apache-2.0 | 需附 NOTICE；https://github.com/opencv/opencv-python |
| numpy | 1.23.5 | BSD-3-Clause（含 0BSD/MIT/Zlib/CC0 混合） | 保留版权声明 |
| Pillow | 9.3.0 | HPND（PIL 许可） | 保留版权声明 |
| tqdm | 4.66.1 | MIT / MPL-2.0 双许可 | 保留版权声明 |
| PyInstaller | 6.9.0 | GPLv2-or-later **含打包例外** | 仅构建工具，其例外明确允许打包专有/其它许可程序；不进产物 |
| Python 运行时 | 3.10 | PSF-2.0 | 打包时附 LICENSE |
| Microsoft.Web.WebView2 SDK | 1.0.4314.35 | Microsoft 软件许可（可再分发） | 仅使用 `Microsoft.Web.WebView2.Core.dll` / `WinForms.dll` / `WebView2Loader.dll`；NuGet 包内含许可条款 |
| Microsoft Edge WebView2 Runtime | 系统安装（156.x） | Microsoft 专有，随 Windows 分发 | **不随本项目分发**，由系统提供；缺失时从微软官网安装 |
| ffmpeg | 发布用 **LGPL** 构建 | LGPL-2.1+ | 分发时必须提供对应源码或源码链接，并保持可替换；**发布产物不得使用 GPL 构建**（本机开发机可用 GPL 构建，但不可分发） |
| torch（可选，GPU） | 2.0.1 | BSD-3 风格 / Apache-2.0 混合 | **默认不随包分发**；由安装器在用户机器上按需下载 |

## 关于 ffmpeg

- 本项目的 release 产物附带 `ffmpeg` **LGPL** 构建（不含 x264/x265）。
- 使用 `NVENC` / `QSV` 硬件编码不依赖 GPL 编码器。
- 若你需要 x264/x265，请自行安装系统 ffmpeg（GPL 构建），本项目不随包提供。

## 关于模型权重

- `transnetv2-pytorch-weights.pth` 由 `transnetv2-pytorch` 包提供，源自 TransNetV2（MIT）。
- 未来若替换为 AutoShot 等权重许可不明的模型，**在取得书面许可前不得随包分发**。

## 专利声明

H.264 / HEVC / AAC 等编解码标准涉及第三方专利池（如 MPEG LA、Access Advance）。
本工具仅调用用户环境中的 ffmpeg；是否以及如何获得相关专利许可，由使用者自行负责。
本项目不随包附带任何 GPL 编解码器二进制。
