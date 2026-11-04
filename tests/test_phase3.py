"""Gate version check, host bootstrap, and rollback-aware plan output."""
import runpy
from pathlib import Path

import pytest

from vps_deployer import bootstrap, cli
from vps_deployer.cli import main
from vps_deployer.config import Repository
from vps_deployer.deployment import Reconciler
from vps_deployer.models import Deployment, Host
from vps_deployer.remote import GATE_PROTOCOL, RemoteHost, Result

ROOT = Path(__file__).resolve().parents[1]


def host(privileged=True):
    ssh = {"host": "h", "privileged_host": "h"} if privileged else {"host": "h"}
    return Host.parse({"name": "prod", "ssh": ssh}, "test")


def test_client_and_gate_protocol_versions_match():
    gate = runpy.run_path(str(ROOT / "vps_deployer" / "data" / "vps-deployer-ssh-gate"))
    assert gate["GATE_PROTOCOL"] == GATE_PROTOCOL


@pytest.mark.parametrize("result,state", [
    (Result("vps-deployer-gate 1\n", "", 0), "ok"),
    (Result("vps-deployer-gate 99\n", "", 0), "mismatch"),
    (Result("", "operation is not allowed", 1), "outdated"),
])
def test_gate_status(monkeypatch, result, state):
    remote = RemoteHost(host())
    monkeypatch.setattr(remote, "run", lambda *a, **k: result)
    assert remote.gate_status()[0] == state


def test_gate_status_without_gate():
    assert RemoteHost(host(False)).gate_status()[0] == "none"


def test_gate_version_is_a_valid_privileged_request():
    assert RemoteHost._privileged_request(["gate-version"]) == {"operation": "gate-version", "arguments": []}


def test_gate_source_is_packaged():
    assert b"GATE_PROTOCOL" in bootstrap.gate_source()


def test_bootstrap_prints_storage_flags_from_manifests(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    repo = Repository.load(ROOT / "examples")
    name = next(iter(repo.hosts))
    storage = bootstrap.storage_paths(repo, name)
    assert "/var/lib/invoicer" in storage
    line = bootstrap.authorized_keys_line(repo.hosts[name], storage)
    assert "--managed-root /srv/vps-deployer --storage /var/lib/invoicer" in line
    assert line.startswith('command="/usr/local/sbin/vps-deployer-ssh-gate')
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "host", "bootstrap", name])
    assert exc.value.code == 0
    assert "authorized_keys" in capsys.readouterr().out


def test_activate_action_flags_reversed_manual_rollback(tmp_path, monkeypatch):
    artifact = tmp_path / "a"; artifact.mkdir()
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"}},
                           tmp_path / "d.yaml")
    repo = Repository(tmp_path); repo.hosts["prod"] = host(False)
    rec = Reconciler(repo, dep, object(), "new")
    monkeypatch.setattr(rec, "previous_release", lambda: "new")
    assert "manual rollback" in str(rec._activate_action("old"))
    monkeypatch.setattr(rec, "previous_release", lambda: "old")
    assert str(rec._activate_action("old")) == "ACTIVATE release new"


def test_bootstrap_install_failure_prints_manual_steps(monkeypatch, capsys):
    from vps_deployer.remote import RemoteError
    def boom(self, *a, **k): raise RemoteError("sudo: a password is required")
    monkeypatch.setattr(RemoteHost, "run", boom)
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "host", "bootstrap", next(iter(Repository.load(ROOT / "examples").hosts)), "--install"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "scp" in err and "sudo install" in err and "password" in err
