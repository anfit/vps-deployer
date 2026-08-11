from pathlib import Path
import runpy

import pytest


GATE = runpy.run_path(str(Path(__file__).parents[1] / "tools" / "vps-deployer-ssh-gate"))
build_argv = GATE["build_argv"]
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


def test_gate_write_file_has_no_shared_temporary_path():
    argv = build_argv(request("write-file", "/etc/vps-deployer/demo.env", "root", "svc-demo", "0640"),
                      ROOT, STORAGE)
    assert argv == ["__write_file__", "/etc/vps-deployer/demo.env", "root", "svc-demo", "0640"]


def test_gate_never_reads_privileged_temporary_paths():
    with pytest.raises(ValueError):
        build_argv(request("read-file", "/tmp/vps-deployer-demo-link"), ROOT, STORAGE)


def test_gate_lock_is_scoped_to_a_deployment_name():
    assert build_argv(request("hold-lock", "demo-prod"), ROOT, STORAGE) == ["__hold_lock__", "demo-prod"]
    with pytest.raises(ValueError):
        build_argv(request("hold-lock", "../other"), ROOT, STORAGE)


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
