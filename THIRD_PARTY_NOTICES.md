# Third-party components

BookWeave's application and converter source are distributed under the MIT license. Model weights and binary runtimes are not included in this source repository.

| Component | Source / license |
| --- | --- |
| Modified Markdown-to-EPUB packager | [overcuriousity/pdf2epub](https://github.com/overcuriousity/pdf2epub), based on commit `5eae669d1c897b4735f369fb2ff4086237db3a81`; MIT, original notice in `pdf2epub/LICENSE` |
| PaddleOCR-VL-1.6 GGUF weights | [Official PaddlePaddle repository](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF), Apache-2.0 |
| llama.cpp | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp), MIT; tested runtime `b11814` |
| Python | [python.org](https://www.python.org/), PSF license and bundled notices |
| Python dependencies | `requirements.txt`; preserve each distribution's license and notices in binary bundles |

Changes to the EPUB packager include image lookup, byte-preserving PNG copies, XHTML metadata compatibility, currency/math handling, and validation with a text fallback for malformed raw HTML.

The two PDF files in `fixtures/` are synthetic English test documents created for this project. User books, OCR output, caches and credentials are not part of the repository.

