#!/usr/bin/env bash
# Připraví .venv + Playwright a spustí monitor.py.
# .venv se NEcommituje (je platformově specifické) — skript ho vždy vytvoří/doplní.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.venv"
PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$REPO_ROOT/requirements.txt"

if [[ ! -f "$REQUIREMENTS" ]]; then
  echo "Chybí requirements.txt v $REPO_ROOT" >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Vytvářím venv v $VENV_DIR …"
  python3 -m venv "$VENV_DIR"
fi

echo "Instaluji Python závislosti (včetně Playwright)…"
"$PYTHON" -m pip install -q --upgrade pip
"$PYTHON" -m pip install -q -r "$REQUIREMENTS"

echo "Instaluji Chromium pro Playwright…"
"$PYTHON" -m playwright install --with-deps chromium 2>/dev/null \
  || "$PYTHON" -m playwright install chromium

echo "Spouštím monitor…"
exec "$PYTHON" "$SCRIPT_DIR/monitor.py" "$@"
