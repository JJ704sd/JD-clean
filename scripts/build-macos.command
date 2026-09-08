#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "请在 Mac 上构建。"
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "构建机需预先安装 uv 和带 Tcl/Tk 的原生 Python 3.12。普通使用者不需要。"
  exit 1
fi
export MACOSX_DEPLOYMENT_TARGET=14.0
uv sync --locked --group build
uv run --locked --group build python scripts/build_desktop.py
echo "构建完成。release 中为当前 Mac 架构的预览包；正式分发仍需签名、公证。"
