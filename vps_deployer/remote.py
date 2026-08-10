from __future__ import annotations

from dataclasses import dataclass
import base64
import json
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

    @property
    def privileged_target(self) -> str:
        host = self.host.ssh.privileged_host or self.host.ssh.host
        return f"{self.host.ssh.privileged_user}@{host}" if self.host.ssh.privileged_user else host

    def ssh_argv(self, argv: list[str], sudo: bool = False) -> list[str]:
        direct_root = sudo and self.host.ssh.privileged_host is not None
        remote = ([] if direct_root else (["sudo", "--"] if sudo else [])) + argv
        if direct_root:
            payload = base64.urlsafe_b64encode(json.dumps(argv, separators=(",", ":")).encode()).decode()
            command = f"vps-deployer-exec {payload}"
        else:
            command = " ".join(shlex.quote(x) for x in remote)
        target = self.privileged_target if direct_root else self.target
        identity = (["-i", str(Path(self.host.ssh.privileged_identity_file).expanduser())]
                    if direct_root and self.host.ssh.privileged_identity_file else [])
        return ["ssh", "-o", "BatchMode=yes", *( ["-v"] if self.verbose else []), *identity, "--", target, command]

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
