#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen --extra dev --quiet
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -e '.[dev]'
fi
.venv/bin/ruff check .
.venv/bin/ruff format --check .
bash -n install.sh bootstrap.sh start.sh scripts/test.sh scripts/publish_release.sh
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck install.sh bootstrap.sh start.sh scripts/test.sh scripts/publish_release.sh
fi
exec .venv/bin/python -m pytest -q "$@"
