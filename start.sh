#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen --quiet
else
  if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
  fi
  if ! .venv/bin/python -c 'import rh_prem_bot' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -e .
  fi
fi
exec .venv/bin/python -m rh_prem_bot "${@:-run}"
