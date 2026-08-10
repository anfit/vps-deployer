from vps_deployer.models import Host
from vps_deployer.remote import RemoteHost


def test_ssh_uses_config_target_and_quotes_arguments():
    host = Host.parse({"name": "prod", "ssh": {"host": "alias", "user": "deploy"}}, "test")
    argv = RemoteHost(host).ssh_argv(["test", "-e", "/path with space"], sudo=True)
    assert argv[:3] == ["ssh", "--", "deploy@alias"]
    assert argv[-1] == "sudo -- test -e '/path with space'"

