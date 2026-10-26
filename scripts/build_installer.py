"""Build one downloadable Bash installer containing the application source and lockfile."""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = r"""#!/usr/bin/env bash
# RWA Premium Monitor — self-extracting Ubuntu installer
set -Eeuo pipefail
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  printf '%s\n' 'Установка: sudo bash rh-prem-bot-installer.run' \
    'Повторная настройка: sudo bash rh-prem-bot-installer.run --reconfigure'
  exit 0
fi
if [[ "$EUID" -ne 0 ]]; then
  command -v sudo >/dev/null || { printf '%s\n' 'Запустите установщик от root.' >&2; exit 1; }
  exec sudo bash "$0" "$@"
fi
umask 022
package_dir="$(mktemp -d /tmp/rh-prem-package.XXXXXX)"
trap 'rm -rf -- "$package_dir"' EXIT
payload_line="$(awk '$0 == "__RH_PREM_PAYLOAD__" {print NR + 1; exit}' "$0")"
[[ -n "$payload_line" ]] || { printf '%s\n' 'Повреждён установщик.' >&2; exit 1; }
tail -n +"$payload_line" "$0" | base64 --decode | \
  tar --no-same-owner --no-same-permissions -xz -C "$package_dir"
bash "$package_dir/install.sh" "$@"
exit 0
__RH_PREM_PAYLOAD__
"""


def build(output: Path) -> Path:
    files = [
        ROOT / p
        for p in ("install.sh", "bootstrap.sh", "pyproject.toml", "uv.lock", ".env.example")
    ]
    files.extend(sorted((ROOT / "src").rglob("*.py")))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in files:
            info = archive.gettarinfo(str(path), str(path.relative_to(ROOT)))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with path.open("rb") as content:
                archive.addfile(info, content)
    encoded = base64.encodebytes(stream.getvalue())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(HEADER.encode() + encoded)
    output.chmod(0o755)
    import hashlib

    Path(str(output) + ".sha256").write_text(
        hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n"
    )
    return output


if __name__ == "__main__":
    print(build(ROOT / "dist/rh-prem-bot-installer.run"))
