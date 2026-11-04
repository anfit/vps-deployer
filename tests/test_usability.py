"""Tests for first-run usability: examples, secret preflight, repo discovery."""
from pathlib import Path

import pytest

from vps_deployer.cli import main, resolve_repo_path
from vps_deployer.config import Repository
from vps_deployer.deployment import release_id

ROOT = Path(__file__).resolve().parents[1]


def test_readme_example_validates_and_resolves_release(monkeypatch):
    """The documented quick-start must work offline: validate + local release ID."""
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    repo = Repository.load(ROOT / "examples")
    assert "invoicer-prod" in repo.deployments
    assert len(release_id(repo.deployments["invoicer-prod"])) == 16


def test_example_validate_command(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "validate"])
    assert exc.value.code == 0
    assert "Valid" in capsys.readouterr().out


def test_secrets_check_reports_names_only(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    monkeypatch.delenv("INVOICER_PROD_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "secrets", "check"])
    assert exc.value.code == 1
    assert "INVOICER_PROD_DATABASE_URL" in capsys.readouterr().out
    monkeypatch.setenv("INVOICER_PROD_DATABASE_URL", "postgres://user:hunter2@db/x")
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "secrets", "check", "invoicer-prod"])
    out = capsys.readouterr().out
    assert exc.value.code == 0 and "hunter2" not in out and "all 1 secret" in out


def test_apply_fails_before_connecting_when_secrets_missing(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    monkeypatch.delenv("INVOICER_PROD_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--repo", str(ROOT / "examples"), "apply", "invoicer-prod"])
    assert exc.value.code == 2
    assert "INVOICER_PROD_DATABASE_URL" in capsys.readouterr().err


def test_repo_resolution_order(tmp_path):
    cwd = tmp_path / "cwd"; cwd.mkdir()
    env = {"VPS_DEPLOYER_REPO": "/a", "SERVICE_INFRA_DIR": "/b"}
    assert resolve_repo_path(Path("/x"), cwd, env) == Path("/x")
    assert resolve_repo_path(None, cwd, env) == Path("/a")
    assert resolve_repo_path(None, cwd, {"SERVICE_INFRA_DIR": "/b"}) == Path("/b")
    assert resolve_repo_path(None, cwd, {}) == cwd
    (cwd / "hosts").mkdir(); (cwd / "deployments").mkdir()
    assert resolve_repo_path(None, cwd, env) == cwd
