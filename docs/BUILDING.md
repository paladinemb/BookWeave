# 构建 BookWeave.app

需要 Apple Silicon Mac、macOS 14+ 和 Xcode Command Line Tools（`xcode-select --install`）。构建过程不安装系统服务，也不修改已有 App。

## 1. 准备运行资源

已有 BookWeave.app 时，可以直接复用其 `Contents/Resources`，然后跳到第 2 步。

从零准备时，建立以下目录（不要提交到 Git）：

```text
runtime-resources/
  python-runtime/bin/python3.12
  paddle-engine/runtime/llama-b11814/llama-server
  paddle-engine/models/PaddleOCR-VL-1.6-GGUF.gguf
  paddle-engine/models/PaddleOCR-VL-1.6-GGUF-mmproj.gguf
```

1. 从 [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases) 获取 macOS arm64 的 CPython 3.12 install-only 发行包，将解压后的完整 `python` 目录作为 `python-runtime`。在该 Python 中安装 `requirements.txt` 的依赖。普通虚拟环境依赖原机器的 Python，不适合作为可搬移的 App 运行时。
2. 从 [llama.cpp b11814](https://github.com/ggml-org/llama.cpp/releases/tag/b11814) 获取 macOS arm64 运行时，将完整目录放在上述位置，保留动态库、Metal 资源和许可证；只复制 `llama-server` 不够。
3. 从 [PaddlePaddle 官方 GGUF 仓库](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF) 下载上述两个文件，保留模型的许可证和说明。

```sh
runtime-resources/python-runtime/bin/python3.12 -m pip install -r requirements.txt
```

## 2. 构建

在仓库根目录运行以下命令之一：

```sh
bash scripts/build_app.sh runtime-resources
# 或复用已安装的运行资源：
bash scripts/build_app.sh /Applications/BookWeave.app/Contents/Resources
```

生成 `dist/BookWeave.app`。目标已存在时构建会停止，避免覆盖；也可用第二个参数指定一个新的 `.app` 路径。

```sh
dist/BookWeave.app/Contents/MacOS/BookWeave --self-test
BOOKWEAVE_APP="$PWD/dist/BookWeave.app" python -B -m unittest discover -s mac-app/tests
open dist/BookWeave.app
```

构建使用本机临时签名；尚未提供 Apple Developer ID 签名和公证。对外分发应用包时，应另外完成正式签名、公证和第三方许可证核对。构建后的 App 已包含模型和运行时，正常转换不需要访问外部服务。

