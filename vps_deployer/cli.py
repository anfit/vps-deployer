from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path
import sys

from . import __version__
from . import bootstrap
from .config import Repository
from .deployment import Reconciler, git_metadata, release_id, select_rollback
from .models import ConfigError
from .remote import RemoteError, RemoteHost
from .systemd import timer_name, unit_name
from .expectations import evaluate_expectations
from .doctor import ERROR, WARN, diagnose
from .scaffold import init_app, init_infra


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vps-deployer")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--repo", type=Path, default=None,
                   help="infra repository root (default: current directory if it holds hosts/ and "
                        "deployments/, else $VPS_DEPLOYER_REPO, else $SERVICE_INFRA_DIR)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate"); val.add_argument("deployment", nargs="?")
    sec = sub.add_parser("secrets", help="inspect secret environment references")
    ss = sec.add_subparsers(dest="secrets_command", required=True)
    ss.add_parser("check", help="list unset secret variables by name").add_argument("deployment", nargs="?")
    host = sub.add_parser("host"); hs = host.add_subparsers(dest="host_command", required=True)
    for name in ("inspect", "onboard"): hs.add_parser(name).add_argument("host")
    hb = hs.add_parser("bootstrap", help="print (or --install) the gate setup for a host")
    hb.add_argument("host"); hb.add_argument("--install", action="store_true",
                                             help="copy the gate to the host over sudo SSH and onboard it")
    ini = sub.add_parser("init", help="scaffold an infra repository, or `init app` for an application")
    ini.add_argument("target", nargs="*", help="[DIR] or: app [DIR]")
    doc = sub.add_parser("doctor", help="offline preflight of an application's .deployer contract")
    doc.add_argument("app", nargs="?", type=Path, default=Path("."))
    lst = sub.add_parser("list", help="list deployments from the manifests (offline)")
    lst.add_argument("--format", choices=("table", "markdown"), default="table")
    for name in ("check", "plan", "apply", "status", "logs", "rollback", "remove"):
        cmd = sub.add_parser(name)
        if name in ("plan", "status"):
            cmd.add_argument("deployment", nargs="?")
            cmd.add_argument("--all", action="store_true", help="every deployment")
        else:
            cmd.add_argument("deployment")
        if name == "plan":
            cmd.add_argument("--exit-code", action="store_true", help="exit 1 when any change is planned")
        if name in ("plan", "apply"): cmd.add_argument("--release-id")
        if name == "apply": cmd.add_argument("--allow-privileged", action="store_true")
        if name == "remove": cmd.add_argument("--allow-privileged", action="store_true")
        if name == "logs":
            cmd.add_argument("--lines", type=int, default=100); cmd.add_argument("--since"); cmd.add_argument("--follow", action="store_true")
        if name == "rollback": cmd.add_argument("--release")
    return p


def resolve_repo_path(explicit: Path | None, cwd: Path | None = None, environ=None) -> Path:
    """Pick the infra repository: --repo, then cwd if it looks like one, then environment."""
    if explicit is not None: return explicit
    environ = os.environ if environ is None else environ
    cwd = Path.cwd() if cwd is None else cwd
    if (cwd / "hosts").is_dir() and (cwd / "deployments").is_dir(): return cwd
    for name in ("VPS_DEPLOYER_REPO", "SERVICE_INFRA_DIR"):
        if environ.get(name): return Path(environ[name])
    return cwd


def _repo(args) -> Repository:
    return Repository.load(resolve_repo_path(args.repo))


def _missing_secrets_message(dep, missing) -> str:
    names = ", ".join(sorted({name for _, name in missing}))
    return f"{dep.name}: secret environment variables not set: {names}"


def _dep(repo, name):
    if name not in repo.deployments: raise ConfigError(f"unknown deployment: {name}")
    return repo.deployments[name]


def _remote(repo, dep, verbose): return RemoteHost(repo.hosts[dep.host], verbose)


def _kind(dep) -> str:
    return "command" if dep.executable else ("timer" if dep.timer else "service")


def _ingress(dep) -> str:
    if dep.http_proxy: return dep.http_proxy.domain
    if dep.healthcheck: return dep.healthcheck["url"].split("/", 3)[2]
    return "-"


def _table(header, rows) -> str:
    widths = [max(len(x) for x in col) for col in zip(header, *rows)] if rows else [len(h) for h in header]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(header)] + [line(r) for r in rows])


def render_listing(repo, fmt: str) -> str:
    deps = _sorted_deployments(repo)
    if fmt == "markdown":
        header = ("deployment", "host", "kind", "user", "ingress", "storage", "secret variables")
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for d in deps:
            storage = ", ".join(f"`{s.path}`" for s in d.storage) or "none"
            secrets = ", ".join(f"`{ref.from_env}`" for ref in d.secrets.values()) or "none"
            cells = (f"`{d.name}`", f"`{d.host}`", _kind(d), f"`{d.user}`", _ingress(d), storage, secrets)
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)
    rows = [(d.name, d.host, _kind(d), d.user, _ingress(d), str(len(d.secrets))) for d in deps]
    return _table(("deployment", "host", "kind", "user", "ingress", "secrets"), rows)


def _status_info(repo, dep, remote) -> dict:
    unit = unit_name(dep); supervisor = timer_name(dep) if dep.timer else unit
    rec = Reconciler(repo, dep, remote, "status")
    active = rec.active_release()
    service = (None if dep.executable else remote.run(["systemctl", "is-active", supervisor], sudo=True, check=False))
    desired = release_id(dep)
    manifest = rec.release_manifest() if active else {}
    metadata_source = dep.source if dep.source.is_dir() else dep.source.parent
    desired_commit, _ = git_metadata(metadata_source, desired)
    manifest_ok = manifest.get("deployment.release") == desired and (not desired_commit or manifest.get("deployment.commit") == desired_commit)
    healthy = (dep.executable is not None or service.returncode == 0) and rec._healthy() and manifest_ok
    return dict(active=active, service=service, desired=desired, manifest=manifest,
                desired_commit=desired_commit, manifest_ok=manifest_ok, healthy=healthy)


def _sorted_deployments(repo):
    return sorted(repo.deployments.values(), key=lambda d: (d.host, d.name))


def _status_all(repo, args) -> int:
    rows, failed = [], False
    for dep in _sorted_deployments(repo):
        try:
            info = _status_info(repo, dep, _remote(repo, dep, args.verbose))
            commit = (info["manifest"].get("deployment.commit") or "-")[:8]
            rows.append((dep.name, dep.host, _kind(dep), "healthy" if info["healthy"] else "unhealthy",
                         str(info["active"] or "none")[:16], commit))
            failed |= not info["healthy"]
        except (ConfigError, RemoteError, RuntimeError) as exc:
            rows.append((dep.name, dep.host, _kind(dep), "error", "-", str(exc).splitlines()[0][:60])); failed = True
    print(_table(("deployment", "host", "kind", "health", "active release", "commit"), rows))
    return 1 if failed else 0


def _plan_all(repo, args) -> int:
    changed = errors = 0
    for dep in _sorted_deployments(repo):
        print(f"== {dep.name} ({dep.host})")
        try:
            missing = repo.missing_secrets(dep)
            if missing: print(f"warning: {_missing_secrets_message(dep, missing)}", file=sys.stderr)
            actions = Reconciler(repo, dep, _remote(repo, dep, args.verbose), release_id(dep, None)).plan(False)
            print("\n".join(map(str, actions)) if actions else "No changes."); changed += bool(actions)
        except (ConfigError, RemoteError, RuntimeError) as exc:
            print(f"error: {exc}"); errors += 1
    print(f"\n{len(repo.deployments)} deployment(s): {changed} with changes, {errors} error(s)")
    return 2 if errors else (1 if changed and args.exit_code else 0)


def run(args) -> int:
    if args.command == "init":
        target = list(args.target)
        if target and target[0] == "app":
            actions = init_app(Path(target[1]) if len(target) > 1 else Path.cwd())
        else:
            actions = init_infra(Path(target[0]) if target else Path.cwd())
        print("\n".join(actions)); return 0
    if args.command == "doctor":
        try: doctor_repo = Repository.load(resolve_repo_path(args.repo))
        except (ConfigError, OSError, KeyError): doctor_repo = None
        findings = diagnose(args.app, doctor_repo)
        for f in findings:
            print(f"{f.level.upper():5} {f.subject}: {f.detail}")
        errors = sum(f.level == ERROR for f in findings); warns = sum(f.level == WARN for f in findings)
        print(f"\n{errors} error(s), {warns} warning(s)"); return 1 if errors else 0
    repo = _repo(args)
    if args.command == "list":
        print(render_listing(repo, args.format)); return 0
    if args.command in {"plan", "status"}:
        if args.all and args.deployment: raise ConfigError(f"{args.command}: give a deployment or --all, not both")
        if args.all: return _status_all(repo, args) if args.command == "status" else _plan_all(repo, args)
        if not args.deployment: raise ConfigError(f"{args.command}: a deployment name or --all is required")
    if args.command == "validate":
        if args.deployment: _dep(repo, args.deployment)
        print(f"Valid: {args.deployment or f'{len(repo.hosts)} host(s), {len(repo.deployments)} deployment(s)'}"); return 0
    if args.command == "secrets":
        targets = [_dep(repo, args.deployment)] if args.deployment else list(repo.deployments.values())
        status = 0
        for dep in targets:
            missing = repo.missing_secrets(dep)
            if missing:
                status = 1; print(_missing_secrets_message(dep, missing))
            elif dep.secrets:
                print(f"{dep.name}: all {len(dep.secrets)} secret variable(s) set")
        return status
    if args.command == "host":
        if args.host not in repo.hosts: raise ConfigError(f"unknown host: {args.host}")
        host = repo.hosts[args.host]; remote = RemoteHost(host, args.verbose)
        if args.host_command == "bootstrap":
            storage = bootstrap.storage_paths(repo, host.name)
            print(bootstrap.render_instructions(host, storage))
            if args.install:
                sudo_host = dataclasses.replace(host, ssh=dataclasses.replace(host.ssh, privileged_host=None))
                direct = RemoteHost(sudo_host, args.verbose)
                try:
                    direct.run(["install", "-d", "-o", "root", "-g", "root", "-m", "0755", host.managed_root, "/etc/vps-deployer"], sudo=True)
                    direct.write_file(bootstrap.GATE_INSTALL_PATH, bootstrap.gate_source(), "0755", "root", "root")
                except RemoteError as exc:
                    print(f"\nerror: {exc}\n{bootstrap.manual_install_hint(host)}", file=sys.stderr); return 1
                print(f"\nInstalled {bootstrap.GATE_INSTALL_PATH} and onboarded {host.name}")
            return 0
        if args.host_command == "inspect":
            checks = [(["uname", "-s"], "linux"), (["uname", "-m"], "architecture"),
                      (["systemctl", "--version"], "systemd"),
                      (["sh", "-c", "command -v tar curl systemctl"], "commands")]
            print(f"host: {host.name}\nssh: connected")
            for argv, label in checks:
                result = remote.run(argv, check=False); print(f"{label}: {result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else 'unavailable'}")
            privilege = remote.run(["test", "-e", host.managed_root], sudo=True, check=False)
            print(f"privileged access: {'available' if privilege.returncode in (0, 1) else 'unavailable'}")
            print(f"managed root: {'present' if remote.exists(host.managed_root, True) else 'absent'}")
            state, detail = remote.gate_status(); print(f"gate: {state} ({detail})")
            return 1 if state in ("mismatch", "outdated") else 0
        remote.run(["install", "-d", "-o", "root", "-g", "root", "-m", "0755", host.managed_root, "/etc/vps-deployer"], sudo=True)
        print(f"Onboarded {host.name}"); return 0
    dep = _dep(repo, args.deployment); remote = _remote(repo, dep, args.verbose)
    if args.command == "check":
        results = evaluate_expectations(dep, remote)
        print(f"Deployment: {dep.name}\nHost: {dep.host}\n\nHOST EXPECTATIONS")
        for result in results:
            print(f"  {'OK' if result.ok else 'FAIL'} {result.subject}: {result.detail}")
        return 0 if all(result.ok for result in results) else 1
    if args.command == "remove":
        rec = Reconciler(repo, dep, remote, "remove")
        actions = rec.remove(args.allow_privileged)
        print(f"Deployment: {dep.name}\nHost: {dep.host}\n")
        print("\n".join(map(str, actions)) if actions else "No changes.")
        return 0
    if args.command in {"plan", "apply"}:
        missing = repo.missing_secrets(dep)
        if missing and args.command == "apply": raise ConfigError(_missing_secrets_message(dep, missing))
        if missing: print(f"warning: {_missing_secrets_message(dep, missing)}; plan shows them as redacted", file=sys.stderr)
        rid = release_id(dep, args.release_id); reconciler = Reconciler(repo, dep, remote, rid)
        if args.command == "plan":
            actions = reconciler.plan(False)
        elif dep.privileged:
            if not args.allow_privileged:
                raise ConfigError("privileged deployment requires --allow-privileged")
            actions = reconciler.apply_privileged()
        else:
            actions = reconciler.apply()
        print(f"Deployment: {dep.name}\nHost: {dep.host}\n")
        print("\n".join(map(str, actions)) if actions else "No changes."); return 0
    unit = unit_name(dep); supervisor = timer_name(dep) if dep.timer else unit; rec = Reconciler(repo, dep, remote, "status")
    if args.command == "status":
        info = _status_info(repo, dep, remote)
        active, service, desired, manifest = info["active"], info["service"], info["desired"], info["manifest"]
        desired_commit, manifest_ok, healthy = info["desired_commit"], info["manifest_ok"], info["healthy"]
        health = "healthy" if healthy else "unhealthy"
        supervisor_label = "executable" if dep.executable else ("timer" if dep.timer else "service")
        supervisor_status = f"/usr/local/bin/{dep.executable}" if dep.executable else (service.stdout.strip() or "unknown")
        print(f"deployment: {dep.name}\nhost: {dep.host}\n{supervisor_label}: {supervisor_status}\n"
              f"desired release: {desired}\nactive release: {active or 'none'}\nhealth: {health}\nuser: {dep.user}")
        print(f"desired commit: {desired_commit or 'unversioned'}\nactive commit: {manifest.get('deployment.commit', 'missing')}\n"
              f"manifest: {'current' if manifest_ok else 'stale or missing'}")
        if dep.timer:
            timer_status = rec.timer_status()
            print(f"last job: {timer_status.get('ExecMainExitTimestamp') or 'never'}\n"
                  f"last result: {timer_status.get('Result', 'unknown')}\n"
                  f"last exit status: {timer_status.get('ExecMainStatus', 'unknown')}")
        if service is not None and service.returncode: print(remote.run(["systemctl", "status", supervisor, "--no-pager", "--lines=20"], sudo=True, check=False).stdout)
        return 0 if healthy else 1
    if args.command == "logs":
        if dep.executable:
            raise ConfigError("command deployments do not have service logs")
        argv = ["journalctl", "-u", unit, "--no-pager", "-n", str(args.lines)]
        if args.since: argv += ["--since", args.since]
        if args.follow: argv += ["--follow"]
        result = remote.run(argv, sudo=True, check=False); print(result.stdout, end=""); return result.returncode
    listing = remote.run(["find", f"{repo.hosts[dep.host].managed_root}/{dep.name}/releases", "-mindepth", "1", "-maxdepth", "1", "-type", "d", "-print"], sudo=True)
    releases = [Path(path).name for path in listing.stdout.splitlines()]
    current = rec.active_release()
    target = select_rollback(releases, str(current), rec.previous_release(), args.release)
    remote.run(["ln", "-sfn", f"releases/{target}", f"{rec.base}/current"], sudo=True)
    if current:
        remote.run(["ln", "-sfn", f"releases/{current}", f"{rec.base}/previous"], sudo=True)
    if not dep.executable:
        remote.run(["systemctl", "restart", supervisor], sudo=True)
    print(f"Rolled back {dep.name} to {target}"); return 0


def main(argv=None) -> None:
    try: raise SystemExit(run(parser().parse_args(argv)))
    except (ConfigError, RemoteError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr); raise SystemExit(2)


if __name__ == "__main__": main()
