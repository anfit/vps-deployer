from pathlib import Path
import os

import pytest

from vps_deployer.config import Repository
from vps_deployer.deployment import (content_hash, env_file, env_matches, git_metadata,
                                     parse_build_properties, render_build_properties,
                                     releases_to_prune, select_rollback, validate_archive)
from vps_deployer.models import ConfigError, Deployment, Host
from vps_deployer.systemd import render_timer, render_unit, timer_name
from vps_deployer.nginx import render_proxy


class RecordingRemote:
    def __init__(self, files=None):
        self.calls = []
        self.files = files or {}

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))

    def write_file(self, path, content, mode, owner, group):
        self.calls.append((["write_file", path, content, mode, owner, group], {}))

    def read(self, path, sudo=False):
        return self.files.get(path)

    def exists(self, path, sudo=False):
        return path in self.files

    def deployment_lock(self, deployment):
        from contextlib import nullcontext
        self.calls.append((["lock", deployment], {}))
        return nullcontext()


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


def test_timer_renders_oneshot_service_and_timer_unit(tmp_path):
    artifact = tmp_path / "artifact"; artifact.mkdir()
    dep = Deployment.parse({
        "name": "cleanup-prod", "host": "prod", "service": {"user": "svc-cleanup"},
        "release": {"source": str(artifact)}, "runtime": {"command": "./cleanup.sh"},
        "timer": {"on_calendar": "*-*-* 03:00:00", "persistent": True,
                  "randomized_delay_sec": 300},
    }, tmp_path / "timer.yaml")
    service = render_unit(dep, "/srv/vps-deployer")
    timer = render_timer(dep)
    assert "Type=oneshot" in service and "Restart=" not in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer and "RandomizedDelaySec=300" in timer
    assert "Unit=vps-deployer-cleanup-prod.service" in timer
    assert timer_name(dep) == "vps-deployer-cleanup-prod.timer"


@pytest.mark.parametrize("timer", [
    {"on_calendar": "daily\nOnCalendar=minutely"},
    {"on_calendar": "daily", "randomized_delay_sec": -1},
    {"on_calendar": "daily", "randomized_delay_sec": 86401},
])
def test_timer_rejects_unsafe_settings(tmp_path, timer):
    with pytest.raises(ConfigError):
        Deployment.parse({"name": "cleanup", "host": "prod", "service": {"user": "svc-cleanup"},
                          "release": {"source": "x"}, "runtime": {"command": "./cleanup.sh"},
                          "timer": timer}, tmp_path / "timer.yaml")


def test_timer_rejects_persistent_service_features(tmp_path):
    with pytest.raises(ConfigError, match="cannot declare"):
        Deployment.parse({"name": "cleanup", "host": "prod", "service": {"user": "svc-cleanup"},
                          "release": {"source": "x"}, "runtime": {"command": "./cleanup.sh"},
                          "timer": {"on_calendar": "daily"},
                          "healthcheck": {"type": "http", "url": "http://127.0.0.1:1"}},
                         tmp_path / "timer.yaml")


def test_timer_health_includes_last_job_result(tmp_path):
    from vps_deployer.deployment import Reconciler
    artifact = tmp_path / "artifact"; artifact.mkdir()
    dep = Deployment.parse({"name": "cleanup", "host": "prod", "service": {"user": "svc-cleanup"},
                            "release": {"source": str(artifact)}, "runtime": {"command": "./cleanup.sh"},
                            "timer": {"on_calendar": "daily"}}, tmp_path / "timer.yaml")
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    from vps_deployer.remote import Result
    class TimerRemote(RecordingRemote):
        result = "success"
        exit_status = "0"
        def run(self, argv, **kwargs):
            if argv[:2] == ["systemctl", "is-active"]:
                return Result("active\n", "", 0)
            if argv[:2] == ["systemctl", "show"]:
                return Result(f"Result={self.result}\nExecMainStatus={self.exit_status}\n"
                              "ExecMainExitTimestamp=Tue 2026-08-11 08:00:00 UTC\n", "", 0)
            return Result("", "", 0)
    remote = TimerRemote(); reconciler = Reconciler(repo, dep, remote, "same")
    assert reconciler._healthy()
    remote.result = "exit-code"; remote.exit_status = "1"
    assert not reconciler._healthy()


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


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:5100/health",
    "http://example.com:5100/health",
    "http://127.0.0.1:70000/health",
    "http://127.0.0.1:5100/health\nInjected: yes",
])
def test_healthcheck_requires_bounded_loopback_http_url(tmp_path, url):
    with pytest.raises(ConfigError, match="healthcheck"):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": {"command": "./run.sh"},
                          "healthcheck": {"type": "http", "url": url}}, tmp_path / "d.yaml")


@pytest.mark.parametrize("upstream", ["http://127.0.0.1:70000", "http://localhost:5100"])
def test_proxy_rejects_invalid_loopback_upstream(tmp_path, upstream):
    with pytest.raises(ConfigError, match="route"):
        Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                          "release": {"source": "x"}, "runtime": {"command": "./run.sh"},
                          "http_proxy": {"domain": "demo.test", "upstream": upstream, "tls": False}},
                         tmp_path / "d.yaml")


@pytest.mark.parametrize("collision", ["storage", "proxy", "port"])
def test_repository_rejects_same_host_resource_collisions(tmp_path, collision):
    repo = Repository(tmp_path)
    repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    def make(name, port, storage, proxy):
        data = {"name": name, "host": "prod", "service": {"user": f"svc-{name}"},
                "release": {"source": "x"}, "runtime": {"command": "./run.sh"},
                "storage": {"data": {"path": storage}},
                "healthcheck": {"type": "http", "url": f"http://127.0.0.1:{port}/health"},
                "http_proxy": {"name": proxy, "domain": f"{name}.test",
                               "upstream": f"http://127.0.0.1:{port}", "tls": False}}
        return Deployment.parse(data, tmp_path / f"{name}.yaml")
    first = make("one", "5101", "/var/lib/one", "one")
    second = make("two", "5102", "/var/lib/two", "two")
    if collision == "storage": second = make("two", "5102", "/var/lib/one", "two")
    if collision == "proxy": second = make("two", "5102", "/var/lib/two", "one")
    if collision == "port": second = make("two", "5101", "/var/lib/two", "two")
    repo.deployments = {first.name: first, second.name: second}
    with pytest.raises(ConfigError, match="conflicts"):
        repo.validate()


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
    assert len(first) == 16
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


@pytest.mark.parametrize("unsafe", ["line1\nline2", "tab\tvalue", "format\u202evalue", "separator\u2028value"])
def test_resolved_environment_rejects_control_characters(tmp_path, monkeypatch, unsafe):
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    repo.deployments[dep.name] = dep; repo.globals["prod"] = {"TZ": unsafe}
    monkeypatch.setenv("DEMO_TOKEN", "safe")
    with pytest.raises(ConfigError, match="control characters"):
        repo.resolve_environment(dep, True)


def test_resolved_secret_rejects_multiline_injection(tmp_path, monkeypatch):
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    repo.deployments[dep.name] = dep; repo.globals["prod"] = {"TZ": "UTC"}
    monkeypatch.setenv("DEMO_TOKEN", "secret\nINJECTED=value")
    with pytest.raises(ConfigError, match="control characters"):
        repo.resolve_environment(dep, True)


def test_git_build_metadata_records_exact_revision(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    (tmp_path / "app.py").write_text("app")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "test"], check=True)
    commit, rendered = git_metadata(tmp_path, "release-123")
    assert f"deployment.commit={commit}" in rendered
    assert "deployment.release=release-123" in rendered
    assert "deployment.actor=vps-deployer" in rendered


def test_build_properties_preserve_application_provenance():
    application = parse_build_properties("build.version=1.2.3\ncommit.hash=abc123\n")
    rendered = render_build_properties(application, {
        "deployment.release": "release-123", "deployment.commit": "def456"})
    assert parse_build_properties(rendered) == {
        "build.version": "1.2.3", "commit.hash": "abc123",
        "deployment.release": "release-123", "deployment.commit": "def456"}


@pytest.mark.parametrize("content", ["broken", "1bad=value", "same=one\nsame=two"])
def test_build_properties_reject_ambiguous_input(content):
    with pytest.raises(ConfigError):
        parse_build_properties(content)


def test_application_cannot_claim_deployment_properties():
    with pytest.raises(ConfigError, match="application-defined"):
        render_build_properties({"deployment.release": "fake"},
                                {"deployment.release": "actual"})


def test_rollback_selection():
    assert select_rollback(["f913abc", "28a01fe"], "28a01fe", "f913abc") == "f913abc"
    assert select_rollback(["003", "001", "002"], "003", "002", "001") == "001"
    with pytest.raises(ConfigError): select_rollback(["001"], "001", None)
    with pytest.raises(ConfigError): select_rollback(["001"], "001", "missing")


def test_release_pruning_keeps_active_and_immediate_predecessor():
    assert releases_to_prune(["new", "previous", "old", "oldest"], "new", "previous") == ["old", "oldest"]


def test_release_pruning_ignores_unsafe_directory_names():
    assert releases_to_prune(["new", "previous", "../state", "bad/name"], "new", "previous") == []


def test_failed_activation_restores_release_and_configuration(tmp_path):
    from vps_deployer.deployment import Reconciler
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    remote = RecordingRemote(); reconciler = Reconciler(repo, dep, remote, "new")
    with pytest.raises(RuntimeError, match="release and configuration restored"):
        reconciler._fail_activation("old", "OLD_ENV\n", "OLD_UNIT\n", None)
    commands = [call[0] for call in remote.calls]
    assert ["ln", "-sfn", "releases/old", f"{reconciler.base}/current"] in commands
    assert ["write_file", reconciler.env_path, b"OLD_ENV\n", "0640", "root", dep.user] in commands
    assert ["write_file", reconciler.unit_path, b"OLD_UNIT\n", "0644", "root", "root"] in commands
    assert commands[-1] == ["systemctl", "restart", reconciler.supervisor_name]


def test_failed_first_activation_stops_service_and_removes_new_configuration(tmp_path):
    from vps_deployer.deployment import Reconciler
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    remote = RecordingRemote(); reconciler = Reconciler(repo, dep, remote, "new")
    with pytest.raises(RuntimeError, match="first deployment stopped"):
        reconciler._fail_activation(None, None, None, None)
    commands = [call[0] for call in remote.calls]
    assert commands[0] == ["systemctl", "disable", "--now", reconciler.supervisor_name]
    assert ["rm", "-f", reconciler.env_path] in commands
    assert ["rm", "-f", reconciler.unit_path] in commands
    assert commands[-1] == ["systemctl", "daemon-reload"]


def test_obsolete_timer_and_proxy_are_reconciled_from_persisted_state(tmp_path):
    from vps_deployer.deployment import Reconciler
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    remote = RecordingRemote(); reconciler = Reconciler(repo, dep, remote, "same")
    reconciler._reconcile_obsolete_resources({"timer": True, "proxy": "old-proxy"})
    commands = [call[0] for call in remote.calls]
    assert ["systemctl", "disable", "--now", "vps-deployer-demo-prod.timer"] in commands
    assert ["rm", "-f", reconciler.timer_path] in commands
    assert ["rm", "-f", "/etc/nginx/sites-enabled/old-proxy"] in commands
    assert ["rm", "-f", "/etc/nginx/sites-available/old-proxy"] in commands


def test_managed_resource_metadata_is_strictly_parsed(tmp_path):
    from vps_deployer.deployment import Reconciler
    dep = deployment(tmp_path)
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    remote = RecordingRemote(); reconciler = Reconciler(repo, dep, remote, "same")
    remote.files[reconciler.state_path] = '{"timer":true,"proxy":"demo"}\n'
    assert reconciler.managed_resources() == {"timer": True, "proxy": "demo"}
    remote.files[reconciler.state_path] = '{"timer":"yes"}\n'
    with pytest.raises(ConfigError, match="managed resource metadata"):
        reconciler.managed_resources()


def test_invalid_nginx_candidate_restores_previous_enabled_site(tmp_path):
    from vps_deployer.deployment import Reconciler
    from vps_deployer.remote import RemoteError
    artifact = tmp_path / "artifact"; artifact.mkdir()
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"},
                            "http_proxy": {"name": "demo", "domain": "demo.test",
                                           "upstream": "http://127.0.0.1:5100", "tls": False}},
                           tmp_path / "d.yaml")
    repo = Repository(tmp_path); repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "test")
    class InvalidNginxRemote(RecordingRemote):
        def run(self, argv, **kwargs):
            super().run(argv, **kwargs)
            if argv == ["nginx", "-t"]:
                raise RemoteError("invalid candidate")
    remote = InvalidNginxRemote()
    reconciler = Reconciler(repo, dep, remote, "same")
    remote.files[str(reconciler.proxy_enabled)] = "symlink"
    remote.files[str(reconciler.proxy_available)] = "OLD\n"
    with pytest.raises(RemoteError):
        reconciler._reconcile_proxy()
    commands = [call[0] for call in remote.calls]
    assert ["ln", "-sfn", str(reconciler.proxy_available), str(reconciler.proxy_enabled)] in commands
    candidate = f"/etc/nginx/sites-available/vps-candidate-{__import__('hashlib').sha256(dep.name.encode()).hexdigest()[:16]}"
    assert ["rm", "-f", candidate] in commands


@pytest.mark.parametrize(("name", "kind"), [
    ("../outside", "file"),
    ("/etc/shadow", "file"),
    ("escape", "symlink"),
    ("device", "device"),
])
def test_release_archive_rejects_unsafe_members(tmp_path, name, kind):
    import io
    import tarfile
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo(name)
        if kind == "file":
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
        elif kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/shadow"
            output.addfile(member)
        else:
            member.type = tarfile.CHRTYPE
            output.addfile(member)
    with pytest.raises(ConfigError):
        validate_archive(archive)


def test_release_archive_accepts_regular_files_and_directories(tmp_path):
    import io
    import tarfile
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        directory = tarfile.TarInfo("bin"); directory.type = tarfile.DIRTYPE; output.addfile(directory)
        member = tarfile.TarInfo("bin/app"); member.size = 3; output.addfile(member, io.BytesIO(b"app"))
    validate_archive(archive)
