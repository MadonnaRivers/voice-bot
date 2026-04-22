#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# This project uses Python 3.12 only (plain `python3` on many systems is still 3.9).
BOOTSTRAP="${PYTHON_BOOTSTRAP:-python3.12}"
if ! command -v "$BOOTSTRAP" >/dev/null 2>&1; then
  echo "error: '$BOOTSTRAP' not found. Install with:" >&2
  echo "  sudo apt update && sudo apt install -y python3.12 python3.12-venv" >&2
  exit 1
fi

if [[ "${1:-}" == "--recreate" ]]; then
  rm -rf venv
  shift || true
fi

venv_mm_version() {
  if [[ -x "$ROOT/venv/bin/python" ]]; then
    "$ROOT/venv/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true
  fi
}

if [[ -d venv ]]; then
  if [[ ! -x "$ROOT/venv/bin/python" ]]; then
    echo "Removing broken venv (missing venv/bin/python) …" >&2
    rm -rf venv
  else
    VV="$(venv_mm_version)"
    if [[ -n "$VV" && "$VV" != "3.12" ]]; then
      echo "Removing venv: it was Python $VV but this project requires 3.12 …" >&2
      rm -rf venv
    fi
  fi
fi

if [[ ! -d venv ]]; then
  "$BOOTSTRAP" -m venv venv
fi

PY="$ROOT/venv/bin/python"
got="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
if [[ "$got" != "3.12" ]]; then
  echo "error: venv reports Python $got, expected 3.12. Try: rm -rf venv && $BOOTSTRAP -m venv venv" >&2
  exit 1
fi

"$PY" -m pip install -U pip
"$PY" -m pip install -r "$ROOT/requirements.txt"

echo ""
echo "venv Python: $($PY --version)  ($PY)"
echo "Activate:    source venv/bin/activate"
echo "If \`python --version\` is still 3.9: run \`deactivate\` then \`hash -r\`, activate again, or use \`./venv/bin/python\`."
echo "Check path:  type -a python   # first line should be .../voice-bot/venv/bin/python"
