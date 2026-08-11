from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import tarfile
import tempfile
import fnmatch
import re
import subprocess
from datetime import datetime, timezone

from .config import Repository
from .models import ConfigError, Deployment, Host
from .remote import RemoteError, RemoteHost
from .systemd import render_timer, render_unit, timer_name, unit_name
from .nginx import nginx_name, render_proxy


@dataclass(frozen=True)
class Action:
    verb: str
    subject: str

    def __str__(self) -> str:
        return f"{self.verb} {self.subject}"


def git_metadata(source: Path, release: str, build_time: datetime | None = None) -> tuple[str | None, str]:
    def git(*args: str) -> str:
        result = subprocess.run(["git", "-C", str(source), *args], text=True, capture_output=True)
        if result.returncode:
            raise ConfigError(f"could not read Git metadata from {source}")
        return result.stdout.strip()
    try:
        commit = git("rev-parse", "HEAD")
        committed = git("show", "-s", "--format=%cI", commit)
        branch = git("branch", "--show-current") or "detached"
    except (ConfigError, FileNotFoundError):
        commit = None; committed = ""; branch = "unversioned"
    built = (build_time or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = f"release.id={release}\n"
    if commit:
        content += f"commit.hash={commit}\ncommit.timestamp={committed}\n"
    content += (f"build.timestamp={built}\nbuild.branch={branch}\n"
                f"build.user=vps-deployer\n")
    return commit, content


def content_hash(source: Path, includes=()) -> str:
    if not source.exists():
        raise ConfigError(f"artifact does not exist: {source}")
    digest = hashlib.sha256()
    paths = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file() and not _ignored(source, p))
    for path in paths:
        if source.is_dir():
            digest.update(path.relative_to(source).as_posix().encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    for include in sorted(includes, key=lambda item: item.target):
        if not include.source.is_file():
            raise ConfigError(f"release include does not exist or is not a file: {include.source}")
        digest.update(include.target.encode())
        digest.update(include.source.read_bytes())
    metadata_source = source if source.is_dir() else source.parent
    commit, _ = git_metadata(metadata_source, "pending")
    if commit:
        digest.update(b"git-commit")
        digest.update(commit.encode())
    return digest.hexdigest()[:7]


def _ignored(root: Path, path: Path) -> bool:
    ignored = {".git", ".venv", ".idea", "__pycache__", ".pytest_cache", ".env", "config.yaml"}
    relative = path.relative_to(root)
    if any(part in ignored or "venv" in part.lower() or part.startswith(".test-") or
           part.endswith((".egg-info", ".key", ".secret")) for part in relative.parts):
        return True
    ignore_file = root / ".vps-deployer-ignore"
    if ignore_file.is_file() and path != ignore_file:
        patterns = (line.strip() for line in ignore_file.read_text(encoding="utf-8").splitlines())
        return any(pattern and not pattern.startswith("#") and
                   (fnmatch.fnmatch(relative.as_posix(), pattern) or
                    fnmatch.fnmatch(relative.as_posix(), pattern.rstrip("/") + "/*"))
                   for pattern in patterns)
    return False


def release_id(dep: Deployment, explicit: str | None = None) -> str:
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", explicit):
            raise ConfigError("invalid release id")
        return explicit
    # Source-state-addressed defaults make repeated apply operations idempotent.
    return content_hash(dep.source, dep.includes)


def validate_archive(path: Path) -> None:
    """Reject tar entries that cannot be extracted safely into a release."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name
                target = PurePosixPath(name)
                if (not name or target.is_absolute() or ".." in target.parts or
                        any(ord(char) < 32 or ord(char) == 127 for char in name)):
                    raise ConfigError(f"unsafe archive member path: {name!r}")
                if not (member.isfile() or member.isdir()):
                    raise ConfigError(f"unsupported archive member type: {name!r}")
    except (tarfile.TarError, OSError) as exc:
        raise ConfigError(f"invalid release archive: {path}") from exc


def env_file(values: dict[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in sorted(values.items()))


def env_matches(current: str | None, expected: dict[str, str], secret_keys: set[str], secrets_resolved: bool) -> bool:
    if current is None:
        return False
    if secrets_resolved or not secret_keys:
        return current == env_file(expected)
    current_lines = {line.split("=", 1)[0]: line + "\n" for line in current.splitlines() if "=" in line}
    if set(current_lines) != set(expected):
        return False
    rendered = {key: env_file({key: value}) for key, value in expected.items() if key not in secret_keys}
    return all(current_lines[key] == line for key, line in rendered.items())


def releases_to_prune(releases: list[str], current: str, previous: str) -> list[str]:
    """Return valid release directories older than the rollback boundary."""
    valid = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return sorted({release for release in releases
                   if valid.fullmatch(release) and release not in {current, previous}})


class Reconciler:
    def __init__(self, repo: Repository, dep: Deployment, remote: RemoteHost, release: str):
        self.repo, self.dep, self.remote, self.release = repo, dep, remote, release
        self.host: Host = repo.hosts[dep.host]
        self.base = f"{self.host.managed_root}/{dep.name}"
        self.release_path = f"{self.base}/releases/{release}"
        self.unit_path = f"/etc/systemd/system/{unit_name(dep)}"
        self.timer_path = f"/etc/systemd/system/{timer_name(dep)}"
        self.env_path = f"/etc/vps-deployer/{dep.name}.env"
        self.state_path = f"/etc/vps-deployer/{dep.name}.state"
        self.proxy_available = f"/etc/nginx/sites-available/{nginx_name(dep)}" if dep.http_proxy else None
        self.proxy_enabled = f"/etc/nginx/sites-enabled/{nginx_name(dep)}" if dep.http_proxy else None

    def plan(self, require_secrets: bool = False) -> list[Action]:
        values, secret_keys = self.repo.resolve_environment(self.dep, require_secrets)
        actions: list[Action] = []
        if self.remote.run(["id", "-u", self.dep.user], check=False).returncode:
            actions.append(Action("CREATE", f"user {self.dep.user}"))
        for path in [self.base, *[s.path for s in self.dep.storage]]:
            if not self.remote.exists(path, sudo=True):
                actions.append(Action("CREATE", path))
        env_changed = not env_matches(self.remote.read(self.env_path, sudo=True), values, secret_keys, require_secrets)
        unit_changed = self.remote.read(self.unit_path, sudo=True) != render_unit(self.dep, self.host.managed_root)
        timer_changed = bool(self.dep.timer and self.remote.read(self.timer_path, sudo=True) != render_timer(self.dep))
        managed = self.managed_resources()
        if env_changed:
            actions.append(Action("UPDATE", "environment file"))
        if unit_changed:
            actions.append(Action("UPDATE", "systemd unit"))
        if timer_changed:
            actions.append(Action("UPDATE", "systemd timer"))
        if managed.get("timer") and not self.dep.timer:
            actions.append(Action("REMOVE", "obsolete systemd timer"))
        desired_proxy = nginx_name(self.dep) if self.dep.http_proxy else None
        if managed.get("proxy") and managed.get("proxy") != desired_proxy:
            actions.append(Action("REMOVE", f"obsolete HTTP proxy {managed['proxy']}"))
        if self.dep.http_proxy and (self.remote.read(str(self.proxy_available), sudo=True) != render_proxy(self.dep) or
                                    not self.remote.exists(str(self.proxy_enabled), sudo=True)):
            actions.append(Action("UPDATE", f"HTTP proxy {self.dep.http_proxy.domain}"))
        if not self.remote.exists(self.release_path, sudo=True):
            actions.append(Action("INSTALL", f"release {self.release}"))
        active = self.active_release()
        if active != self.release:
            actions.extend([Action("ACTIVATE", f"release {self.release}"),
                            Action("RESTART", "timer" if self.dep.timer else "service")])
        elif env_changed or unit_changed or timer_changed:
            actions.append(Action("RESTART", "timer" if self.dep.timer else "service"))
        if managed != self.desired_resources:
            actions.append(Action("UPDATE", "managed resource metadata"))
        return actions

    @property
    def desired_resources(self) -> dict[str, object]:
        return {"timer": bool(self.dep.timer),
                "proxy": nginx_name(self.dep) if self.dep.http_proxy else None}

    def managed_resources(self) -> dict[str, object]:
        content = self.remote.read(self.state_path, sudo=True)
        if content is None:
            return {"timer": False, "proxy": None}
        try:
            state = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.dep.name}: invalid managed resource metadata") from exc
        if (not isinstance(state, dict) or not isinstance(state.get("timer"), bool) or
                (state.get("proxy") is not None and not isinstance(state.get("proxy"), str))):
            raise ConfigError(f"{self.dep.name}: invalid managed resource metadata")
        return {"timer": state["timer"], "proxy": state.get("proxy")}

    def active_release(self) -> str | None:
        result = self.remote.run(["readlink", f"{self.base}/current"], sudo=True, check=False)
        return Path(result.stdout.strip()).name if result.returncode == 0 else None

    def previous_release(self) -> str | None:
        result = self.remote.run(["readlink", f"{self.base}/previous"], sudo=True, check=False)
        return Path(result.stdout.strip()).name if result.returncode == 0 else None

    def _write_privileged(self, path: str, content: str, mode: str, owner: str) -> None:
        user, group = owner.split(":")
        self.remote.write_file(path, content.encode(), mode, user, group)

    def _restore_configuration(self, environment: str | None, unit: str | None,
                               timer: str | None) -> None:
        files = (
            (self.env_path, environment, "0640", f"root:{self.dep.user}"),
            (self.unit_path, unit, "0644", "root:root"),
            (self.timer_path if self.dep.timer else None, timer, "0644", "root:root"),
        )
        for path, content, mode, owner in files:
            if path is None:
                continue
            if content is None:
                self.remote.run(["rm", "-f", path], sudo=True)
            else:
                self._write_privileged(path, content, mode, owner)
        self.remote.run(["systemctl", "daemon-reload"], sudo=True)

    def _fail_activation(self, previous: str | None, environment: str | None,
                         unit: str | None, timer: str | None) -> None:
        message = self._rollback_activation(previous, environment, unit, timer)
        raise RuntimeError(f"health check failed; {message}")

    def _rollback_activation(self, previous: str | None, environment: str | None,
                             unit: str | None, timer: str | None) -> str:
        if previous:
            self.remote.run(["ln", "-sfn", f"releases/{previous}", f"{self.base}/current"], sudo=True)
            self._restore_configuration(environment, unit, timer)
            self.remote.run(["systemctl", "restart", self.supervisor_name], sudo=True)
            return "previous release and configuration restored"
        self.remote.run(["systemctl", "disable", "--now", self.supervisor_name], sudo=True, check=False)
        self._restore_configuration(environment, unit, timer)
        return "first deployment stopped"

    def apply(self) -> list[Action]:
        if self.dep.privileged:
            raise ConfigError("privileged deployment requires explicit allow_privileged=True")
        with self.remote.deployment_lock(self.dep.name):
            return self._apply()

    def apply_privileged(self) -> list[Action]:
        with self.remote.deployment_lock(self.dep.name):
            return self._apply()

    def _apply(self) -> list[Action]:
        values, _ = self.repo.resolve_environment(self.dep, True)
        actions = self.plan(True)
        self.remote.run(["mkdir", "-p", self.host.managed_root, "/etc/vps-deployer", f"{self.base}/releases"], sudo=True)
        if self.dep.user != "root" and self.remote.run(["id", "-u", self.dep.user], check=False).returncode:
            self.remote.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", self.dep.user], sudo=True)
        for store in self.dep.storage:
            self.remote.run(["install", "-d", "-o", self.dep.user, "-g", self.dep.user, "-m", "0750", store.path], sudo=True)
        new_release = not self.remote.exists(self.release_path, sudo=True)
        if new_release:
            self._install_release()
        old_env = self.remote.read(self.env_path, sudo=True)
        old_unit = self.remote.read(self.unit_path, sudo=True)
        old_timer = self.remote.read(str(self.timer_path), sudo=True) if self.dep.timer else None
        old_resources = self.managed_resources()
        new_env, new_unit = env_file(values), render_unit(self.dep, self.host.managed_root)
        new_timer = render_timer(self.dep) if self.dep.timer else None
        if old_env != new_env:
            self._write_privileged(self.env_path, new_env, "0640", f"root:{self.dep.user}")
        units_changed = old_unit != new_unit or old_timer != new_timer
        if old_unit != new_unit:
            self._write_privileged(self.unit_path, new_unit, "0644", "root:root")
        if self.dep.timer and old_timer != new_timer:
            self._write_privileged(self.timer_path, str(new_timer), "0644", "root:root")
        if units_changed:
            self.remote.run(["systemctl", "daemon-reload"], sudo=True)
            self.remote.run(["systemctl", "enable", self.supervisor_name], sudo=True)
        previous = self.active_release()
        if previous != self.release:
            self.remote.run(["ln", "-sfn", f"releases/{self.release}", f"{self.base}/current"], sudo=True)
            self.remote.run(["systemctl", "restart", self.supervisor_name], sudo=True)
            if not self._healthy():
                self._fail_activation(previous, old_env, old_unit, old_timer)
        elif old_env != new_env or units_changed:
            self.remote.run(["systemctl", "restart", self.supervisor_name], sudo=True)
            if not self._healthy():
                self._restore_configuration(old_env, old_unit, old_timer)
                self.remote.run(["systemctl", "restart", self.supervisor_name], sudo=True)
                raise RuntimeError("health check failed; previous configuration restored")
        try:
            if self.dep.http_proxy:
                self._reconcile_proxy()
            self._reconcile_obsolete_resources(old_resources)
        except (RemoteError, RuntimeError) as exc:
            if previous != self.release:
                outcome = self._rollback_activation(previous, old_env, old_unit, old_timer)
                raise RuntimeError(f"proxy reconciliation failed; {outcome}") from exc
            if old_env != new_env or units_changed:
                self._restore_configuration(old_env, old_unit, old_timer)
                self.remote.run(["systemctl", "restart", self.supervisor_name], sudo=True)
            raise RuntimeError("proxy reconciliation failed; previous configuration restored") from exc
        desired_state = json.dumps(self.desired_resources, sort_keys=True, separators=(",", ":")) + "\n"
        if self.remote.read(self.state_path, sudo=True) != desired_state:
            self._write_privileged(self.state_path, desired_state, "0644", "root:root")
        # Prune only after the whole activation (including proxy reconciliation)
        # succeeded. The prior active release is retained as the rollback target.
        if previous and previous != self.release:
            self.remote.run(["ln", "-sfn", f"releases/{previous}", f"{self.base}/previous"], sudo=True)
            self._prune_releases(previous)
        return actions

    def _reconcile_proxy(self) -> None:
        desired = render_proxy(self.dep)
        enabled_exists = self.remote.exists(str(self.proxy_enabled), sudo=True)
        if self.remote.read(str(self.proxy_available), sudo=True) == desired and enabled_exists:
            return
        candidate_name = f"vps-candidate-{hashlib.sha256(self.dep.name.encode()).hexdigest()[:16]}"
        candidate = f"/etc/nginx/sites-available/{candidate_name}"
        with self.remote.deployment_lock("nginx"):
            self._write_privileged(candidate, desired, "0644", "root:root")
            try:
                self.remote.run(["ln", "-sfn", candidate, str(self.proxy_enabled)], sudo=True)
                self.remote.run(["nginx", "-t"], sudo=True)
                self._write_privileged(str(self.proxy_available), desired, "0644", "root:root")
                self.remote.run(["ln", "-sfn", str(self.proxy_available), str(self.proxy_enabled)], sudo=True)
                self.remote.run(["rm", "-f", candidate], sudo=True)
                self.remote.run(["systemctl", "reload", "nginx"], sudo=True)
            except (RemoteError, RuntimeError):
                if enabled_exists:
                    self.remote.run(["ln", "-sfn", str(self.proxy_available), str(self.proxy_enabled)], sudo=True)
                else:
                    self.remote.run(["rm", "-f", str(self.proxy_enabled)], sudo=True)
                self.remote.run(["rm", "-f", candidate], sudo=True)
                raise

    def _reconcile_obsolete_resources(self, previous: dict[str, object]) -> None:
        if previous.get("timer") and not self.dep.timer:
            self.remote.run(["systemctl", "disable", "--now", timer_name(self.dep)], sudo=True, check=False)
            self.remote.run(["rm", "-f", self.timer_path], sudo=True)
            self.remote.run(["systemctl", "daemon-reload"], sudo=True)
        elif not previous.get("timer") and self.dep.timer:
            self.remote.run(["systemctl", "disable", unit_name(self.dep)], sudo=True, check=False)
        old_proxy = previous.get("proxy")
        desired_proxy = nginx_name(self.dep) if self.dep.http_proxy else None
        if isinstance(old_proxy, str) and old_proxy != desired_proxy:
            self._remove_proxy(old_proxy)

    def _remove_proxy(self, proxy: str) -> None:
        available = f"/etc/nginx/sites-available/{proxy}"
        enabled = f"/etc/nginx/sites-enabled/{proxy}"
        with self.remote.deployment_lock("nginx"):
            self.remote.run(["rm", "-f", enabled], sudo=True)
            try:
                self.remote.run(["nginx", "-t"], sudo=True)
            except RemoteError:
                self.remote.run(["ln", "-sfn", available, enabled], sudo=True)
                raise
            self.remote.run(["rm", "-f", available], sudo=True)
            self.remote.run(["systemctl", "reload", "nginx"], sudo=True)

    def _prune_releases(self, previous: str) -> None:
        releases_path = f"{self.base}/releases"
        listing = self.remote.run(["find", releases_path, "-mindepth", "1", "-maxdepth", "1",
                                   "-type", "d", "-print"], sudo=True)
        releases = [Path(path).name for path in listing.stdout.splitlines()]
        for release in releases_to_prune(releases, self.release, previous):
            self.remote.run(["rm", "-rf", f"{releases_path}/{release}"], sudo=True)

    def _install_release(self) -> None:
        upload = f"/tmp/vps-deployer-{self.dep.name}-{self.release}.tar.gz"
        archive: Path
        cleanup = None
        if self.dep.source.is_dir():
            temp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            temp.close(); archive = Path(temp.name); cleanup = archive
            with tarfile.open(archive, "w:gz") as tar:
                for item in self.dep.source.iterdir():
                    if item.relative_to(self.dep.source).as_posix() == "build.properties":
                        continue
                    if not _ignored(self.dep.source, item):
                        tar.add(item, arcname=item.name, filter=lambda info: None if _ignored(
                            self.dep.source, self.dep.source / Path(info.name)) else info)
                for include in self.dep.includes:
                    tar.add(include.source, arcname=include.target)
        else:
            if self.dep.includes:
                raise ConfigError("release includes require a directory artifact source")
            temp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            temp.close(); archive = Path(temp.name); cleanup = archive
            shutil.copyfile(self.dep.source, archive)
        try:
            validate_archive(archive)
            self.remote.upload(archive, upload)
            self.remote.run(["mkdir", "-p", self.release_path], sudo=True)
            self.remote.run(["tar", "-xzf", upload, "-C", self.release_path], sudo=True)
            self.remote.run(["chown", "-R", f"root:{self.dep.user}", self.release_path], sudo=True)
            self.remote.run(["chmod", "-R", "u=rwX,g=rX,o=", self.release_path], sudo=True)
            metadata_source = self.dep.source if self.dep.source.is_dir() else self.dep.source.parent
            _, metadata = git_metadata(metadata_source, self.release)
            self._write_privileged(f"{self.release_path}/build.properties", metadata, "0640", f"root:{self.dep.user}")
            executable = shlex.split(self.dep.command)[0][2:]
            if self.dep.working_directory != ".":
                executable = f"{self.dep.working_directory}/{executable}"
            self.remote.run(["chmod", "0750", f"{self.release_path}/{executable}"], sudo=True)
            self.remote.run(["rm", "-f", upload])
        finally:
            if cleanup: cleanup.unlink(missing_ok=True)

    def release_manifest(self) -> dict[str, str]:
        content = self.remote.read(f"{self.base}/current/build.properties", sudo=True)
        return dict(line.split("=", 1) for line in content.splitlines() if "=" in line)

    @property
    def supervisor_name(self) -> str:
        return timer_name(self.dep) if self.dep.timer else unit_name(self.dep)

    def _healthy(self) -> bool:
        hc = self.dep.healthcheck
        if self.dep.timer:
            return self.remote.run(["systemctl", "is-active", "--quiet", timer_name(self.dep)],
                                   sudo=True, check=False).returncode == 0
        if not hc:
            return self.remote.run(["systemctl", "is-active", "--quiet", unit_name(self.dep)], sudo=True, check=False).returncode == 0
        if hc.get("type") != "http" or not hc.get("url"):
            raise ConfigError("only http health checks are supported")
        return self.remote.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                                "--retry", "5", "--retry-delay", "2", "--retry-connrefused", str(hc["url"])], check=False).returncode == 0

    def remove(self, allow_privileged: bool = False) -> list[Action]:
        if self.dep.privileged and not allow_privileged:
            raise ConfigError("privileged deployment removal requires --allow-privileged")
        actions: list[Action] = []
        managed = self.managed_resources()
        if (managed.get("timer") or self.dep.timer) and self.remote.exists(self.timer_path, sudo=True):
            actions.append(Action("REMOVE", "systemd timer"))
            self.remote.run(["systemctl", "disable", "--now", timer_name(self.dep)], sudo=True, check=False)
            self.remote.run(["rm", "-f", self.timer_path], sudo=True)
        if self.remote.exists(self.unit_path, sudo=True):
            actions.append(Action("REMOVE", "systemd unit"))
            self.remote.run(["systemctl", "disable", "--now", unit_name(self.dep)], sudo=True, check=False)
            self.remote.run(["rm", "-f", self.unit_path], sudo=True)
            self.remote.run(["systemctl", "daemon-reload"], sudo=True)
            self.remote.run(["systemctl", "reset-failed"], sudo=True, check=False)
        if self.remote.exists(self.env_path, sudo=True):
            actions.append(Action("REMOVE", "environment file"))
            self.remote.run(["rm", "-f", self.env_path], sudo=True)
        proxy = managed.get("proxy") or (nginx_name(self.dep) if self.dep.http_proxy else None)
        if isinstance(proxy, str):
            actions.append(Action("REMOVE", f"HTTP proxy {proxy}"))
            self._remove_proxy(proxy)
        if self.remote.exists(self.state_path, sudo=True):
            self.remote.run(["rm", "-f", self.state_path], sudo=True)
        # The active and rollback releases, writable storage, and service users are retained.
        return actions


def select_rollback(releases: list[str], current: str, previous: str | None,
                    requested: str | None = None) -> str:
    available = set(releases)
    target = requested or previous
    if target is None:
        raise ConfigError("no previous release available")
    if target not in available or target == current:
        raise ConfigError("requested rollback release is unavailable or already active")
    return target
