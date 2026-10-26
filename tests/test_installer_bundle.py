import base64
import io
import subprocess
import tarfile

from scripts.build_installer import ROOT, build


def test_self_extracting_installer_contains_current_source_and_no_credentials(tmp_path):
    installer = build(tmp_path / "rh-prem-bot-installer.run")
    header, encoded = installer.read_bytes().split(b"\n__RH_PREM_PAYLOAD__\n", 1)
    assert header.startswith(b"#!/usr/bin/env bash")
    assert "--no-same-owner" in header.decode()
    with tarfile.open(fileobj=io.BytesIO(base64.decodebytes(encoded)), mode="r:gz") as archive:
        names = archive.getnames()
        assert "install.sh" in names and "uv.lock" in names
        assert "src/rh_prem_bot/__main__.py" in names
        assert ".env" not in names and ".env.example" in names
        assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
        assert all("__pycache__" not in n and "sqlite" not in n for n in names)
        assert archive.extractfile("install.sh").read() == (ROOT / "install.sh").read_bytes()
    assert installer.stat().st_mode & 0o111
    result = subprocess.run(["bash", str(installer), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "sudo bash rh-prem-bot-installer.run" in result.stdout
    assert subprocess.run(["bash", "-n", str(installer)]).returncode == 0
