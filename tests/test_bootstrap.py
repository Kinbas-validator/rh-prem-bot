import fcntl
import hashlib
import os
import pty
import shlex
import subprocess
import termios
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


def run_bootstrap(
    tmp_path,
    *,
    args="",
    bad_checksum=False,
    bad_filename=False,
    repository="Kinbas-validator/rh-prem-bot",
):
    payload = tmp_path / "payload.run"
    marker = tmp_path / "executed"
    payload.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + shlex.quote(str(marker)) + "\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    if bad_checksum:
        digest = "0" * 64
    filename = "../../elsewhere" if bad_filename else "rh-prem-bot-installer.run"
    harness = f"""
source {shlex.quote(str(PROJECT / "bootstrap.sh"))}
REPOSITORY={shlex.quote(repository)}
bootstrap_require_root() {{ :; }}
download_release() {{
  printf '%s\\n' "$1" >> {shlex.quote(str(tmp_path / "urls"))}
  if [[ "$1" == *.sha256 ]]; then
    printf '%s  %s\\n' {shlex.quote(digest)} {shlex.quote(filename)} > "$2"
  else
    cp {shlex.quote(str(payload))} "$2"
  fi
}}
bootstrap_main {args}
"""
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", "-c", harness], stdin=slave, capture_output=True, text=True, timeout=5
        )
    finally:
        os.close(master)
        os.close(slave)
    return result, marker


def test_downloads_release_verifies_checksum_and_forwards_update_flags(tmp_path):
    result, marker = run_bootstrap(tmp_path, args="--keep-config")
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text().strip() == "--keep-config"
    assert (tmp_path / "urls").read_text().splitlines() == [
        "https://github.com/Kinbas-validator/rh-prem-bot/releases/latest/download/rh-prem-bot-installer.run",
        "https://github.com/Kinbas-validator/rh-prem-bot/releases/latest/download/rh-prem-bot-installer.run.sha256",
    ]


def test_selects_immutable_version_release(tmp_path):
    result, marker = run_bootstrap(tmp_path, args="--version v1.2.0 --reconfigure")
    assert result.returncode == 0 and marker.read_text().strip() == "--reconfigure"
    assert "/releases/download/v1.2.0/" in (tmp_path / "urls").read_text()


@pytest.mark.parametrize("bad_checksum,bad_filename", [(True, False), (False, True)])
def test_corrupted_or_invalid_checksums_are_never_executed(tmp_path, bad_checksum, bad_filename):
    result, marker = run_bootstrap(tmp_path, bad_checksum=bad_checksum, bad_filename=bad_filename)
    assert result.returncode != 0 and not marker.exists()


@pytest.mark.parametrize(
    "args,repository",
    [
        ("--version ../../other", "Kinbas-validator/rh-prem-bot"),
        ("--version", "Kinbas-validator/rh-prem-bot"),
        ("", "https://malicious.invalid/other"),
        ("--unknown", "Kinbas-validator/rh-prem-bot"),
    ],
)
def test_invalid_sources_and_options_fail_before_download(tmp_path, args, repository):
    result, marker = run_bootstrap(tmp_path, args=args, repository=repository)
    assert result.returncode != 0 and not marker.exists()
    assert not (tmp_path / "urls").exists()


def test_help_needs_neither_root_nor_terminal():
    result = subprocess.run(
        ["bash", str(PROJECT / "bootstrap.sh"), "--help"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0 and "rh-prem-bot update" in result.stdout


def test_piped_bootstrap_help_does_not_depend_on_bash_source_array():
    code = (PROJECT / "bootstrap.sh").read_text()
    result = subprocess.run(
        ["bash", "-s", "--", "--help"], input=code, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0 and "rh-prem-bot update" in result.stdout


def test_pipe_installation_passes_the_terminal_to_the_wizard(tmp_path):
    marker = tmp_path / "executed"
    payload = tmp_path / "fixture.run"
    payload.write_text(
        '#!/bin/bash\n[[ -t 0 ]] || exit 9\nprintf "%s" "$1" > ' + shlex.quote(str(marker)) + "\n"
    )
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    code = (PROJECT / "bootstrap.sh").read_text()
    dispatch = 'if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then'
    fixture = f"""
bootstrap_require_root() {{ :; }}
download_release() {{
 if [[ "$1" == *.sha256 ]]; then
  printf '%s  rh-prem-bot-installer.run\\n' '{digest}' > "$2"
 else
  cp {shlex.quote(str(payload))} "$2"
 fi
}}
"""
    code = code.replace(dispatch, fixture + dispatch)
    master, slave = pty.openpty()

    def attach_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    try:
        result = subprocess.run(
            ["bash", "-s", "--", "--keep-config"],
            input=code,
            capture_output=True,
            text=True,
            timeout=5,
            preexec_fn=attach_terminal,
            pass_fds=(slave,),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.read_text() == "--keep-config"
    finally:
        os.close(master)
        os.close(slave)
