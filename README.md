<img src="assets/BookWeave.png" width="96" alt="BookWeave icon" />

# BookWeave

本地运行的 macOS PDF → EPUB 应用。支持中文、英文、文字版 PDF 和扫描版 PDF，保留页面图片，生成可编辑的文字和 Markdown。

A native macOS app that turns Chinese and English PDFs into editable EPUBs using local PaddleOCR-VL-1.6 inference. Text PDFs skip OCR; scanned pages retain their reference images.

## 功能

- 原生 SwiftUI 界面，文件在本机处理。
- 构建时精简无用运行资源，保留主识别模型、必需视觉模型及服务端依赖。
- PaddleOCR-VL-1.6 GGUF，通过 llama.cpp 使用 Apple GPU。
- 每段最多 300 页并平均分配：1000 页分 4 段，800 页分 3 段；最终汇总为一个 EPUB。
- 逐页缓存、断点续转，界面显示各段页码范围、进度及异常数量。
- 文字版页面直接提取；扫描页识别与下一页准备重叠执行。
- 保留正文图片和扫描页参考图，过滤多页重复的小型页边装饰。
- 扫描页清理已确认的重复边饰和空白，使用最高 300 DPI 高清输入；保留正文、图注、页码及不确定图形。
- 检测明显循环输出；异常页保留原图并列入校对清单。
- 单页超时自动跳过并标注原 PDF 页码，全部分段完成后自动校验并生成转换报告。
- 验证 EPUB 正文结构，避免识别文字中的异常标签破坏电子书。

## 使用与构建

运行环境为 **Apple Silicon Mac，macOS 14 或更新版本**。本仓库提供源码，模型权重和 Python/llama.cpp 运行时单独准备，详见 [构建说明](docs/BUILDING.md)。

选择 PDF，填写书名和电子书语言，点击“开始／继续转换”。同一份 PDF 的已完成页面会自动复用，各段自动依次处理；异常页自动标记并跳过。每次输出保存到独立目录，避免覆盖已编辑的文件。

使用方法、缓存位置和图片处理规则见 [使用说明](docs/使用说明.md)。可选安装 [Sigil](https://sigil-ebook.com/) 编辑 EPUB。

## 测试

使用 Python 3.12：

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -B -m unittest discover -s mac-app/tests
(cd pdf2epub && python -B -m unittest discover -s tests)
```

未安装 App 时，应用包测试会跳过。可以设置 `BOOKWEAVE_APP` 指向构建后的 `.app`。单元测试使用合成文件，不需要用户文档或下载 OCR 模型；应用包测试会检查打包的运行时和模型。

## 转换质量

OCR 仍可能识别错误，复杂多栏、表格、密集小字需要校对。扫描页参考图放在对应文字之后，不能保证原 PDF 的图文穿插位置。待校对清单仅提示已检测的异常，不代表其他页面已人工验证。

缓存加速的是继续转换和重新打包；第一次识别整本扫描书仍需要时间。当前使用整页 OCR，并未接入完整的 PaddleOCR 版面分析流水线。

## 开源组件

程序源码使用 [MIT License](LICENSE)。EPUB 打包器改编自 [overcuriousity/pdf2epub](https://github.com/overcuriousity/pdf2epub)，保留其 [原始许可证](pdf2epub/LICENSE)。模型、推理引擎和 Python 依赖分别遵循各自许可证，参见 [第三方组件说明](THIRD_PARTY_NOTICES.md)。

