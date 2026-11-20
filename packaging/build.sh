#!/usr/bin/env bash
# Build a single-file Picture House program for this computer with PyInstaller.
#   pip install pyinstaller && packaging/build.sh
# The result is in dist/. GitHub Actions runs this on Windows, Mac and Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
SEP=":"; [[ "${OS:-}" == "Windows_NT" ]] && SEP=";"
ICON=()
[[ -f packaging/icon.ico ]] && ICON=(--icon packaging/icon.ico)
python -m PyInstaller --noconfirm --clean --onefile --console \
  --name picture-house \
  "${ICON[@]}" \
  --add-data "app${SEP}app" \
  --add-data "picture-house.py${SEP}." \
  --hidden-import csv --hidden-import difflib --hidden-import datetime \
  --hidden-import shutil --hidden-import subprocess \
  picture-house-server.py
