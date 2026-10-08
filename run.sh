#!/usr/bin/env bash
# Khởi động Building Data Studio: tạo .venv nếu chưa có, cài dependency, mở trình duyệt.
#   ./run.sh                 # mặc định cổng 8765
#   ./run.sh --port 9000     # đổi cổng
#   ./run.sh --no-browser    # không tự mở trình duyệt
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [[ ! -x .venv/bin/python ]]; then
  echo "▶ Tạo môi trường Python (.venv)…"
  "$PYTHON" -m venv .venv
fi
if [[ ! -f .venv/.installed || requirements.txt -nt .venv/.installed ]]; then
  echo "▶ Cài dependency…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  touch .venv/.installed
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
missing=()
for cmd in ogr2ogr ogrinfo tippecanoe; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if (( ${#missing[@]} )); then
  echo "⚠ Thiếu: ${missing[*]} — cài bằng: brew install gdal tippecanoe pmtiles"
fi

exec .venv/bin/python -m app.server "$@"
