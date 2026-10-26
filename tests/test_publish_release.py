import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("state", ["missing", "draft", "published"])
def test_publishes_both_assets_before_exposing_latest_and_supports_draft_retry(tmp_path, state):
    (tmp_path / "dist").mkdir()
    for name in ["rh-prem-bot-installer.run", "rh-prem-bot-installer.run.sha256"]:
        (tmp_path / "dist" / name).write_text("fixture")
    executable = tmp_path / "gh"
    executable.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$GH_CALLS"
if [[ "$2" == view ]]; then
 case "$FIXTURE_STATE" in missing) exit 1 ;; draft) echo true ;; published) echo false ;; esac
fi
""")
    executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "FIXTURE_STATE": state,
        "GH_CALLS": str(tmp_path / "calls"),
        "REPOSITORY": "Kinbas-validator/rh-prem-bot",
        "RELEASE_TAG": "v1.2.0",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/publish_release.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    if state == "published":
        assert result.returncode != 0 and len(calls) == 1
    else:
        assert result.returncode == 0
        assert len(calls) == 3 and "release edit" in calls[-1]
        assert "--draft=false --latest" in calls[-1]
        assert all(
            name in calls[1]
            for name in ["dist/rh-prem-bot-installer.run", "dist/rh-prem-bot-installer.run.sha256"]
        )
        assert ("release create" if state == "missing" else "release upload") in calls[1]
