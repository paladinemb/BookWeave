#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
resources="${1:?Usage: bash scripts/build_app.sh RESOURCES [OUTPUT.app]}"
output="${2:-$root/dist/BookWeave.app}"
[[ "$(uname -s)" == Darwin ]] || { echo 'Requires macOS.' >&2; exit 1; }
[[ "$output" == *.app && ! -e "$output" ]] || { echo 'Choose a new .app output path.' >&2; exit 1; }
for file in python-runtime/bin/python3.12 paddle-engine/runtime/llama-b11814/llama-server paddle-engine/models/PaddleOCR-VL-1.6-GGUF.gguf paddle-engine/models/PaddleOCR-VL-1.6-GGUF-mmproj.gguf; do
    [[ -f "$resources/$file" ]] || { echo "Missing resource: $file" >&2; exit 1; }
done
mkdir -p "$output/Contents/MacOS" "$output/Contents/Resources/pdf2epub/modules"
ditto "$resources/python-runtime" "$output/Contents/Resources/python-runtime"
ditto "$resources/paddle-engine" "$output/Contents/Resources/paddle-engine"
cp "$root/mac-app/Info.plist" "$output/Contents/Info.plist"
cp "$root/assets/AppIcon.icns" "$output/Contents/Resources/AppIcon.icns"
cp "$root/mac-app/paddle_converter.py" "$output/Contents/Resources/paddle_converter.py"
cp "$root/pdf2epub/modules/"*.py "$output/Contents/Resources/pdf2epub/modules/"
cp "$root/pdf2epub/LICENSE" "$output/Contents/Resources/pdf2epub/LICENSE"
cp "$root/THIRD_PARTY_NOTICES.md" "$root/LICENSE" "$output/Contents/Resources/"
"$output/Contents/Resources/python-runtime/bin/python3.12" -B -I "$root/scripts/prune_resources.py" "$output/Contents/Resources"
cache="$(mktemp -d)"
trap 'rm -rf "$cache"' EXIT
xcrun swiftc -parse-as-library -O -target "$(uname -m)-apple-macos14.0" -module-cache-path "$cache" "$root/mac-app/PDFtoEPUB.swift" -o "$output/Contents/MacOS/BookWeave"
codesign --force --deep --sign - "$output"
codesign --verify --deep --strict "$output"
"$output/Contents/MacOS/BookWeave" --self-test
echo "Built: $output"
