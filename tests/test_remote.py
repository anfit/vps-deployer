from vps_deployer.models import Host
import base64
import json
from vps_deployer.remote import RemoteHost


def test_ssh_uses_config_target_and_quotes_arguments():
    host = Host.parse({"name": "prod", "ssh": {"host": "alias", "user": "deploy"}}, "test")
    argv = RemoteHost(host).ssh_argv(["test", "-e", "/path with space"], sudo=True)
    assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "--", "deploy@alias"]
    assert argv[-1] == "sudo -- test -e '/path with space'"


def test_privileged_host_uses_direct_root_transport():
    host = Host.parse({"name": "prod", "ssh": {"host": "alias", "user": "deploy",
                                                       "privileged_host": "server", "privileged_user": "root",
                                                       "privileged_identity_file": "~/.ssh/deploy"}}, "test")
    argv = RemoteHost(host).ssh_argv(["systemctl", "daemon-reload"], sudo=True)
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "-i"]
    assert argv[5:7] == ["--", "root@server"]
    assert argv[7].startswith("vps-deployer-op ")
    payload = argv[7].split()[1]
    assert json.loads(base64.urlsafe_b64decode(payload)) == {
        "operation": "service-control", "arguments": ["daemon-reload"]}


def test_privileged_write_uses_stdin_capability(monkeypatch):
    host = Host.parse({"name": "prod", "ssh": {"host": "alias", "user": "deploy",
                                                       "privileged_host": "server", "privileged_user": "root"}}, "test")
    captured = {}
    remote = RemoteHost(host)
    monkeypatch.setattr(remote, "run", lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs))
    remote.write_file("/etc/vps-deployer/demo.env", b"SECRET=value\n", "0640", "root", "svc-demo")
    assert captured["argv"] == ["write-file", "/etc/vps-deployer/demo.env", "root", "svc-demo", "0640"]
    assert captured["kwargs"]["input_data"] == b"SECRET=value\n"


def test_privileged_systemd_write_uses_validated_unit_capability(monkeypatch):
    host = Host.parse({"name": "prod", "ssh": {"host": "alias", "user": "deploy",
                                                       "privileged_host": "server", "privileged_user": "root"}}, "test")
    captured = {}
    remote = RemoteHost(host)
    monkeypatch.setattr(remote, "run", lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs))
    path = "/etc/systemd/system/vps-deployer-demo.service"
    remote.write_file(path, b"validated unit\n", "0644", "root", "root")
    assert captured["argv"] == ["write-unit", path]
    assert captured["kwargs"]["input_data"] == b"validated unit\n"
