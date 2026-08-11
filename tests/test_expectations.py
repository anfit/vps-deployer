from pathlib import Path

import pytest

from vps_deployer.expectations import evaluate_expectations, require_expectations
from vps_deployer.models import ConfigError, Deployment
from vps_deployer.remote import Result


class ExpectationRemote:
    def __init__(self, *, commands=(), versions=None, paths=(), architecture="x86_64"):
        self.commands = set(commands)
        self.versions = versions or {}
        self.paths = set(paths)
        self.architecture = architecture
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ["uname", "-m"]:
            return Result(self.architecture + "\n", "", 0)
        if argv == ["nginx", "-t"]:
            return Result("", "", 0 if "nginx" in self.commands else 1)
        if argv[:2] == ["sh", "-c"]:
            return Result("", "", 0 if argv[-1] in self.commands else 1)
        if argv[:2] == ["test", "-e"]:
            return Result("", "", 0 if argv[2] in self.paths else 1)
        if len(argv) == 2 and argv[1] == "--version":
            value = self.versions.get(argv[0])
            return Result((value + "\n") if value else "", "", 0 if value else 1)
        return Result("", "", 1)


def manifest(tmp_path: Path, expect=None) -> Deployment:
    source = tmp_path / "app"; source.mkdir(exist_ok=True)
    data = {"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
            "release": {"source": str(source)}}
    if expect is not None:
        data["expect"] = expect
    return Deployment.parse(data, tmp_path / "demo.yaml")


def test_application_and_infrastructure_expectations_merge(tmp_path):
    source = tmp_path / "app"; contract = source / ".deployer"
    contract.mkdir(parents=True)
    (contract / "expect.yaml").write_text(
        "commands:\n  python3:\n    version: '>=3.12'\narchitecture: x86_64\n")
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(source)},
                            "expect": {"paths": ["/etc/example/cert.pem"]}},
                           tmp_path / "demo.yaml")
    assert [(item.name, item.version) for item in dep.expectations.commands] == [("python3", ">=3.12")]
    assert dep.expectations.paths == ("/etc/example/cert.pem",)
    assert dep.expectations.architecture == "x86_64"


def test_expectations_include_implicit_tools_and_versions(tmp_path):
    dep = manifest(tmp_path, {"commands": {"python3": {"version": ">=3.12"}},
                              "architecture": "x86_64"})
    remote = ExpectationRemote(commands={"python3", "systemctl", "tar"},
                               versions={"python3": "Python 3.12.7"})
    results = require_expectations(dep, remote)
    assert all(result.ok for result in results)
    assert {result.subject for result in results} >= {
        "architecture", "command python3", "command systemctl", "command tar"}


def test_proxy_uses_privileged_read_only_nginx_validation(tmp_path):
    source = tmp_path / "app"; source.mkdir()
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(source)},
                            "http_proxy": {"domain": "demo.test",
                                           "upstream": "http://127.0.0.1:5100", "tls": False}},
                           tmp_path / "demo.yaml")
    remote = ExpectationRemote(commands={"nginx", "systemctl", "tar"})
    assert all(result.ok for result in evaluate_expectations(dep, remote))
    assert ["nginx", "-t"] in remote.calls


def test_expectation_failures_are_aggregated(tmp_path):
    dep = manifest(tmp_path, {"commands": {"python3": {"version": ">=3.12"}},
                              "paths": ["/etc/example/cert.pem"], "architecture": "aarch64"})
    remote = ExpectationRemote(commands={"python3", "systemctl", "tar"},
                               versions={"python3": "Python 3.11.9"})
    with pytest.raises(ConfigError) as error:
        require_expectations(dep, remote)
    message = str(error.value)
    assert "architecture" in message
    assert "does not satisfy >=3.12" in message
    assert "/etc/example/cert.pem" in message


@pytest.mark.parametrize("expect", [
    {"commands": {"python3": {"version": "~=3.12"}}},
    {"commands": ["bad command"]},
    {"paths": ["relative/path"]},
    {"architecture": "x86_64\nother"},
    {"remediate": "apt install"},
])
def test_expectation_schema_rejects_unsafe_or_remediating_forms(tmp_path, expect):
    with pytest.raises(ConfigError):
        manifest(tmp_path, expect)
