"""Offline preflight of an application's `.deployer/` contract."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml

from .deployment import validate_runtime_entrypoint, walk_release_files
from .models import ConfigError, parse_expectations

OK, WARN, ERROR = "ok", "warn", "error"

# Names that usually indicate developer-local or secret material.
SUSPICIOUS = (".env.*", "*.pem", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*",
              "credentials*", "*.sqlite*", "*.db", "*.log", "state.json", "token*.json",
              "config.yml", "*.local.*")
SAFE_NAMES = {".env.example", ".env.sample", ".env.template"}
SCAN_LIMIT = 20000


@dataclass(frozen=True)
class Finding:
    level: str
    subject: str
    detail: str


def _shell_scripts(deployer_dir: Path) -> list[Path]:
    return sorted(p for p in deployer_dir.glob("*.sh") if p.is_file())


def _check_script(app: Path, script: Path) -> list[Finding]:
    rel = script.relative_to(app).as_posix()
    data = script.read_bytes()
    findings: list[Finding] = []
    if b"\r\n" in data:
        findings.append(Finding(ERROR, rel, "CRLF line endings break the shebang on Linux; "
                                            "convert to LF and add a .gitattributes rule"))
    if not data.startswith(b"#!"):
        findings.append(Finding(ERROR, rel, "missing shebang (first line must start with #!)"))
    elif b"\r" not in data.split(b"\n", 1)[0]:
        findings.append(Finding(OK, rel, "LF endings and shebang present"))
    return findings


def _check_ignore(app: Path) -> list[Finding]:
    findings: list[Finding] = []
    ignore = app / ".deployer" / "ignore.txt"
    legacy = app / ".vps-deployer-ignore"
    if ignore.is_file():
        findings.append(Finding(OK, ".deployer/ignore.txt", "present"))
    elif legacy.is_file():
        findings.append(Finding(WARN, ".vps-deployer-ignore",
                                "legacy ignore file; move it to .deployer/ignore.txt"))
    else:
        findings.append(Finding(WARN, ".deployer/ignore.txt",
                                "absent; only built-in exclusions keep local files out of releases"))
    return findings


def _check_included_files(app: Path) -> list[Finding]:
    included, suspicious = 0, []
    for path in walk_release_files(app):
        included += 1
        if included > SCAN_LIMIT:
            return [Finding(WARN, "release contents", f"more than {SCAN_LIMIT} files; scan skipped")]
        rel = path.relative_to(app).as_posix()
        if path.name not in SAFE_NAMES and any(fnmatch(path.name, pattern) for pattern in SUSPICIOUS):
            suspicious.append(rel)
    findings = [Finding(WARN, "release contents", f"{rel} would be uploaded; add it to .deployer/ignore.txt "
                                                  "unless it is intentional") for rel in sorted(suspicious)[:25]]
    if not suspicious:
        findings.append(Finding(OK, "release contents", f"{included} file(s); no credential-like names"))
    return findings


def _check_line_endings_rule(app: Path) -> list[Finding]:
    attrs = app / ".gitattributes"
    if (app / ".git").exists() and not (attrs.is_file() and "eol=lf" in attrs.read_text(encoding="utf-8", errors="replace")):
        return [Finding(WARN, ".gitattributes", "no eol=lf rule; Windows checkouts can silently turn "
                                                "shell scripts into CRLF")]
    return []


def _check_expectations(app: Path) -> list[Finding]:
    path = app / ".deployer" / "expect.yaml"
    if not path.is_file():
        return [Finding(WARN, ".deployer/expect.yaml", "absent; host assumptions are undeclared")]
    try:
        parse_expectations(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path))
    except (ConfigError, yaml.YAMLError) as exc:
        return [Finding(ERROR, ".deployer/expect.yaml", str(exc))]
    return [Finding(OK, ".deployer/expect.yaml", "valid")]


def _check_manifests(app: Path, repo) -> list[Finding]:
    findings: list[Finding] = []
    for dep in repo.deployments.values():
        if dep.source.resolve() != app.resolve():
            continue
        try:
            validate_runtime_entrypoint(dep)
            findings.append(Finding(OK, f"deployment {dep.name}", "runtime entrypoint present in release"))
        except ConfigError as exc:
            findings.append(Finding(ERROR, f"deployment {dep.name}", str(exc)))
    return findings


def diagnose(app: Path, repo=None) -> list[Finding]:
    app = app.resolve()
    if not app.is_dir():
        return [Finding(ERROR, str(app), "not a directory")]
    deployer = app / ".deployer"
    if not deployer.is_dir():
        return [Finding(ERROR, ".deployer/", "missing; run `vps-deployer init app` to create it")]
    findings: list[Finding] = []
    scripts = _shell_scripts(deployer)
    if (deployer / "run.sh").is_file():
        findings.append(Finding(OK, ".deployer/run.sh", "present"))
    elif scripts:
        findings.append(Finding(WARN, ".deployer/run.sh",
                                "absent; manifests must select a named entrypoint explicitly"))
    else:
        findings.append(Finding(ERROR, ".deployer/run.sh", "no entrypoint script found"))
    for script in scripts:
        findings += _check_script(app, script)
    findings += _check_expectations(app) + _check_ignore(app) + _check_included_files(app)
    findings += _check_line_endings_rule(app)
    if repo is not None:
        findings += _check_manifests(app, repo)
    return findings
