from __future__ import annotations

import shlex
from .models import Deployment


def unit_name(deployment: Deployment) -> str:
    return f"vps-deployer-{deployment.name}.service"


def timer_name(deployment: Deployment) -> str:
    return f"vps-deployer-{deployment.name}.timer"


def render_unit(deployment: Deployment, managed_root: str) -> str:
    base = f"{managed_root}/{deployment.name}/current"
    working = base if deployment.working_directory == "." else f"{base}/{deployment.working_directory}"
    writable = "\n".join(f"ReadWritePaths={s.path}" for s in deployment.storage)
    command_parts = shlex.split(deployment.command)
    if command_parts[0].startswith("./"):
        command_parts[0] = f"{working}/{command_parts[0][2:]}"
    command = " ".join(shlex.quote(x) for x in command_parts)
    service_type = "oneshot" if deployment.timer else "simple"
    restart = "" if deployment.timer else f"Restart={deployment.restart}\n"
    install = "" if deployment.timer else "\n[Install]\nWantedBy=multi-user.target\n"
    return f"""[Unit]
Description=vps-deployer service {deployment.name}
After=network-online.target
Wants=network-online.target

[Service]
Type={service_type}
User={deployment.user}
Group={deployment.user}
WorkingDirectory={working}
EnvironmentFile=/etc/vps-deployer/{deployment.name}.env
ExecStart={command}
{restart}NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
{writable}

{install}"""


def render_timer(deployment: Deployment) -> str:
    if not deployment.timer:
        raise ValueError("deployment has no timer")
    timer = deployment.timer
    delay = f"RandomizedDelaySec={timer.randomized_delay_sec}\n" if timer.randomized_delay_sec else ""
    return f"""[Unit]
Description=vps-deployer timer {deployment.name}

[Timer]
OnCalendar={timer.on_calendar}
Persistent={'true' if timer.persistent else 'false'}
{delay}Unit={unit_name(deployment)}

[Install]
WantedBy=timers.target
"""
