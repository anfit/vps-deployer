from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import Repository
from .deployment import Reconciler, git_metadata, release_id, select_rollback
from .models import ConfigError
from .remote import RemoteError, RemoteHost
from .systemd import timer_name, unit_name


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vps-deployer")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="infra repository root")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate"); val.add_argument("deployment", nargs="?")
    host = sub.add_parser("host"); hs = host.add_subparsers(dest="host_command", required=True)
    for name in ("inspect", "onboard"): hs.add_parser(name).add_argument("host")
    for name in ("plan", "apply", "status", "logs", "rollback", "remove"):
        cmd = sub.add_parser(name); cmd.add_argument("deployment")
        if name in ("plan", "apply"): cmd.add_argument("--release-id")
        if name == "apply": cmd.add_argument("--allow-privileged", action="store_true")
        if name == "remove": cmd.add_argument("--allow-privileged", action="store_true")
        if name == "logs":
            cmd.add_argument("--lines", type=int, default=100); cmd.add_argument("--since"); cmd.add_argument("--follow", action="store_true")
        if name == "rollback": cmd.add_argument("--release")
    return p


def _repo(args) -> Repository:
    return Repository.load(args.repo)


def _dep(repo, name):
    if name not in repo.deployments: raise ConfigError(f"unknown deployment: {name}")
    return repo.deployments[name]


def _remote(repo, dep, verbose): return RemoteHost(repo.hosts[dep.host], verbose)


def run(args) -> int:
    repo = _repo(args)
    if args.command == "validate":
        if args.deployment: _dep(repo, args.deployment)
        print(f"Valid: {args.deployment or f'{len(repo.hosts)} host(s), {len(repo.deployments)} deployment(s)'}"); return 0
    if args.command == "host":
        if args.host not in repo.hosts: raise ConfigError(f"unknown host: {args.host}")
        host = repo.hosts[args.host]; remote = RemoteHost(host, args.verbose)
        if args.host_command == "inspect":
            checks = [(["uname", "-s"], "linux"), (["uname", "-m"], "architecture"),
                      (["systemctl", "--version"], "systemd"),
                      (["sh", "-c", "command -v tar curl systemctl"], "commands")]
            print(f"host: {host.name}\nssh: connected")
            for argv, label in checks:
                result = remote.run(argv, check=False); print(f"{label}: {result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else 'unavailable'}")
            privilege = remote.run(["test", "-e", host.managed_root], sudo=True, check=False)
            print(f"privileged access: {'available' if privilege.returncode in (0, 1) else 'unavailable'}")
            print(f"managed root: {'present' if remote.exists(host.managed_root, True) else 'absent'}"); return 0
        remote.run(["install", "-d", "-o", "root", "-g", "root", "-m", "0755", host.managed_root, "/etc/vps-deployer"], sudo=True)
        print(f"Onboarded {host.name}"); return 0
    dep = _dep(repo, args.deployment); remote = _remote(repo, dep, args.verbose)
    if args.command == "remove":
        rec = Reconciler(repo, dep, remote, "remove")
        actions = rec.remove(args.allow_privileged)
        print(f"Deployment: {dep.name}\nHost: {dep.host}\n")
        print("\n".join(map(str, actions)) if actions else "No changes.")
        return 0
    if args.command in {"plan", "apply"}:
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
        active = rec.active_release(); service = remote.run(["systemctl", "is-active", supervisor], sudo=True, check=False)
        desired = release_id(dep)
        manifest = rec.release_manifest() if active else {}
        metadata_source = dep.source if dep.source.is_dir() else dep.source.parent
        desired_commit, _ = git_metadata(metadata_source, desired)
        manifest_ok = manifest.get("release.id") == desired and (not desired_commit or manifest.get("commit.hash") == desired_commit)
        healthy = service.returncode == 0 and rec._healthy() and manifest_ok
        health = "healthy" if healthy else "unhealthy"
        supervisor_label = "timer" if dep.timer else "service"
        print(f"deployment: {dep.name}\nhost: {dep.host}\n{supervisor_label}: {service.stdout.strip() or 'unknown'}\n"
              f"desired release: {desired}\nactive release: {active or 'none'}\nhealth: {health}\nuser: {dep.user}")
        print(f"desired commit: {desired_commit or 'unversioned'}\nactive commit: {manifest.get('commit.hash', 'missing')}\n"
              f"manifest: {'current' if manifest_ok else 'stale or missing'}")
        if dep.timer:
            timer_status = rec.timer_status()
            print(f"last job: {timer_status.get('ExecMainExitTimestamp') or 'never'}\n"
                  f"last result: {timer_status.get('Result', 'unknown')}\n"
                  f"last exit status: {timer_status.get('ExecMainStatus', 'unknown')}")
        if service.returncode: print(remote.run(["systemctl", "status", supervisor, "--no-pager", "--lines=20"], sudo=True, check=False).stdout)
        return 0 if healthy else 1
    if args.command == "logs":
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
    remote.run(["systemctl", "restart", supervisor], sudo=True)
    print(f"Rolled back {dep.name} to {target}"); return 0


def main(argv=None) -> None:
    try: raise SystemExit(run(parser().parse_args(argv)))
    except (ConfigError, RemoteError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr); raise SystemExit(2)


if __name__ == "__main__": main()
