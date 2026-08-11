from pathlib import Path
import runpy

import pytest

from vps_deployer.models import Deployment
from vps_deployer.systemd import render_timer, render_unit, timer_name, unit_name


GATE = runpy.run_path(str(Path(__file__).parents[1] / "tools" / "vps-deployer-ssh-gate"))
build_argv = GATE["build_argv"]
validate_unit = GATE["validate_unit"]
ROOT = "/srv/custom"
STORAGE = {"/var/lib/demo"}


def request(operation, *arguments):
    return {"operation": operation, "arguments": list(arguments)}


@pytest.mark.parametrize("operation", [
    request("extract-release", "-xzf", "/tmp/vps-deployer-demo-a.tar.gz", "-C", "/srv/custom/demo/releases/a"),
    request("install", "-d", "-o", "svc-demo", "-g", "svc-demo", "-m", "0750", "/var/lib/demo"),
    request("set-link", "-sfn", "releases/a", "/srv/custom/demo/current"),
    request("service-control", "restart", "vps-deployer-demo.service"),
])
def test_gate_builds_fixed_commands_for_capabilities(operation):
    argv = build_argv(operation, ROOT, STORAGE)
    assert argv[0] in {"tar", "install", "ln", "systemctl"}


def test_tar_capability_adds_defensive_flags():
    argv = build_argv(request("extract-release", "-xzf", "/tmp/vps-deployer-demo-a.tar.gz",
                              "-C", "/srv/custom/demo/releases/a"), ROOT, STORAGE)
    assert argv == ["tar", "--no-same-owner", "--no-same-permissions", "-xzf",
                    "/tmp/vps-deployer-demo-a.tar.gz", "-C", "/srv/custom/demo/releases/a"]


@pytest.mark.parametrize("operation", [
    request("extract-release", "--checkpoint-action=exec=sh", "/tmp/x", "-C", "/srv/custom/demo"),
    request("install", "-d", "-o", "root", "-g", "root", "-m", "0750", "/var/lib/other"),
    request("remove-paths", "-rf", "/var/lib/demo"),
    request("service-control", "restart", "sshd.service"),
    request("set-link", "-s", "/etc/shadow", "/srv/custom/demo/current"),
])
def test_gate_rejects_shell_power_and_cross_service_paths(operation):
    with pytest.raises(ValueError):
        build_argv(operation, ROOT, STORAGE)


def test_gate_honors_configured_managed_root():
    operation = request("path-exists", "-e", "/srv/vps-deployer/demo/current")
    with pytest.raises(ValueError):
        build_argv(operation, ROOT, STORAGE)


def test_gate_expectation_path_reveals_only_absolute_path_existence():
    assert build_argv(request("expect-path", "-e", "/etc/example/cert.pem"), ROOT, STORAGE) == [
        "test", "-e", "/etc/example/cert.pem"]
    for path in ("/", "relative", "/etc/../shadow", "/path with spaces"):
        with pytest.raises(ValueError):
            build_argv(request("expect-path", "-e", path), ROOT, STORAGE)


def test_gate_write_file_has_no_shared_temporary_path():
    argv = build_argv(request("write-file", "/etc/vps-deployer/demo.env", "root", "svc-demo", "0640"),
                      ROOT, STORAGE)
    assert argv == ["__write_file__", "/etc/vps-deployer/demo.env", "root", "svc-demo", "0640"]


def service_unit(*, user="svc-demo", command="/srv/custom/demo/current/.deployer/run"):
    return f"""[Unit]
Description=vps-deployer service demo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory=/srv/custom/demo/current
EnvironmentFile=/etc/vps-deployer/demo.env
ExecStart={command}
Restart=always
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/demo


[Install]
WantedBy=multi-user.target
"""


def test_gate_accepts_only_valid_non_root_managed_units():
    target = "/etc/systemd/system/vps-deployer-demo.service"
    assert build_argv(request("write-unit", target), ROOT, STORAGE) == ["__write_unit__", target]
    validate_unit(target, service_unit(), ROOT, STORAGE)


@pytest.mark.parametrize(("user", "command"), [
    ("root", "/bin/sh"),
    ("svc-demo", "/bin/sh"),
    ("svc-demo", "/srv/custom/other/current/run"),
])
def test_gate_rejects_units_that_escape_non_root_release_policy(user, command):
    with pytest.raises(ValueError):
        validate_unit("/etc/systemd/system/vps-deployer-demo.service",
                      service_unit(user=user, command=command), ROOT, STORAGE)


def test_opaque_write_file_cannot_replace_systemd_units():
    with pytest.raises(ValueError):
        build_argv(request("write-file", "/etc/systemd/system/vps-deployer-demo.service",
                           "root", "root", "0644"), ROOT, STORAGE)


@pytest.mark.parametrize(("scheduled", "with_storage"), [
    (False, False), (False, True), (True, False), (True, True)])
def test_gate_accepts_client_rendered_hardened_units(tmp_path, scheduled, with_storage):
    data = {"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
            "release": {"source": str(tmp_path)}}
    if with_storage:
        data["storage"] = {"data": {"path": "/var/lib/demo"}}
    if scheduled:
        data["timer"] = {"on_calendar": "daily", "randomized_delay_sec": 60}
    dep = Deployment.parse(data, tmp_path / "demo.yaml")
    validate_unit(f"/etc/systemd/system/{unit_name(dep)}", render_unit(dep, ROOT), ROOT, STORAGE)
    if scheduled:
        validate_unit(f"/etc/systemd/system/{timer_name(dep)}", render_timer(dep), ROOT, STORAGE)


def test_gate_never_reads_privileged_temporary_paths():
    with pytest.raises(ValueError):
        build_argv(request("read-file", "/tmp/vps-deployer-demo-link"), ROOT, STORAGE)


def test_gate_lock_is_scoped_to_a_deployment_name():
    assert build_argv(request("hold-lock", "demo-prod"), ROOT, STORAGE) == ["__hold_lock__", "demo-prod"]
    with pytest.raises(ValueError):
        build_argv(request("hold-lock", "../other"), ROOT, STORAGE)


def test_gate_allows_only_fixed_timer_result_inspection():
    operation = request("service-control", "show", "vps-deployer-demo.service", "--no-pager",
                        "--property=Result", "--property=ExecMainStatus",
                        "--property=ExecMainExitTimestamp")
    assert build_argv(operation, ROOT, STORAGE)[0] == "systemctl"
    operation["arguments"][-1] = "--property=Environment"
    with pytest.raises(ValueError):
        build_argv(operation, ROOT, STORAGE)


@pytest.mark.parametrize("value", ["bad\nvalue", "bad\tvalue", "bad\u202evalue", "bad\u2028value"])
def test_gate_rejects_ascii_and_unicode_controls(value):
    with pytest.raises(ValueError):
        build_argv(request("path-exists", "-e", f"/srv/custom/demo/{value}"), ROOT, STORAGE)


@pytest.mark.parametrize("tail", [
    ["--since", "--root=/", "--follow"],
    ["--output", "json"],
    ["--since"],
])
def test_gate_rejects_journal_option_injection(tail):
    operation = request("service-logs", "-u", "vps-deployer-demo.service", "--no-pager", "-n", "100", *tail)
    with pytest.raises(ValueError):
        build_argv(operation, ROOT, STORAGE)
