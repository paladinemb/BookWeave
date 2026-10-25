BookWeave supports offline Chinese/English PDF → EPUB conversion. Runtime and both OCR weights are bundled; no separate Python installation or model download is needed after installation.

| Download | Platform |
| --- | --- |
| macos-arm64.dmg | Apple Silicon Mac, macOS 14+ |
| macos-x64.dmg | Intel Mac, macOS 14+ |
| windows-x64-setup.exe | Windows 10/11 64-bit, AVX2-capable CPU |
| linux-x64.deb | Ubuntu 24.04+ / compatible Debian, x64 AVX2-capable CPU |

macOS retains the native SwiftUI interface. Windows/Linux use a desktop interface with the same conversion core, segment progress and automatic review reports. CPU OCR on Windows/Linux and Intel Mac can be considerably slower than Apple Silicon GPU OCR. GPU recognition uses a 90-second page limit, CPU recognition 300 seconds. Apple Silicon automatically uses CPU if no usable Metal device is available.

Large PDFs are split into balanced segments; up to two different segments run together when resource monitoring permits. Small PDFs stay serial. Bad pages are marked against the original PDF page number and do not stop the remaining pages.

These packages do not have Developer ID notarization or Windows Authenticode signing. OCR text may need proofreading; structural validation does not certify recognition accuracy.

`*.sha256` and `SHA256SUMS` are integrity checks, not extra installers. Source code archives are not installers. All four platform builds must pass their included runtime and PDF conversion checks before this release is published.
