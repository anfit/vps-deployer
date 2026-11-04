"""Where secret values come from.

Manifests keep naming a variable (`secrets.<KEY>.from_env: NAME`). This module only
decides where `NAME` is looked up. Sources are tried in order and the first hit wins:

- ``env``     process environment (always first by default; the original behavior)
- ``file``    a user-only ``NAME=value`` file (dotenv style, no interpolation)
- ``keyring`` the operating system credential store (needs ``vps-deployer[keyring]``)

The order is set with ``VPS_DEPLOYER_SECRET_SOURCES`` (default ``env,file``). Values are
never logged or placed in error messages; reports name variables and sources only.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping

from .models import ConfigError

DEFAULT_SOURCES = ("env", "file")
KNOWN_SOURCES = ("env", "file", "keyring")
KEYRING_SERVICE = "vps-deployer"
SOURCES_VARIABLE = "VPS_DEPLOYER_SECRET_SOURCES"
FILE_VARIABLE = "VPS_DEPLOYER_SECRETS_FILE"


def default_secrets_file(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    explicit = environ.get(FILE_VARIABLE)
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vps-deployer" / "secrets.env"


def parse_secrets_file(text: str, where: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise ConfigError(f"{where}:{number}: expected NAME=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def _check_private(path: Path) -> None:
    """On POSIX, refuse a secrets file readable by group or others."""
    if os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path}: permissions {mode:04o} are too open; run chmod 600 on it")


@dataclass
class SecretResolver:
    sources: tuple[str, ...] = DEFAULT_SOURCES
    environ: Mapping[str, str] | None = None
    file_path: Path | None = None

    def __post_init__(self) -> None:
        for source in self.sources:
            if source not in KNOWN_SOURCES:
                raise ConfigError(f"unknown secret source {source!r}; known: {', '.join(KNOWN_SOURCES)}")
        self._file_cache: dict[str, str] | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "SecretResolver":
        environ = os.environ if environ is None else environ
        raw = environ.get(SOURCES_VARIABLE, "")
        sources = tuple(part.strip() for part in raw.split(",") if part.strip()) or DEFAULT_SOURCES
        return cls(sources, environ, default_secrets_file(environ))

    # ---- lookup -------------------------------------------------------------
    def lookup(self, name: str) -> tuple[str, str] | None:
        """Return (source, value) for the first source holding `name`, else None."""
        for source in self.sources:
            value = self._from(source, name)
            if value is not None:
                return source, value
        return None

    def get(self, name: str) -> str | None:
        found = self.lookup(name)
        return found[1] if found else None

    def where(self, name: str) -> str | None:
        found = self.lookup(name)
        return found[0] if found else None

    def _from(self, source: str, name: str) -> str | None:
        if source == "env":
            return (os.environ if self.environ is None else self.environ).get(name)
        if source == "file":
            return self._file_values().get(name)
        return _keyring_get(name)

    def _file_values(self) -> dict[str, str]:
        if self._file_cache is None:
            path = self.file_path or default_secrets_file(self.environ)
            if path.is_file():
                _check_private(path)
                self._file_cache = parse_secrets_file(path.read_text(encoding="utf-8"), str(path))
            else:
                self._file_cache = {}
        return self._file_cache

    # ---- store --------------------------------------------------------------
    def store(self, source: str, name: str, value: str) -> str:
        """Persist a value in `file` or `keyring`; returns where it was stored."""
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ConfigError("secret values may not contain line breaks or NUL")
        if source == "file":
            path = self.file_path or default_secrets_file(self.environ)
            _write_file_value(path, name, value)
            self._file_cache = None
            return str(path)
        if source == "keyring":
            _keyring_module().set_password(KEYRING_SERVICE, name, value)
            return "OS credential store"
        raise ConfigError("secrets can be stored in 'file' or 'keyring'")

    def remove(self, source: str, name: str) -> bool:
        """Delete `name` from `file` or `keyring`; returns whether anything was removed."""
        if source == "file":
            path = self.file_path or default_secrets_file(self.environ)
            removed = _remove_file_value(path, name) if path.is_file() else False
            self._file_cache = None
            return removed
        if source == "keyring":
            return _keyring_delete(name)
        raise ConfigError("secrets can be removed from 'file' or 'keyring'")

    def holders(self, name: str) -> list[str]:
        """Every configured source that holds `name` (not only the winning one)."""
        found = []
        for source in self.sources:
            try:
                if self._from(source, name) is not None:
                    found.append(source)
            except ConfigError:
                if source != "keyring":
                    raise
        return found

    def file_names(self) -> list[str]:
        """Names stored in the secrets file (the keyring cannot be enumerated portably)."""
        return sorted(self._file_values())


def _keyring_module():
    try:
        import keyring
    except ImportError as exc:
        raise ConfigError("the keyring source needs the optional dependency: "
                          "pipx inject vps-deployer keyring  (or pip install 'vps-deployer[keyring]')") from exc
    return keyring


def _keyring_get(name: str) -> str | None:
    return _keyring_module().get_password(KEYRING_SERVICE, name)


def _keyring_delete(name: str) -> bool:
    module = _keyring_module()
    try:
        module.delete_password(KEYRING_SERVICE, name)
    except Exception as exc:  # keyring.errors.PasswordDeleteError when absent
        if type(exc).__name__ == "PasswordDeleteError":
            return False
        raise
    return True


def _line_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    return stripped.partition("=")[0].strip()


def _remove_file_value(path: Path, name: str) -> bool:
    _check_private(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if _line_name(line) != name]
    if len(kept) == len(lines):
        return False
    _atomic_write(path, kept)
    return True


def _write_file_value(path: Path, name: str, value: str) -> None:
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ConfigError(f"invalid secret variable name: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if path.is_file():
        _check_private(path)
    entry = f"{name}={value}"
    replaced = False
    for index, line in enumerate(lines):
        if _line_name(line) == name:
            lines[index] = entry
            replaced = True
    if not replaced:
        lines.append(entry)
    _atomic_write(path, lines)


def _atomic_write(path: Path, lines: list) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".secrets-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n" if lines else "")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
