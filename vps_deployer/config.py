from __future__ import annotations

from pathlib import Path
import os
import yaml

from .models import ConfigError, Deployment, Host


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.hosts: dict[str, Host] = {}
        self.deployments: dict[str, Deployment] = {}
        self.globals: dict[str, dict[str, str]] = {}

    @classmethod
    def load(cls, root: Path) -> "Repository":
        repo = cls(root)
        for path in sorted((repo.root / "hosts").glob("*.yaml")):
            host = Host.parse(_yaml(path), str(path))
            if host.name in repo.hosts:
                raise ConfigError(f"duplicate host: {host.name}")
            repo.hosts[host.name] = host
        for path in sorted((repo.root / "globals").glob("*.yaml")):
            raw = _yaml(path)
            repo.globals[path.stem] = {str(k): str(v) for k, v in raw.items()}
        for path in sorted((repo.root / "deployments").rglob("*.yaml")):
            dep = Deployment.parse(_yaml(path), path)
            if dep.name in repo.deployments:
                raise ConfigError(f"duplicate deployment: {dep.name}")
            repo.deployments[dep.name] = dep
        repo.validate()
        return repo

    def validate(self) -> None:
        for dep in self.deployments.values():
            if dep.host not in self.hosts:
                raise ConfigError(f"{dep.name}: unknown host {dep.host}")
            globals_for_dep = self._global_values(dep)
            for key, ref in dep.environment.items():
                if ref.from_global and ref.from_global not in globals_for_dep:
                    raise ConfigError(f"{dep.name}: unknown global reference {ref.from_global} for {key}")

    def _global_values(self, dep: Deployment) -> dict[str, str]:
        # Prefer a globals file matching the suffix (foo-prod -> prod), then host, then merge all if unique.
        candidates = [dep.name.rsplit("-", 1)[-1], dep.host]
        for candidate in candidates:
            if candidate in self.globals:
                return self.globals[candidate]
        merged: dict[str, str] = {}
        for values in self.globals.values():
            merged.update(values)
        return merged

    def resolve_environment(self, dep: Deployment, require_secrets: bool) -> tuple[dict[str, str], set[str]]:
        result: dict[str, str] = {}
        secret_keys: set[str] = set()
        globals_ = self._global_values(dep)
        for key, ref in dep.environment.items():
            result[key] = ref.literal if ref.literal is not None else globals_[str(ref.from_global)]
        for key, ref in dep.secrets.items():
            secret_keys.add(key)
            value = os.environ.get(str(ref.from_env))
            if value is None:
                if require_secrets:
                    raise ConfigError(f"{dep.name}: required secret environment variable is not set for {key}")
                value = "<secret>"
            result[key] = value
        return result, secret_keys


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data
