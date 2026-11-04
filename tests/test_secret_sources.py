"""Secret sources: env stays first and authoritative; file/keyring are fallbacks."""
import os
from pathlib import Path

import pytest

from vps_deployer import secret_sources
from vps_deployer.cli import main
from vps_deployer.config import Repository
from vps_deployer.models import ConfigError, Deployment, Host
from vps_deployer.secret_sources import SecretResolver, default_secrets_file, parse_secrets_file

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def run_cli(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def secrets_file(tmp_path, text="A=1\n"):
    path = tmp_path / "secrets.env"
    path.write_text(text, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_parse_file_formats():
    values = parse_secrets_file("# c\n\nA=1\nexport B = 'two words'\nC=\"x\"\nD=a=b\n", "f")
    assert values == {"A": "1", "B": "two words", "C": "x", "D": "a=b"}
    with pytest.raises(ConfigError, match="f:1"):
        parse_secrets_file("not valid\n", "f")


def test_environment_wins_over_file(tmp_path):
    path = secrets_file(tmp_path, "TOKEN=from-file\nONLY_FILE=f\n")
    resolver = SecretResolver(("env", "file"), {"TOKEN": "from-env"}, path)
    assert resolver.lookup("TOKEN") == ("env", "from-env")
    assert resolver.lookup("ONLY_FILE") == ("file", "f")
    assert resolver.lookup("MISSING") is None


def test_default_chain_is_unchanged_without_a_file(tmp_path):
    resolver = SecretResolver.from_environment({"X": "1", "VPS_DEPLOYER_SECRETS_FILE": str(tmp_path / "none.env")})
    assert resolver.sources == ("env", "file")
    assert resolver.get("X") == "1" and resolver.get("Y") is None


def test_sources_variable_and_unknown_source():
    env = {"VPS_DEPLOYER_SECRET_SOURCES": "file, env"}
    assert SecretResolver.from_environment(env).sources == ("file", "env")
    with pytest.raises(ConfigError, match="unknown secret source"):
        SecretResolver(("vault",))


def test_default_file_location(tmp_path):
    assert default_secrets_file({"VPS_DEPLOYER_SECRETS_FILE": str(tmp_path / "s.env")}) == tmp_path / "s.env"
    assert default_secrets_file({"APPDATA": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}).name == "secrets.env"


@posix_only
def test_open_permissions_are_refused(tmp_path):
    path = secrets_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="too open"):
        SecretResolver(("file",), {}, path).get("A")


def test_store_creates_and_replaces_entries(tmp_path):
    path = tmp_path / "sub" / "secrets.env"
    resolver = SecretResolver(("file",), {}, path)
    resolver.store("file", "TOKEN", "one")
    resolver.store("file", "OTHER", "x")
    resolver.store("file", "TOKEN", "two")
    assert parse_secrets_file(path.read_text(), "f") == {"TOKEN": "two", "OTHER": "x"}
    assert resolver.get("TOKEN") == "two"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ConfigError):
        resolver.store("file", "BAD", "line\nbreak")
    with pytest.raises(ConfigError):
        resolver.store("file", "1bad", "v")


def test_keyring_source_needs_optional_dependency(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    with pytest.raises(ConfigError, match="keyring"):
        SecretResolver(("keyring",), {}).get("X")


def test_keyring_source_with_fake_module(monkeypatch):
    class Fake:
        store = {}
        @classmethod
        def get_password(cls, service, name): return cls.store.get((service, name))
        @classmethod
        def set_password(cls, service, name, value): cls.store[(service, name)] = value
    monkeypatch.setitem(__import__("sys").modules, "keyring", Fake)
    resolver = SecretResolver(("env", "keyring"), {})
    resolver.store("keyring", "TOKEN", "k")
    assert resolver.lookup("TOKEN") == ("keyring", "k")


def make_repo(tmp_path, resolver):
    artifact = tmp_path / "a"; artifact.mkdir()
    dep = Deployment.parse({"name": "demo", "host": "prod", "service": {"user": "svc-demo"},
                            "release": {"source": str(artifact)}, "runtime": {"command": "./run.sh"},
                            "secrets": {"TOKEN": {"from_env": "DEMO_TOKEN"}}}, tmp_path / "d.yaml")
    repo = Repository(tmp_path)
    repo.hosts["prod"] = Host.parse({"name": "prod", "ssh": {"host": "prod"}}, "t")
    repo.deployments["demo"] = dep
    repo.secret_resolver = resolver
    return repo, dep


def test_repository_resolves_through_the_chain(tmp_path):
    path = secrets_file(tmp_path, "DEMO_TOKEN=filesecret\n")
    repo, dep = make_repo(tmp_path, SecretResolver(("env", "file"), {}, path))
    assert repo.missing_secrets(dep) == []
    values, keys = repo.resolve_environment(dep, True)
    assert values["TOKEN"] == "filesecret" and keys == {"TOKEN"}
    repo.secret_resolver = SecretResolver(("env", "file"), {}, tmp_path / "absent.env")
    assert repo.missing_secrets(dep) == [("TOKEN", "DEMO_TOKEN")]
    with pytest.raises(ConfigError) as exc:
        repo.resolve_environment(dep, True)
    assert "filesecret" not in str(exc.value)


def test_secrets_set_and_sources_commands(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(root))
    monkeypatch.setenv("VPS_DEPLOYER_SECRETS_FILE", str(tmp_path / "secrets.env"))
    monkeypatch.delenv("INVOICER_PROD_DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("s3cret-value\n"))
    assert run_cli(["secrets", "set", "INVOICER_PROD_DATABASE_URL", "--stdin"]) == 0
    out = capsys.readouterr()
    assert "s3cret-value" not in out.out + out.err
    assert run_cli(["--repo", str(root / "examples"), "secrets", "sources", "invoicer-prod"]) == 0
    table = capsys.readouterr().out
    assert "INVOICER_PROD_DATABASE_URL" in table and "file" in table and "s3cret-value" not in table
    assert run_cli(["--repo", str(root / "examples"), "secrets", "check", "invoicer-prod"]) == 0


def test_sources_exit_code_reports_unset(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(root))
    monkeypatch.setenv("VPS_DEPLOYER_SECRETS_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("INVOICER_PROD_DATABASE_URL", raising=False)
    assert run_cli(["--repo", str(root / "examples"), "secrets", "sources"]) == 1
    assert "UNSET" in capsys.readouterr().out


def test_remove_and_holders(tmp_path):
    path = secrets_file(tmp_path, "# keep\nA=1\nexport B=2\nC=3\n")
    resolver = SecretResolver(("env", "file"), {"A": "env"}, path)
    assert resolver.holders("A") == ["env", "file"]
    assert resolver.file_names() == ["A", "B", "C"]
    assert resolver.remove("file", "B") is True
    assert resolver.remove("file", "B") is False
    assert parse_secrets_file(path.read_text(), "f") == {"A": "1", "C": "3"}
    assert "# keep" in path.read_text()
    assert resolver.remove("file", "A") and resolver.remove("file", "C")
    assert path.read_text().strip() == "# keep"
    assert SecretResolver(("file",), {}, tmp_path / "none.env").remove("file", "X") is False


def test_keyring_remove_with_fake_module(monkeypatch):
    class PasswordDeleteError(Exception): pass
    class Fake:
        store = {("vps-deployer", "T"): "k"}
        @classmethod
        def get_password(cls, service, name): return cls.store.get((service, name))
        @classmethod
        def delete_password(cls, service, name):
            if (service, name) not in cls.store: raise PasswordDeleteError()
            del cls.store[(service, name)]
    monkeypatch.setitem(__import__("sys").modules, "keyring", Fake)
    resolver = SecretResolver(("keyring",), {})
    assert resolver.holders("T") == ["keyring"]
    assert resolver.remove("keyring", "T") is True
    assert resolver.remove("keyring", "T") is False


def test_list_unset_and_rotate_commands(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(root))
    monkeypatch.setenv("VPS_DEPLOYER_SECRETS_FILE", str(tmp_path / "secrets.env"))
    monkeypatch.delenv("INVOICER_PROD_DATABASE_URL", raising=False)
    repo = ["--repo", str(root / "examples")]
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("v1\n"))
    assert run_cli([*repo, "secrets", "rotate", "INVOICER_PROD_DATABASE_URL", "--stdin"]) == 0
    out = capsys.readouterr().out
    assert "invoicer-prod" in out and "v1" not in out
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("x\n"))
    run_cli(["secrets", "set", "STRAY_ONE", "--stdin"]); capsys.readouterr()
    assert run_cli([*repo, "secrets", "list"]) == 0
    listing = capsys.readouterr().out
    assert "INVOICER_PROD_DATABASE_URL" in listing and "file" in listing and "STRAY_ONE" in listing
    assert "v1" not in listing
    assert run_cli([*repo, "secrets", "unset", "INVOICER_PROD_DATABASE_URL"]) == 0
    captured = capsys.readouterr()
    assert "invoicer-prod" in captured.err
    assert run_cli([*repo, "secrets", "unset", "INVOICER_PROD_DATABASE_URL"]) == 1
    assert run_cli([*repo, "secrets", "list"]) == 1
