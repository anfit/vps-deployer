"""Host bootstrap: install the SSH gate and print the matching authorized_keys line."""
from __future__ import annotations

from importlib import resources

GATE_INSTALL_PATH = "/usr/local/sbin/vps-deployer-ssh-gate"


def gate_source() -> bytes:
    return resources.files("vps_deployer").joinpath("data/vps-deployer-ssh-gate").read_bytes()


def storage_paths(repo, host_name: str) -> list[str]:
    """Storage paths of every deployment on the host, for the gate's --storage policy."""
    paths = {s.path for dep in repo.deployments.values() if dep.host == host_name for s in dep.storage}
    return sorted(paths)


def authorized_keys_line(host, storage: list[str]) -> str:
    flags = f"--managed-root {host.managed_root}" + "".join(f" --storage {p}" for p in storage)
    return (f'command="{GATE_INSTALL_PATH} {flags}",restrict '
            "ssh-ed25519 AAAA...REPLACE_WITH_DEDICATED_PUBLIC_KEY vps-deployer")


def render_instructions(host, storage: list[str]) -> str:
    ssh = host.ssh
    lines = [
        f"Bootstrap plan for host {host.name}",
        "",
        f"1. Install the gate (root:root 0755): {GATE_INSTALL_PATH}",
        "   Run `vps-deployer host bootstrap " + host.name + " --install` to do this over your",
        "   existing sudo-capable SSH access, or copy the packaged script yourself.",
        "2. Create a dedicated key pair for the privileged identity (ssh-keygen -t ed25519).",
        "3. Add this line to the privileged user's ~/.ssh/authorized_keys on the host,",
        "   replacing the placeholder with the new public key:",
        "",
        "   " + authorized_keys_line(host, storage),
        "",
        "4. In hosts/" + host.name + ".yaml set ssh.privileged_host, privileged_user and",
        "   privileged_identity_file to that key.",
        "5. Verify: vps-deployer host inspect " + host.name + "   (reports the gate protocol).",
    ]
    if not storage:
        lines.insert(-1, "   Note: no deployment on this host declares storage, so no --storage flags are emitted.")
    if ssh.privileged_host is None:
        lines.append("\nThis host currently uses sudo mode; steps 2-4 are only needed to adopt the gate.")
    return "\n".join(lines)
