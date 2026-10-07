#!/usr/bin/env bash
# SlideCapture build script (Linux / WSL)
#   ./build.sh           -> dist/SlideCapture/SlideCapture
#   ./build.sh onefile   -> dist/SlideCapture
# ※ Windows 用の exe は Windows 上（build.bat）でしか作れません
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "[1/3] Creating virtual environment .venv ..."
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "[2/3] Installing packages ..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt pyinstaller

export ONEFILE=0
[ "${1:-}" = "onefile" ] && export ONEFILE=1

echo "[3/3] Building (ONEFILE=$ONEFILE) ..."
pyinstaller --noconfirm --clean slide_capture.spec
echo "Done: see dist/"
