from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess

from .models import Host


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    returncode: int


class RemoteError(RuntimeError):
    pass


class RemoteHost:
    def __init__(self, host: Host, verbose: bool = False):
        self.host = host
        self.verbose = verbose

    @property
    def target(self) -> str:
        return f"{self.host.ssh.user}@{self.host.ssh.host}" if self.host.ssh.user else self.host.ssh.host

    def ssh_argv(self, argv: list[str], sudo: bool = False) -> list[str]:
        remote = (["sudo", "--"] if sudo else []) + argv
        command = " ".join(shlex.quote(x) for x in remote)
        return ["ssh", *( ["-v"] if self.verbose else []), "--", self.target, command]

    def run(self, argv: list[str], *, sudo: bool = False, check: bool = True, input_data: bytes | None = None) -> Result:
        proc = subprocess.run(self.ssh_argv(argv, sudo), input=input_data, capture_output=True)
        result = Result(proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace"), proc.returncode)
        if check and proc.returncode:
            raise RemoteError(f"remote command failed ({argv[0]}): {result.stderr.strip()}")
        return result

    def upload(self, local: Path, remote_path: str) -> None:
        args = ["scp", *( ["-v"] if self.verbose else []), "-r" if local.is_dir() else "", "--", str(local), f"{self.target}:{remote_path}"]
        proc = subprocess.run([x for x in args if x], capture_output=True, text=True)
        if proc.returncode:
            raise RemoteError(f"artifact upload failed: {proc.stderr.strip()}")

    def read(self, path: str, sudo: bool = False) -> str | None:
        result = self.run(["cat", path], sudo=sudo, check=False)
        return result.stdout if result.returncode == 0 else None

    def exists(self, path: str, sudo: bool = False) -> bool:
        return self.run(["test", "-e", path], sudo=sudo, check=False).returncode == 0

