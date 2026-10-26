import os
import pty
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT / "install.sh"


def installer_run(root: Path, scenario="ok", *, reconfigure=False):
    """Exercise real deploy/rollback files in a temporary root; mock privileged/network calls."""
    helper = root / "wizard.py"
    helper.write_text("""import json,os,sys
from pathlib import Path
args=sys.argv[1:]
config=Path(args[args.index('--env-file')+1])
command=args[args.index('--env-file')+2]
if command=='setup':
 config.write_text('TELEGRAM_BOT_TOKEN=123:TEST_SECRET\\nTELEGRAM_CHAT_IDS=123\\nTELEGRAM_ADMIN_IDS=123\\nDB_PATH='+os.environ['STATE_DIR']+'/bot.sqlite3\\n')
 config.chmod(0o600)
elif command=='doctor':
 sys.exit(1 if os.environ['SCENARIO']=='doctor-fail' else 0)
elif command=='health':
 print(json.dumps({'healthy':os.environ['SCENARIO'] not in ['startup-fail','stop-fail-new'],
                   'pid':999 if os.environ['SCENARIO']=='stale-pid' else 123}))
 sys.exit(1 if os.environ['SCENARIO'] in ['startup-fail','stop-fail-new'] else 0)
""")
    harness = f"""
source {shlex.quote(str(INSTALLER))}
APP_ROOT={shlex.quote(str(root / "app"))}
CONFIG_DIR={shlex.quote(str(root / "etc"))}
STATE_DIR={shlex.quote(str(root / "state"))}
UNIT={shlex.quote(str(root / "systemd" / "rh-prem-bot.service"))}
CONTROL={shlex.quote(str(root / "bin" / "rh-prem-bot"))}
INSTALL_LOCK={shlex.quote(str(root / "install.lock"))}
export STATE_DIR SCENARIO={shlex.quote(scenario)}
export PYTHONPATH={shlex.quote(str(PROJECT / "src"))}
mkdir -p "$(dirname "$UNIT")" "$(dirname "$CONTROL")"
require_root() {{ :; }}
require_platform() {{ :; }}
id() {{ return 0; }}
chown() {{ :; }}
install() {{
  local args=()
  while (($#)); do
    case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
  done
  command install "${{args[@]}}"
}}
install_runtime() {{
  mkdir -p "$RELEASE/.venv/bin"
  printf '%s\\n' '#!/usr/bin/env bash' \\
    {shlex.quote("exec " + shlex.quote(sys.executable) + ' "$@"')} > "$RELEASE/.venv/bin/python"
  chmod +x "$RELEASE/.venv/bin/python"
}}
service_python() {{ {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "$@"; }}
runuser() {{ shift 3; "$@"; }}
sleep() {{ :; }}
systemctl() {{
  printf '%s\\n' "$*" >> {shlex.quote(str(root / "systemctl.log"))}
  case "$1" in
    show) printf '123\\n' ;;
    is-active) test -f {shlex.quote(str(root / "active"))} ;;
    is-enabled) test -f {shlex.quote(str(root / "enabled"))} ;;
    start|restart) touch {shlex.quote(str(root / "active"))} ;;
    stop)
      if [[ "$SCENARIO" == stop-fail-new && "$ACTIVATED" -eq 1 ]]; then return 1; fi
      rm -f {shlex.quote(str(root / "active"))} ;;
    enable) touch {shlex.quote(str(root / "enabled"))} ;;
    disable) rm -f {shlex.quote(str(root / "enabled"))} ;;
    *) return 0 ;;
  esac
}}
main {"--reconfigure" if reconfigure else ""}
"""
    master, slave = pty.openpty()
    try:
        os.write(master, b"\n")
        return subprocess.run(
            ["bash", "-c", harness],
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=20,
            cwd=PROJECT,
        )
    finally:
        os.close(slave)
        os.close(master)


def test_install_creates_paths_permissions_unit_and_wrapper(tmp_path):
    result = installer_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TEST_SECRET" not in result.stdout + result.stderr
    assert (tmp_path / "app/current").is_symlink()
    assert (tmp_path / "enabled").exists() and (tmp_path / "active").exists()
    config = tmp_path / "etc/bot.env"
    assert config.stat().st_mode & 0o777 == 0o640
    unit = (tmp_path / "systemd/rh-prem-bot.service").read_text()
    assert "User=rh-prem-bot" in unit and "User=root" not in unit
    assert str(tmp_path / "app/current/.venv/bin/python") in unit
    assert f"--env-file {config} run" in unit
    assert "Restart=on-failure" in unit
    assert "CPUQuota=50%" in unit and "MemoryMax=256M" in unit
    assert "TasksMax=64" in unit and "Nice=10" in unit
    assert "TEST_SECRET" not in unit
    control = tmp_path / "bin/rh-prem-bot"
    assert control.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(control)]).returncode == 0
    assert subprocess.run([str(control), "--help"], capture_output=True).returncode == 0
    assert "bootstrap.sh' --keep-config" in control.read_text()
    assert not list((tmp_path / "state").glob(".install.*"))


def test_upgrade_preserves_configuration_and_database(tmp_path):
    assert installer_run(tmp_path).returncode == 0
    config = tmp_path / "etc/bot.env"
    contents = config.read_bytes()
    old_release = (tmp_path / "app/current").resolve()
    from rh_prem_bot.store import Store

    db = Store(str(tmp_path / "state/bot.sqlite3"))
    db.set("thresholds", {"min_premium_usd": 12345})
    db.close()
    result = installer_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == contents
    assert (tmp_path / "app/current").resolve() != old_release
    assert old_release.is_dir()
    db = Store(str(tmp_path / "state/bot.sqlite3"))
    assert db.get("thresholds") == {"min_premium_usd": 12345}
    db.close()


def test_updates_keep_current_and_previous_release_only(tmp_path):
    releases = []
    for _ in range(3):
        result = installer_run(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        releases.append((tmp_path / "app/current").resolve())
    assert not releases[0].exists()
    assert releases[1].is_dir() and releases[2].is_dir()
    assert len(list((tmp_path / "app/releases").iterdir())) == 2


@pytest.mark.parametrize("scenario", ["doctor-fail", "startup-fail", "stale-pid"])
def test_failed_upgrade_restores_old_service_and_files(tmp_path, scenario):
    assert installer_run(tmp_path).returncode == 0
    original = (tmp_path / "app/current").resolve()
    old_files = {
        p: p.read_bytes()
        for p in [
            tmp_path / "etc/bot.env",
            tmp_path / "systemd/rh-prem-bot.service",
            tmp_path / "bin/rh-prem-bot",
        ]
    }
    from rh_prem_bot.store import Store

    db = Store(str(tmp_path / "state/bot.sqlite3"))
    db.set("thresholds", {"min_premium_usd": 54321})
    db.close()
    result = installer_run(tmp_path, scenario, reconfigure=True)
    assert result.returncode != 0
    assert (tmp_path / "app/current").resolve() == original
    assert (tmp_path / "active").exists() and (tmp_path / "enabled").exists()
    assert all(p.read_bytes() == value for p, value in old_files.items())
    assert len(list((tmp_path / "app/releases").iterdir())) == 1
    db = Store(str(tmp_path / "state/bot.sqlite3"))
    assert db.get("thresholds") == {"min_premium_usd": 54321}
    db.close()


def test_failed_first_install_leaves_no_enabled_service(tmp_path):
    result = installer_run(tmp_path, "startup-fail")
    assert result.returncode != 0
    assert not (tmp_path / "app/current").exists()
    assert not (tmp_path / "systemd/rh-prem-bot.service").exists()
    assert not (tmp_path / "bin/rh-prem-bot").exists()
    assert not (tmp_path / "etc/bot.env").exists()
    assert not (tmp_path / "enabled").exists() and not (tmp_path / "active").exists()


def test_failed_stop_during_rollback_retains_backups_and_does_not_overwrite_live_db(tmp_path):
    assert installer_run(tmp_path).returncode == 0
    from rh_prem_bot.store import Store

    original = (tmp_path / "app/current").resolve()
    db = Store(str(tmp_path / "state/bot.sqlite3"))
    db.set("thresholds", {"min_premium_usd": 54321})
    db.close()
    result = installer_run(tmp_path, "stop-fail-new", reconfigure=True)
    assert result.returncode != 0 and "Откат требует проверки" in result.stderr
    assert (tmp_path / "app/current").resolve() != original
    assert (tmp_path / "active").exists()
    assert list((tmp_path / "state").glob(".install.*/backup-db"))
    db = Store(str(tmp_path / "state/bot.sqlite3"))
    assert db.get("thresholds") == {}
    db.close()


def test_reconfigure_applies_new_env_instead_of_saved_telegram_thresholds(tmp_path):
    assert installer_run(tmp_path).returncode == 0
    from rh_prem_bot.store import Store

    db = Store(str(tmp_path / "state/bot.sqlite3"))
    db.set("thresholds", {"min_premium_usd": 12345})
    db.close()
    result = installer_run(tmp_path, reconfigure=True)
    assert result.returncode == 0, result.stdout + result.stderr
    db = Store(str(tmp_path / "state/bot.sqlite3"))
    assert db.get("thresholds") == {}
    db.close()


def test_installer_help_and_syntax_without_root():
    assert subprocess.run(["bash", "-n", str(INSTALLER)]).returncode == 0
    result = subprocess.run(["bash", str(INSTALLER), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "sudo bash install.sh" in result.stdout


def test_installation_does_not_modify_other_services_or_docker(tmp_path):
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    other = systemd / "other-bot.service"
    other.write_text("Other application settings")
    docker = tmp_path / "docker-daemon.json"
    docker.write_text('{"existing": true}')
    result = installer_run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert other.read_text() == "Other application settings"
    assert docker.read_text() == '{"existing": true}'
    for line in (tmp_path / "systemctl.log").read_text().splitlines():
        if line.split()[0] in {"start", "stop", "restart", "enable", "disable"}:
            assert line.split()[1] == "rh-prem-bot.service"
    code = INSTALLER.read_text()
    assert "apt-get" not in code and "pip install" not in code
