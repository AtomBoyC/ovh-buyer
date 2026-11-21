#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Find Python 3.9+
PYTHON_BIN=""
for cand in python3 /Library/Developer/CommandLineTools/usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "❌ 未检测到 Python 3.9+，请先安装 Python 3。"
  exit 1
fi

mkdir -p data

echo "🚀 使用 $("$PYTHON_BIN" --version) 启动 OVH Sniper..."
exec "$PYTHON_BIN" "$PROJECT_DIR/run.py" "$@"
