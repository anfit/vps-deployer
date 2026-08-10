from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shlex
import tarfile
import tempfile

from .config import Repository
from .models import ConfigError, Deployment, Host
from .remote import RemoteHost
from .systemd import render_unit, unit_name


@dataclass(frozen=True)
class Action:
    verb: str
    subject: str

    def __str__(self) -> str:
        return f"{self.verb} {self.subject}"


def content_hash(source: Path) -> str:
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
    return digest.hexdigest()[:7]


def _ignored(root: Path, path: Path) -> bool:
    ignored = {".git", ".venv", ".idea", "__pycache__", ".pytest_cache"}
    return any(part in ignored or part.endswith(".egg-info") for part in path.relative_to(root).parts)


def release_id(dep: Deployment, explicit: str | None = None) -> str:
    if explicit:
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", explicit):
            raise ConfigError("invalid release id")
        return explicit
    # Content-addressed defaults make repeated apply operations genuinely idempotent.
    return content_hash(dep.source)


def env_file(values: dict[str, str]) -> str:
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in sorted(values.items()))


class Reconciler:
    def __init__(self, repo: Repository, dep: Deployment, remote: RemoteHost, release: str):
        self.repo, self.dep, self.remote, self.release = repo, dep, remote, release
        self.host: Host = repo.hosts[dep.host]
        self.base = f"{self.host.managed_root}/{dep.name}"
        self.release_path = f"{self.base}/releases/{release}"
        self.unit_path = f"/etc/systemd/system/{unit_name(dep)}"
        self.env_path = f"/etc/vps-deployer/{dep.name}.env"

    def plan(self, require_secrets: bool = False) -> list[Action]:
        values, _ = self.repo.resolve_environment(self.dep, require_secrets)
        actions: list[Action] = []
        if self.remote.run(["id", "-u", self.dep.user], check=False).returncode:
            actions.append(Action("CREATE", f"user {self.dep.user}"))
        for path in [self.base, *[s.path for s in self.dep.storage]]:
            if not self.remote.exists(path, sudo=True):
                actions.append(Action("CREATE", path))
        env_changed = self.remote.read(self.env_path, sudo=True) != env_file(values)
        unit_changed = self.remote.read(self.unit_path, sudo=True) != render_unit(self.dep, self.host.managed_root)
        if env_changed:
            actions.append(Action("UPDATE", "environment file"))
        if unit_changed:
            actions.append(Action("UPDATE", "systemd unit"))
        if not self.remote.exists(self.release_path, sudo=True):
            actions.append(Action("INSTALL", f"release {self.release}"))
        active = self.active_release()
        if active != self.release:
            actions.extend([Action("ACTIVATE", f"release {self.release}"), Action("RESTART", "service")])
        elif env_changed or unit_changed:
            actions.append(Action("RESTART", "service"))
        return actions

    def active_release(self) -> str | None:
        result = self.remote.run(["readlink", f"{self.base}/current"], sudo=True, check=False)
        return Path(result.stdout.strip()).name if result.returncode == 0 else None

    def _write_privileged(self, path: str, content: str, mode: str, owner: str) -> None:
        temp = f"/tmp/vps-deployer-{self.dep.name}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"
        self.remote.run(["tee", temp], input_data=content.encode())
        self.remote.run(["install", "-o", owner.split(":")[0], "-g", owner.split(":")[1], "-m", mode, temp, path], sudo=True)
        self.remote.run(["rm", "-f", temp])

    def apply(self) -> list[Action]:
        if self.dep.privileged:
            raise ConfigError("privileged deployment requires explicit allow_privileged=True")
        return self._apply()

    def apply_privileged(self) -> list[Action]:
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
        new_env, new_unit = env_file(values), render_unit(self.dep, self.host.managed_root)
        if old_env != new_env:
            self._write_privileged(self.env_path, new_env, "0640", f"root:{self.dep.user}")
        if old_unit != new_unit:
            self._write_privileged(self.unit_path, new_unit, "0644", "root:root")
            self.remote.run(["systemctl", "daemon-reload"], sudo=True)
            self.remote.run(["systemctl", "enable", unit_name(self.dep)], sudo=True)
        previous = self.active_release()
        if previous != self.release:
            self.remote.run(["ln", "-sfn", f"releases/{self.release}", f"{self.base}/current"], sudo=True)
            self.remote.run(["systemctl", "restart", unit_name(self.dep)], sudo=True)
            if not self._healthy() and previous:
                self.remote.run(["ln", "-sfn", f"releases/{previous}", f"{self.base}/current"], sudo=True)
                self.remote.run(["systemctl", "restart", unit_name(self.dep)], sudo=True)
                raise RuntimeError("health check failed; previous release restored")
        elif old_env != new_env or old_unit != new_unit:
            self.remote.run(["systemctl", "restart", unit_name(self.dep)], sudo=True)
            if not self._healthy():
                raise RuntimeError("health check failed after configuration restart")
        return actions

    def _install_release(self) -> None:
        upload = f"/tmp/vps-deployer-{self.dep.name}-{self.release}.tar.gz"
        archive: Path
        cleanup = None
        if self.dep.source.is_dir():
            temp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
            temp.close(); archive = Path(temp.name); cleanup = archive
            with tarfile.open(archive, "w:gz") as tar:
                for item in self.dep.source.iterdir():
                    if not _ignored(self.dep.source, item):
                        tar.add(item, arcname=item.name, filter=lambda info: None if any(
                            part in {".git", ".venv", ".idea", "__pycache__", ".pytest_cache"} or part.endswith(".egg-info")
                            for part in Path(info.name).parts) else info)
        else:
            archive = self.dep.source
        try:
            self.remote.upload(archive, upload)
            self.remote.run(["mkdir", "-p", self.release_path], sudo=True)
            self.remote.run(["tar", "-xzf", upload, "-C", self.release_path], sudo=True)
            self.remote.run(["chown", "-R", f"root:{self.dep.user}", self.release_path], sudo=True)
            self.remote.run(["chmod", "-R", "u=rwX,g=rX,o=", self.release_path], sudo=True)
            executable = shlex.split(self.dep.command)[0][2:]
            if self.dep.working_directory != ".":
                executable = f"{self.dep.working_directory}/{executable}"
            self.remote.run(["chmod", "0750", f"{self.release_path}/{executable}"], sudo=True)
            self.remote.run(["rm", "-f", upload])
        finally:
            if cleanup: cleanup.unlink(missing_ok=True)

    def _healthy(self) -> bool:
        hc = self.dep.healthcheck
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
        if self.remote.exists(self.unit_path, sudo=True):
            actions.append(Action("REMOVE", "systemd unit"))
            self.remote.run(["systemctl", "disable", "--now", unit_name(self.dep)], sudo=True, check=False)
            self.remote.run(["rm", "-f", self.unit_path], sudo=True)
            self.remote.run(["systemctl", "daemon-reload"], sudo=True)
            self.remote.run(["systemctl", "reset-failed"], sudo=True, check=False)
        if self.remote.exists(self.env_path, sudo=True):
            actions.append(Action("REMOVE", "environment file"))
            self.remote.run(["rm", "-f", self.env_path], sudo=True)
        # Releases, writable storage and service users are intentionally retained.
        return actions


def select_rollback(releases: list[str], current: str, requested: str | None = None) -> str:
    available = sorted(set(releases), reverse=True)
    if requested:
        if requested not in available or requested == current:
            raise ConfigError("requested rollback release is unavailable or already active")
        return requested
    try:
        idx = available.index(current)
    except ValueError as exc:
        raise ConfigError("active release is not in the release list") from exc
    if idx + 1 >= len(available):
        raise ConfigError("no previous release available")
    return available[idx + 1]
