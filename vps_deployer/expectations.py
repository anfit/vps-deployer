from __future__ import annotations

from dataclasses import dataclass
import re

from .models import ConfigError, Deployment
from .remote import RemoteHost


@dataclass(frozen=True)
class ExpectationResult:
    subject: str
    ok: bool
    detail: str


def _version(value: str) -> tuple[int, ...] | None:
    match = re.search(r"(?<![0-9])([0-9]+(?:\.[0-9]+)*)(?![0-9])", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _satisfies(actual: tuple[int, ...], constraint: str) -> bool:
    match = re.fullmatch(r"(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)*)", constraint)
    if not match:
        return False
    expected = tuple(int(part) for part in match.group(2).split("."))
    length = max(len(actual), len(expected))
    left = actual + (0,) * (length - len(actual))
    right = expected + (0,) * (length - len(expected))
    return {">=": left >= right, "<=": left <= right, "==": left == right,
            ">": left > right, "<": left < right}[match.group(1)]


def evaluate_expectations(deployment: Deployment, remote: RemoteHost) -> list[ExpectationResult]:
    expected_commands = {item.name: item.version for item in deployment.expectations.commands}
    for command in ("systemctl", "tar"):
        expected_commands.setdefault(command, None)
    if deployment.healthcheck:
        expected_commands.setdefault("curl", None)
    if deployment.http_proxy:
        expected_commands.setdefault("nginx", None)

    results: list[ExpectationResult] = []
    if deployment.expectations.architecture:
        result = remote.run(["uname", "-m"], check=False)
        actual = result.stdout.strip() if result.returncode == 0 else "unavailable"
        expected = deployment.expectations.architecture
        results.append(ExpectationResult("architecture", actual == expected,
                                         f"{actual} {'matches' if actual == expected else 'does not match'} {expected}"))

    for command, constraint in sorted(expected_commands.items()):
        if command == "nginx" and deployment.http_proxy and constraint is None:
            available = remote.run(["nginx", "-t"], sudo=True, check=False).returncode == 0
            results.append(ExpectationResult("command nginx", available,
                                             "available and configuration valid" if available
                                             else "missing or configuration invalid"))
            continue
        presence = remote.run(["sh", "-c", 'command -v -- "$1" >/dev/null 2>&1',
                               "vps-deployer-expect", command], check=False)
        if presence.returncode:
            results.append(ExpectationResult(f"command {command}", False, "missing"))
            continue
        if constraint is None:
            results.append(ExpectationResult(f"command {command}", True, "available"))
            continue
        reported = remote.run([command, "--version"], check=False)
        output = (reported.stdout + "\n" + reported.stderr).strip()
        actual = _version(output) if reported.returncode == 0 else None
        ok = actual is not None and _satisfies(actual, constraint)
        rendered = ".".join(map(str, actual)) if actual is not None else "unparseable"
        results.append(ExpectationResult(f"command {command}", ok,
                                         f"version {rendered} {'satisfies' if ok else 'does not satisfy'} {constraint}"))

    for path in deployment.expectations.paths:
        exists = remote.expected_path_exists(path)
        results.append(ExpectationResult(f"path {path}", exists,
                                         "exists" if exists else "missing"))
    return results


def require_expectations(deployment: Deployment, remote: RemoteHost) -> list[ExpectationResult]:
    results = evaluate_expectations(deployment, remote)
    failures = [result for result in results if not result.ok]
    if failures:
        detail = "\n".join(f"- {result.subject}: {result.detail}" for result in failures)
        raise ConfigError(f"{deployment.name}: host expectations failed:\n{detail}")
    return results
