from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import os
import re
import tarfile
import unicodedata
from typing import Any
import yaml

SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SAFE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_UNIT_PATH = re.compile(r"^/?[A-Za-z0-9._+@:/-]+$")
SAFE_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
SAFE_ARCHITECTURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_CONSTRAINT = re.compile(r"^(>=|<=|==|>|<)\s*([0-9]+(?:\.[0-9]+)*)$")


class ConfigError(ValueError):
    pass


def safe_text(value: str, where: str, *, systemd: bool = False) -> str:
    if any(unicodedata.category(char)[0] == "C" or unicodedata.category(char) in {"Zl", "Zp"}
           for char in value):
        raise ConfigError(f"{where}: control characters are not allowed")
    if systemd and "%" in value:
        raise ConfigError(f"{where}: systemd specifiers are not allowed")
    return value


def _required(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field {key}")
    return data[key]


def safe_absolute(path: str, where: str) -> str:
    safe_text(path, where, systemd=True)
    p = PurePosixPath(path)
    if not p.is_absolute() or ".." in p.parts or str(p) == "/" or not SAFE_UNIT_PATH.fullmatch(path):
        raise ConfigError(f"{where}: unsafe absolute path")
    return str(p)


def local_path(value: str, base: Path, where: str) -> Path:
    """Expand explicit environment roots and reject filesystem traversal."""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if resolved is None:
            raise ConfigError(f"{where}: environment path variable {name} is not set")
        return resolved

    expanded = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", replace, value)
    if "$" in expanded:
        raise ConfigError(f"{where}: invalid or unresolved path variable")
    candidate = Path(expanded)
    if ".." in candidate.parts:
        raise ConfigError(f"{where}: path traversal is not allowed")
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str | None = None
    privileged_host: str | None = None
    privileged_user: str | None = None
    privileged_identity_file: str | None = None


@dataclass(frozen=True)
class Host:
    name: str
    ssh: SSHConfig
    managed_root: str = "/srv/vps-deployer"

    @classmethod
    def parse(cls, data: dict[str, Any], source: str) -> "Host":
        name = str(_required(data, "name", source))
        if not SAFE_NAME.fullmatch(name):
            raise ConfigError(f"{source}: invalid host name")
        ssh = _required(data, "ssh", source)
        if not isinstance(ssh, dict):
            raise ConfigError(f"{source}: ssh must be a mapping")
        return cls(name, SSHConfig(str(_required(ssh, "host", source)), str(ssh["user"]) if ssh.get("user") else None,
                                   str(ssh["privileged_host"]) if ssh.get("privileged_host") else None,
                                   str(ssh["privileged_user"]) if ssh.get("privileged_user") else None,
                                   str(ssh["privileged_identity_file"]) if ssh.get("privileged_identity_file") else None),
                   safe_absolute(str(data.get("managed_root", "/srv/vps-deployer")), source))


@dataclass(frozen=True)
class ValueRef:
    literal: str | None = None
    from_global: str | None = None
    from_env: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str, secret: bool = False) -> "ValueRef":
        if isinstance(value, (str, int, float, bool)) and not secret:
            return cls(literal=safe_text(str(value), where))
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a reference mapping")
        allowed = {"from_env"} if secret else {"from_global"}
        keys = set(value)
        if len(keys) != 1 or not keys <= allowed:
            raise ConfigError(f"{where}: invalid reference")
        reference = value.get("from_env") if secret else value.get("from_global")
        if not isinstance(reference, str) or not SAFE_ENV_NAME.fullmatch(reference):
            raise ConfigError(f"{where}: invalid environment variable name")
        return cls(from_global=value.get("from_global"), from_env=value.get("from_env"))


@dataclass(frozen=True)
class Storage:
    name: str
    path: str


@dataclass(frozen=True)
class HttpProxy:
    name: str
    domain: str
    upstream: str
    tls: bool
    certificate: str | None
    certificate_key: str | None


@dataclass(frozen=True)
class ReleaseInclude:
    source: Path
    target: str


@dataclass(frozen=True)
class Timer:
    on_calendar: str
    persistent: bool = True
    randomized_delay_sec: int = 0


@dataclass(frozen=True)
class CommandExpectation:
    name: str
    version: str | None = None


@dataclass(frozen=True)
class Expectations:
    commands: tuple[CommandExpectation, ...] = ()
    paths: tuple[str, ...] = ()
    architecture: str | None = None


def parse_expectations(data: Any, where: str) -> Expectations:
    if data is None:
        return Expectations()
    if not isinstance(data, dict) or set(data) - {"commands", "paths", "architecture"}:
        raise ConfigError(f"{where}: expect must contain only commands, paths and architecture")
    raw_commands = data.get("commands") or {}
    command_versions: dict[str, str | None] = {}
    if isinstance(raw_commands, list):
        for value in raw_commands:
            command_versions[str(value)] = None
    elif isinstance(raw_commands, dict):
        for name, settings in raw_commands.items():
            if settings is None:
                settings = {}
            if not isinstance(settings, dict) or set(settings) - {"version"}:
                raise ConfigError(f"{where}: invalid expectation for command {name}")
            version = settings.get("version")
            if version is not None and (not isinstance(version, str) or not VERSION_CONSTRAINT.fullmatch(version)):
                raise ConfigError(f"{where}: invalid version constraint for command {name}")
            command_versions[str(name)] = version
    else:
        raise ConfigError(f"{where}: expect.commands must be a list or mapping")
    if any(not SAFE_COMMAND.fullmatch(name) for name in command_versions):
        raise ConfigError(f"{where}: invalid expected command name")
    raw_paths = data.get("paths") or []
    if not isinstance(raw_paths, list):
        raise ConfigError(f"{where}: expect.paths must be a list")
    paths = tuple(safe_absolute(str(value), f"{where}: expect.paths") for value in raw_paths)
    architecture = data.get("architecture")
    if architecture is not None and (not isinstance(architecture, str) or
                                     not SAFE_ARCHITECTURE.fullmatch(architecture)):
        raise ConfigError(f"{where}: invalid expected architecture")
    commands = tuple(CommandExpectation(name, version) for name, version in sorted(command_versions.items()))
    return Expectations(commands, tuple(sorted(set(paths))), architecture)


def application_expectations(source: Path) -> Expectations:
    label = f"{source}:.deployer/expect.yaml"
    content: str | None = None
    if source.is_dir():
        path = source / ".deployer" / "expect.yaml"
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            label = str(path)
    elif source.is_file():
        try:
            with tarfile.open(source, "r:gz") as archive:
                members = [member for member in archive.getmembers()
                           if PurePosixPath(member.name) == PurePosixPath(".deployer/expect.yaml")]
                if len(members) > 1 or (members and not members[0].isfile()):
                    raise ConfigError(f"{label}: invalid or duplicate contract")
                if members:
                    stream = archive.extractfile(members[0])
                    if stream is None:
                        raise ConfigError(f"{label}: contract is unreadable")
                    content = stream.read(64 * 1024 + 1).decode("utf-8")
                    if len(content.encode("utf-8")) > 64 * 1024:
                        raise ConfigError(f"{label}: contract is too large")
        except (tarfile.TarError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{label}: invalid contract artifact") from exc
    if content is None:
        return Expectations()
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{label}: invalid YAML") from exc
    return parse_expectations(data, label)


def merge_expectations(application: Expectations, infrastructure: Expectations,
                       where: str) -> Expectations:
    commands = {item.name: item.version for item in application.commands}
    for item in infrastructure.commands:
        if item.name in commands and commands[item.name] != item.version:
            raise ConfigError(f"{where}: conflicting expectation for command {item.name}")
        commands[item.name] = item.version
    if (application.architecture and infrastructure.architecture and
            application.architecture != infrastructure.architecture):
        raise ConfigError(f"{where}: conflicting architecture expectations")
    return Expectations(tuple(CommandExpectation(name, version) for name, version in sorted(commands.items())),
                        tuple(sorted(set(application.paths) | set(infrastructure.paths))),
                        infrastructure.architecture or application.architecture)


@dataclass(frozen=True)
class Deployment:
    name: str
    host: str
    user: str
    privileged: bool
    source: Path
    includes: tuple[ReleaseInclude, ...]
    command: str
    working_directory: str = "."
    restart: str = "always"
    healthcheck: dict[str, Any] | None = None
    environment: dict[str, ValueRef] = field(default_factory=dict)
    secrets: dict[str, ValueRef] = field(default_factory=dict)
    storage: tuple[Storage, ...] = ()
    http_proxy: HttpProxy | None = None
    timer: Timer | None = None
    expectations: Expectations = Expectations()
    manifest_path: Path = Path()

    @classmethod
    def parse(cls, data: dict[str, Any], path: Path) -> "Deployment":
        source_name = str(path)
        name = str(_required(data, "name", source_name))
        if not SAFE_NAME.fullmatch(name):
            raise ConfigError(f"{source_name}: invalid deployment name")
        service = _required(data, "service", source_name)
        release = _required(data, "release", source_name)
        runtime = data.get("runtime") or {}
        if not all(isinstance(x, dict) for x in (service, release, runtime)):
            raise ConfigError(f"{source_name}: service, release and runtime must be mappings")
        user = str(_required(service, "user", source_name))
        privileged = service.get("privileged", False)
        if not isinstance(privileged, bool):
            raise ConfigError(f"{source_name}: service.privileged must be boolean")
        if not SAFE_USER.fullmatch(user) or (user == "root" and not privileged) or (privileged and user != "root"):
            raise ConfigError(f"{source_name}: invalid service user")
        command = safe_text(str(runtime.get("command", "./.deployer/run.sh")).strip(),
                            f"{source_name}: runtime.command", systemd=True)
        if not command.startswith("./") or ".." in PurePosixPath(command.split()[0]).parts:
            raise ConfigError(f"{source_name}: runtime command must be relative to the release")
        wd = safe_text(str(runtime.get("working_directory", ".")),
                       f"{source_name}: runtime.working_directory", systemd=True)
        if (PurePosixPath(wd).is_absolute() or ".." in PurePosixPath(wd).parts or
                not SAFE_UNIT_PATH.fullmatch(wd)):
            raise ConfigError(f"{source_name}: unsafe working_directory")
        restart = str(runtime.get("restart", "always"))
        if restart not in {"always", "on-failure", "no"}:
            raise ConfigError(f"{source_name}: invalid restart policy")
        env = {str(k): ValueRef.parse(v, f"environment.{k}") for k, v in (data.get("environment") or {}).items()}
        secrets = {str(k): ValueRef.parse(v, f"secrets.{k}", True) for k, v in (data.get("secrets") or {}).items()}
        invalid_keys = [key for key in (*env, *secrets) if not SAFE_ENV_NAME.fullmatch(key)]
        if invalid_keys:
            raise ConfigError(f"{source_name}: invalid environment variable name: {invalid_keys[0]}")
        storage_data = data.get("storage") or {}
        if not isinstance(storage_data, dict) or any(not isinstance(value, dict) for value in storage_data.values()):
            raise ConfigError(f"{source_name}: storage must be a mapping of mappings")
        stores = tuple(Storage(str(k), safe_absolute(str(v.get("path", "")), f"storage.{k}"))
                       for k, v in storage_data.items())
        proxy_data = data.get("http_proxy")
        proxy = None
        if proxy_data is not None:
            if not isinstance(proxy_data, dict):
                raise ConfigError(f"{source_name}: http_proxy must be a mapping")
            proxy_name = str(proxy_data.get("name", name))
            domain = str(_required(proxy_data, "domain", source_name))
            upstream = str(_required(proxy_data, "upstream", source_name))
            if not SAFE_NAME.fullmatch(proxy_name):
                raise ConfigError(f"{source_name}: invalid http_proxy.name")
            upstream_match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", upstream)
            if (not re.fullmatch(r"[A-Za-z0-9.-]+", domain) or not upstream_match or
                    int(upstream_match.group(1)) > 65535):
                raise ConfigError(f"{source_name}: invalid http_proxy route")
            tls = proxy_data.get("tls", True)
            if not isinstance(tls, bool):
                raise ConfigError(f"{source_name}: http_proxy.tls must be boolean")
            if tls:
                certificate = safe_absolute(str(_required(proxy_data, "certificate", source_name)), source_name)
                certificate_key = safe_absolute(str(_required(proxy_data, "certificate_key", source_name)), source_name)
            else:
                if proxy_data.get("certificate") or proxy_data.get("certificate_key"):
                    raise ConfigError(f"{source_name}: HTTP-only proxy must not declare certificates")
                certificate = certificate_key = None
            proxy = HttpProxy(proxy_name, domain, upstream, tls, certificate, certificate_key)
        timer_data = data.get("timer")
        timer = None
        if timer_data is not None:
            if not isinstance(timer_data, dict):
                raise ConfigError(f"{source_name}: timer must be a mapping")
            on_calendar = safe_text(str(_required(timer_data, "on_calendar", source_name)),
                                    f"{source_name}: timer.on_calendar", systemd=True)
            persistent = timer_data.get("persistent", True)
            delay = timer_data.get("randomized_delay_sec", 0)
            if not isinstance(persistent, bool) or not isinstance(delay, int) or not 0 <= delay <= 86400:
                raise ConfigError(f"{source_name}: invalid timer settings")
            if data.get("healthcheck") is not None or proxy is not None:
                raise ConfigError(f"{source_name}: timer deployments cannot declare healthcheck or http_proxy")
            timer = Timer(on_calendar, persistent, delay)
        healthcheck = data.get("healthcheck")
        if healthcheck is not None:
            if not isinstance(healthcheck, dict) or set(healthcheck) != {"type", "url"}:
                raise ConfigError(f"{source_name}: healthcheck must contain only type and url")
            health_type = str(healthcheck.get("type"))
            health_url = safe_text(str(healthcheck.get("url", "")), f"{source_name}: healthcheck.url")
            match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})(/[^\s]*)?", health_url)
            if health_type != "http" or not match or int(match.group(1)) > 65535:
                raise ConfigError(f"{source_name}: healthcheck must be a loopback HTTP URL")
            healthcheck = {"type": health_type, "url": health_url}
        src = local_path(str(_required(release, "source", source_name)), path.parent, f"{source_name}: release.source")
        includes: list[ReleaseInclude] = []
        for index, item in enumerate(release.get("include", []) or []):
            if not isinstance(item, dict):
                raise ConfigError(f"{source_name}: release.include[{index}] must be a mapping")
            include_source = local_path(str(_required(item, "source", f"release.include[{index}]")), path.parent,
                                        f"{source_name}: release.include[{index}].source")
            target = str(_required(item, "target", f"release.include[{index}]"))
            safe_text(target, f"{source_name}: release.include[{index}].target")
            target_path = PurePosixPath(target)
            if target_path.is_absolute() or ".." in target_path.parts or target in ("", "."):
                raise ConfigError(f"{source_name}: unsafe release include target")
            includes.append(ReleaseInclude(include_source, target))
        expected = merge_expectations(application_expectations(src),
                                      parse_expectations(data.get("expect"), source_name), source_name)
        return cls(name, str(_required(data, "host", source_name)), user, privileged, src, tuple(includes), command, wd, restart,
                   healthcheck, env, secrets, stores, proxy, timer, expected, path)
