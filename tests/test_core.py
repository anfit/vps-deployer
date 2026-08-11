from pathlib import Path
import os

import pytest

from vps_deployer.config import Repository
from vps_deployer.deployment import (content_hash, env_file, env_matches, git_metadata,
                                     releases_to_prune, select_rollback)
from vps_deployer.models import ConfigError, Deployment, Host
from vps_deployer.systemd import render_unit
from vps_deployer.nginx import render_proxy


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


@pytest.mark.parametrize(("field", "value"), [
    ("working_directory", ".\nUser=root"),
    ("working_directory", "app%N"),
    ("command", "./run.sh\nUser=root"),
])
def test_systemd_fields_reject_directives_and_specifiers(tmp_path, field, value):
    runtime = {"command": "./run.sh", field: value}
    with pytest.raises(ConfigError):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": runtime}, tmp_path / "d.yaml")


@pytest.mark.parametrize("path", ["/var/lib/demo\nReadWritePaths=/", "/var/lib/demo%N", "/var/lib/demo data"])
def test_systemd_and_nginx_paths_reject_unsafe_syntax(tmp_path, path):
    with pytest.raises(ConfigError):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": {"command": "./run.sh"},
                          "storage": {"data": {"path": path}}}, tmp_path / "d.yaml")


@pytest.mark.parametrize("key", ["BAD-NAME", "NAME\nINJECT", "1NAME"])
def test_environment_keys_reject_invalid_names(tmp_path, key):
    with pytest.raises(ConfigError, match="environment variable name"):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": {"command": "./run.sh"},
                          "environment": {key: "value"}}, tmp_path / "d.yaml")


def test_missing_environment_file_never_matches():
    assert env_matches(None, {"TOKEN": "redacted"}, {"TOKEN"}, False) is False


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


def test_http_proxy_is_rendered_per_deployment(tmp_path):
    artifact = tmp_path / "artifact"; artifact.mkdir()
    dep = Deployment.parse({
        "name": "demo-dev", "host": "prod", "service": {"user": "svc-demo"},
        "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"},
        "http_proxy": {"name": "demo-dev", "domain": "dev.example.test",
                       "upstream": "http://127.0.0.1:5101",
                       "certificate": "/etc/ssl/dev/fullchain.pem",
                       "certificate_key": "/etc/ssl/dev/privkey.pem"},
    }, tmp_path / "dev.yaml")
    rendered = render_proxy(dep)
    assert "server_name dev.example.test;" in rendered
    assert "proxy_pass http://127.0.0.1:5101;" in rendered
    assert "ssl_certificate /etc/ssl/dev/fullchain.pem;" in rendered


def test_http_only_proxy_omits_tls_server(tmp_path):
    artifact = tmp_path / "artifact"; artifact.mkdir()
    dep = Deployment.parse({
        "name": "demo-dev", "host": "prod", "service": {"user": "svc-demo"},
        "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"},
        "http_proxy": {"name": "demo-dev", "domain": "dev.example.test",
                       "upstream": "http://127.0.0.1:8100", "tls": False},
    }, tmp_path / "dev.yaml")
    rendered = render_proxy(dep)
    assert "listen 80;" in rendered
    assert "listen 443 ssl;" not in rendered
    assert "ssl_certificate" not in rendered


def test_http_only_proxy_rejects_certificate_fields(tmp_path):
    artifact = tmp_path / "artifact"; artifact.mkdir()
    with pytest.raises(ConfigError, match="must not declare certificates"):
        Deployment.parse({
            "name": "demo-dev", "host": "prod", "service": {"user": "svc-demo"},
            "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"},
            "http_proxy": {"domain": "dev.example.test", "upstream": "http://127.0.0.1:8100",
                           "tls": False, "certificate": "/etc/ssl/cert.pem"},
        }, tmp_path / "dev.yaml")


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


def test_content_hash_honors_repository_ignore_file(tmp_path):
    source = tmp_path / "a"; source.mkdir(); (source / "app.py").write_text("app")
    (source / ".vps-deployer-ignore").write_text("local.properties\nhelpers/*\n")
    first = content_hash(source)
    (source / "local.properties").write_text("secret")
    helpers = source / "helpers"; helpers.mkdir(); (helpers / "debug.py").write_text("local")
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


def test_local_paths_expand_roots_and_reject_traversal(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"; artifact.mkdir()
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": "${PROJECTS_DIR}/artifact"},
                            "runtime": {"command": "./run.sh"}}, tmp_path / "d.yaml")
    assert dep.source == artifact.resolve()
    with pytest.raises(ConfigError, match="traversal"):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "${PROJECTS_DIR}/../secret"},
                          "runtime": {"command": "./run.sh"}}, tmp_path / "d.yaml")
    monkeypatch.delenv("PROJECTS_DIR")
    with pytest.raises(ConfigError, match="is not set"):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "${PROJECTS_DIR}/artifact"},
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


def test_redacted_plan_compares_secret_keys_without_secret_values():
    current = "PORT=5101\nTOKEN=actual-secret\n"
    expected = {"PORT": "5101", "TOKEN": "<secret>"}
    assert env_matches(current, expected, {"TOKEN"}, False)
    assert not env_matches(current, {**expected, "PORT": "5102"}, {"TOKEN"}, False)
    assert not env_matches(current, {"PORT": "5101"}, set(), False)


def test_git_build_metadata_records_exact_revision(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    (tmp_path / "app.py").write_text("app")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "test"], check=True)
    commit, rendered = git_metadata(tmp_path, "release-123")
    assert f"commit.hash={commit}" in rendered
    assert "release.id=release-123" in rendered
    assert "build.user=vps-deployer" in rendered


def test_rollback_selection():
    assert select_rollback(["003", "001", "002"], "003") == "002"
    assert select_rollback(["003", "001", "002"], "003", "001") == "001"
    with pytest.raises(ConfigError): select_rollback(["001"], "001")


def test_release_pruning_keeps_active_and_immediate_predecessor():
    assert releases_to_prune(["new", "previous", "old", "oldest"], "new", "previous") == ["old", "oldest"]


def test_release_pruning_ignores_unsafe_directory_names():
    assert releases_to_prune(["new", "previous", "../state", "bad/name"], "new", "previous") == []
