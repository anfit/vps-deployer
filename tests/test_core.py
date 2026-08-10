from pathlib import Path
import os

import pytest

from vps_deployer.config import Repository
from vps_deployer.deployment import content_hash, env_file, select_rollback
from vps_deployer.models import ConfigError, Deployment, Host
from vps_deployer.systemd import render_unit


def deployment(tmp_path: Path) -> Deployment:
    artifact = tmp_path / "artifact"
    artifact.mkdir(); (artifact / "app").write_text("hello")
    return Deployment.parse({
        "name": "demo-prod", "host": "prod", "service": {"user": "svc-demo"},
        "release": {"source": str(artifact)},
        "runtime": {"command": "./bin/demo", "working_directory": "app"},
        "environment": {"TZ": {"from_global": "TZ"}},
        "secrets": {"TOKEN": {"from_env": "DEMO_TOKEN"}},
        "storage": {"data": {"path": "/var/lib/demo"}},
    }, tmp_path / "deployments" / "demo-prod.yaml")


def test_validation_rejects_unsafe_command(tmp_path):
    with pytest.raises(ConfigError):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": {"command": "/bin/sh"}}, tmp_path / "d.yaml")


def test_root_requires_explicit_privileged_marker(tmp_path):
    base = {"name": "root-demo", "host": "prod", "release": {"source": "x"},
            "runtime": {"command": "./run.sh"}}
    with pytest.raises(ConfigError):
        Deployment.parse({**base, "service": {"user": "root"}}, tmp_path / "d.yaml")
    dep = Deployment.parse({**base, "service": {"user": "root", "privileged": True}}, tmp_path / "d.yaml")
    assert dep.privileged is True


def test_systemd_hardening_and_storage(tmp_path):
    unit = render_unit(deployment(tmp_path), "/srv/vps-deployer")
    assert "User=svc-demo" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/var/lib/demo" in unit
    assert "WorkingDirectory=/srv/vps-deployer/demo-prod/current/app" in unit
    assert "ExecStart=/srv/vps-deployer/demo-prod/current/app/bin/demo" in unit


def test_content_hash_is_stable_and_content_sensitive(tmp_path):
    source = tmp_path / "a"; source.mkdir(); file = source / "x"; file.write_text("one")
    first = content_hash(source)
    assert content_hash(source) == first
    file.write_text("two")
    assert content_hash(source) != first


def test_content_hash_ignores_local_virtualenv(tmp_path):
    source = tmp_path / "a"; source.mkdir(); (source / "app.py").write_text("app")
    first = content_hash(source)
    (source / ".venv").mkdir(); (source / ".venv" / "local-only").write_text("ignored")
    (source / "config.yaml").write_text("password: local-secret")
    (source / ".test-venv").mkdir(); (source / ".test-venv" / "python").write_text("ignored")
    assert content_hash(source) == first


def test_release_include_is_validated_and_affects_hash(tmp_path):
    artifact = tmp_path / "artifact"; artifact.mkdir(); (artifact / "app.py").write_text("app")
    config = tmp_path / "prod.yaml"; config.write_text("mode: prod")
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(artifact), "include": [
                                {"source": str(config), "target": "config/production.yaml"}]},
                            "runtime": {"command": "./run.sh"}}, tmp_path / "d.yaml")
    first = content_hash(dep.source, dep.includes)
    config.write_text("mode: changed")
    assert content_hash(dep.source, dep.includes) != first
    with pytest.raises(ConfigError):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": str(artifact), "include": [
                              {"source": str(config), "target": "../secret"}]},
                          "runtime": {"command": "./run.sh"}}, tmp_path / "d.yaml")


def test_secret_resolution_and_redacted_error(tmp_path, monkeypatch):
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    repo.deployments[dep.name] = dep; repo.globals["prod"] = {"TZ": "Europe/Warsaw"}
    monkeypatch.setenv("DEMO_TOKEN", "super-secret")
    values, secret_keys = repo.resolve_environment(dep, True)
    assert values["TOKEN"] == "super-secret" and secret_keys == {"TOKEN"}
    assert "super-secret" not in env_file({"SAFE": "value"})
    monkeypatch.delenv("DEMO_TOKEN")
    with pytest.raises(ConfigError) as exc: repo.resolve_environment(dep, True)
    assert "super-secret" not in str(exc.value)


def test_rollback_selection():
    assert select_rollback(["003", "001", "002"], "003") == "002"
    assert select_rollback(["003", "001", "002"], "003", "001") == "001"
    with pytest.raises(ConfigError): select_rollback(["001"], "001")
