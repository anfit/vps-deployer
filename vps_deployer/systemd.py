from __future__ import annotations

import shlex
from .models import Deployment


def unit_name(deployment: Deployment) -> str:
    return f"vps-deployer-{deployment.name}.service"


def render_unit(deployment: Deployment, managed_root: str) -> str:
    base = f"{managed_root}/{deployment.name}/current"
    working = base if deployment.working_directory == "." else f"{base}/{deployment.working_directory}"
    writable = "\n".join(f"ReadWritePaths={s.path}" for s in deployment.storage)
    command = " ".join(shlex.quote(x) for x in shlex.split(deployment.command))
    return f"""[Unit]
Description=vps-deployer service {deployment.name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={deployment.user}
Group={deployment.user}
WorkingDirectory={working}
EnvironmentFile=/etc/vps-deployer/{deployment.name}.env
ExecStart={command}
Restart={deployment.restart}
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
{writable}

[Install]
WantedBy=multi-user.target
"""

