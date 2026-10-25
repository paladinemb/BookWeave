# 桌面安装包发布

`.github/workflows/release.yml` 在 GitHub 分别启动四个平台的构建。无需把模型或现成 App 提交到源码仓库。

| 安装包 | 系统 | 识别运行方式 |
| --- | --- | --- |
| `BookWeave-VERSION-macos-arm64.dmg` | Apple Silicon，macOS 14+ | Metal GPU |
| `BookWeave-VERSION-macos-x64.dmg` | Intel，macOS 14+ | CPU |
| `BookWeave-VERSION-windows-x64-setup.exe` | Windows 10/11 x64，支持 AVX2 | CPU |
| `BookWeave-VERSION-linux-x64.deb` | Ubuntu 24.04+／兼容 Debian x64，支持 AVX2 | CPU |

Windows／Linux 使用共用转换核心的桌面界面，支持分段进度、原 PDF 页码、缓存继续和自动校对报告。资源读数不可用时保守采用单任务。手机端不包含在本次桌面发布中。

## 自动发布

修改 `mac-app/Info.plist` 中的三段版本号（如 `2.4.1`）和递增 build 号并推送 main，即触发发布。也可在 Actions → Build and publish desktop installers → Run workflow 手动启动。

流程先建立草稿 Release，再构建并验证四个平台；每个成功的平台把安装包和校验文件上传到草稿。全部成功后统一公开发布。中途失败时保留草稿，公开页不会出现只有一半平台的正式版本。修复后重新运行或推送工作流变更可继续；已公开版本禁止覆盖，下一次发布必须递增版本。

官方模型和 Python／llama.cpp 运行资源的 SHA256 固定在 `release/resources.json`。校验不符就停止，不能用空壳或缺模型的包代替。模型缓存只影响构建速度，最终安装包包含离线所需模型和运行环境。

发布包中不得包含用户 PDF、转换结果、个人缓存、访问令牌或原开发电脑路径。CI 使用仓库内的合成 PDF 测试，包含真实 OCR 和 EPUB 结构校验；这不等同于长篇扫描书全书准确率验收。

## 安装

macOS：打开 `.dmg`，把 BookWeave 拖入 Applications。

Windows：运行 `-setup.exe`，默认安装到当前用户目录，建立开始菜单／桌面快捷方式，可在系统中卸载。

Ubuntu／Debian：`sudo apt install ./BookWeave-VERSION-linux-x64.deb`，安装后在应用菜单打开。

安装包未做 Apple Developer ID 公证或 Windows Authenticode 签名，首次运行可能受到系统来源检查。校验值与正式代码签名的作用不同；下载校验文件不代表通过系统签名认证。

每个安装包必须小于 GitHub 的 2 GiB 单文件限制。`*.sha256` 与 `SHA256SUMS` 只用于校验，自动生成的 Source code 压缩包是源码，都不计入安装包数量。
