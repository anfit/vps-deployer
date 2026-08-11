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
            request = self._privileged_request(argv)
            payload = base64.urlsafe_b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
            command = f"vps-deployer-op {payload}"
        else:
            command = " ".join(shlex.quote(x) for x in remote)
        target = self.privileged_target if direct_root else self.target
        identity = (["-i", str(Path(self.host.ssh.privileged_identity_file).expanduser())]
                    if direct_root and self.host.ssh.privileged_identity_file else [])
        return ["ssh", "-o", "BatchMode=yes", *( ["-v"] if self.verbose else []), *identity, "--", target, command]

    @staticmethod
    def _privileged_request(argv: list[str]) -> dict[str, object]:
        command = argv[0] if argv else ""
        operation = {
            "cat": "read-file", "test": "path-exists", "readlink": "read-link",
            "mkdir": "ensure-directories", "install": "install", "useradd": "create-user",
            "systemctl": "service-control", "nginx": "validate-nginx", "ln": "set-link",
            "find": "list-releases", "rm": "remove-paths", "tar": "extract-release",
            "chown": "set-ownership", "chmod": "set-mode", "journalctl": "service-logs",
            "write-file": "write-file",
        }.get(command)
        if not operation:
            raise RemoteError(f"no privileged operation for command: {command}")
        return {"operation": operation, "arguments": argv[1:]}

    def write_file(self, path: str, content: bytes, mode: str, owner: str, group: str) -> None:
        if self.host.ssh.privileged_host is not None:
            self.run(["write-file", path, owner, group, mode], sudo=True, input_data=content)
            return
        script = ("set -eu; target=$1; owner=$2; group=$3; mode=$4; "
                  "temp=$(mktemp \"$(dirname \"$target\")/.vps-deployer.XXXXXX\"); "
                  "trap 'rm -f \"$temp\"' EXIT; chmod 0600 \"$temp\"; cat > \"$temp\"; "
                  "chown \"$owner:$group\" \"$temp\"; chmod \"$mode\" \"$temp\"; "
                  "mv -fT \"$temp\" \"$target\"; trap - EXIT")
        self.run(["sh", "-c", script, "vps-deployer-write", path, owner, group, mode],
                 sudo=True, input_data=content)

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
