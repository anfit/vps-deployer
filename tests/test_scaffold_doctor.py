"""Tests for init, init app, doctor, list and --all commands."""
from pathlib import Path

import pytest

from vps_deployer import cli
from vps_deployer.cli import main, render_listing
from vps_deployer.config import Repository
from vps_deployer.doctor import ERROR, WARN, diagnose
from vps_deployer.models import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def levels(findings):
    return {(f.level, f.subject) for f in findings}


def test_init_creates_a_valid_infra_repository(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    infra = tmp_path / "infra"
    assert run_cli(["init", str(infra)]) == 0
    assert "create  hosts/prod.yaml" in capsys.readouterr().out
    assert run_cli(["--repo", str(infra), "validate"]) == 0
    assert "EXAMPLE_SERVICE_PROD_API_TOKEN=" in (infra / ".env.example").read_text()
    # Second run never overwrites.
    (infra / "README.md").write_text("mine")
    assert run_cli(["init", str(infra)]) == 0
    assert (infra / "README.md").read_text() == "mine"
    assert "skip    README.md" in capsys.readouterr().out


def test_init_app_output_passes_doctor(tmp_path, capsys):
    app = tmp_path / "app"
    assert run_cli(["init", "app", str(app)]) == 0
    assert "create  .deployer/run.sh" in capsys.readouterr().out
    findings = diagnose(app)
    assert not [f for f in findings if f.level == ERROR]
    assert b"\r\n" not in (app / ".deployer" / "run.sh").read_bytes()
    assert "eol=lf" in (app / ".gitattributes").read_text()


def test_init_app_extends_existing_gitattributes(tmp_path):
    app = tmp_path / "app"; app.mkdir()
    (app / ".gitattributes").write_text("* text=auto")
    cli.init_app(app)
    text = (app / ".gitattributes").read_text()
    assert text.startswith("* text=auto\n") and "*.sh text eol=lf" in text


def test_doctor_requires_deployer_directory(tmp_path):
    assert (ERROR, ".deployer/") in levels(diagnose(tmp_path))


def test_doctor_flags_crlf_and_missing_shebang(tmp_path):
    d = tmp_path / ".deployer"; d.mkdir()
    (d / "run.sh").write_bytes(b"#!/bin/sh\r\nexec true\r\n")
    (d / "bootstrap.sh").write_bytes(b"echo hi\n")
    found = levels(diagnose(tmp_path))
    assert (ERROR, ".deployer/run.sh") in found
    assert (ERROR, ".deployer/bootstrap.sh") in found


def test_doctor_reports_credential_like_files_and_bad_expectations(tmp_path):
    d = tmp_path / ".deployer"; d.mkdir()
    (d / "run.sh").write_text("#!/bin/sh\n")
    (d / "ignore.txt").write_text("state.json\n")
    (d / "expect.yaml").write_text("commands: 5\n")
    (tmp_path / "server.pem").write_text("x")
    (tmp_path / ".env.example").write_text("A=")
    (tmp_path / "state.json").write_text("{}")  # ignored by ignore.txt
    found = levels(diagnose(tmp_path))
    assert (ERROR, ".deployer/expect.yaml") in found
    assert (WARN, "release contents") in found
    subjects = [f.detail for f in diagnose(tmp_path) if f.subject == "release contents"]
    assert any("server.pem" in s for s in subjects)
    assert not any("state.json" in s for s in subjects)
    assert not any(".env.example" in s for s in subjects)


def test_doctor_checks_manifests_pointing_at_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    app = ROOT / "examples" / "artifacts" / "invoicer"
    repo = Repository.load(ROOT / "examples")
    found = levels(diagnose(app, repo))
    assert (cli.ERROR, "deployment invoicer-prod") not in found
    assert ("ok", "deployment invoicer-prod") in found


def test_doctor_command_exit_code(tmp_path, capsys):
    assert run_cli(["doctor", str(tmp_path)]) == 1
    assert "missing" in capsys.readouterr().out


def test_list_renders_table_and_markdown(monkeypatch):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    repo = Repository.load(ROOT / "examples")
    table = render_listing(repo, "table")
    assert "invoicer-prod" in table and "service" in table and "127.0.0.1:8104" in table
    md = render_listing(repo, "markdown")
    assert md.splitlines()[0].startswith("| deployment") and "`invoicer-prod`" in md
    assert "`INVOICER_PROD_DATABASE_URL`" in md and "`/var/lib/invoicer`" in md


def test_plan_and_status_need_target(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    assert run_cli(["--repo", str(ROOT / "examples"), "plan"]) == 2
    assert run_cli(["--repo", str(ROOT / "examples"), "status", "invoicer-prod", "--all"]) == 2
    assert "not both" in capsys.readouterr().err


class FakeReconciler:
    actions = []

    def __init__(self, *a, **k): pass

    def plan(self, _): return list(self.actions)


def test_plan_all_summarises_and_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("VPS_DEPLOYER_DIR", str(ROOT))
    monkeypatch.setenv("INVOICER_PROD_DATABASE_URL", "x")
    monkeypatch.setattr(cli, "_remote", lambda *a, **k: object())
    monkeypatch.setattr(cli, "Reconciler", FakeReconciler)
    argv = ["--repo", str(ROOT / "examples"), "plan", "--all"]
    FakeReconciler.actions = []
    assert run_cli(argv) == 0
    assert "0 with changes" in capsys.readouterr().out
    FakeReconciler.actions = ["CREATE something"]
    assert run_cli(argv) == 0
    assert run_cli(argv + ["--exit-code"]) == 1
    assert "1 with changes" in capsys.readouterr().out
