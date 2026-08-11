from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import os
import re
import unicodedata
from typing import Any

SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SAFE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_UNIT_PATH = re.compile(r"^/?[A-Za-z0-9._+@:/-]+$")


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
    manifest_path: Path = Path()

    @classmethod
    def parse(cls, data: dict[str, Any], path: Path) -> "Deployment":
        source_name = str(path)
        name = str(_required(data, "name", source_name))
        if not SAFE_NAME.fullmatch(name):
            raise ConfigError(f"{source_name}: invalid deployment name")
        service = _required(data, "service", source_name)
        release = _required(data, "release", source_name)
        runtime = _required(data, "runtime", source_name)
        if not all(isinstance(x, dict) for x in (service, release, runtime)):
            raise ConfigError(f"{source_name}: service, release and runtime must be mappings")
        user = str(_required(service, "user", source_name))
        privileged = service.get("privileged", False)
        if not isinstance(privileged, bool):
            raise ConfigError(f"{source_name}: service.privileged must be boolean")
        if not SAFE_USER.fullmatch(user) or (user == "root" and not privileged) or (privileged and user != "root"):
            raise ConfigError(f"{source_name}: invalid service user")
        command = safe_text(str(_required(runtime, "command", source_name)).strip(),
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
        stores = tuple(Storage(str(k), safe_absolute(str(v.get("path", "")), f"storage.{k}"))
                       for k, v in (data.get("storage") or {}).items() if isinstance(v, dict))
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
            if not re.fullmatch(r"[A-Za-z0-9.-]+", domain) or not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", upstream):
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
        return cls(name, str(_required(data, "host", source_name)), user, privileged, src, tuple(includes), command, wd, restart,
                   data.get("healthcheck"), env, secrets, stores, proxy, path)
