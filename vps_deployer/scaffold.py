"""Scaffolding for new infrastructure repositories and application contracts."""
from __future__ import annotations

import os
from pathlib import Path

INFRA_FILES: dict[str, str] = {
    "hosts/prod.yaml": """\
# Replace the alias and user with values for your own host (an alias from
# ~/.ssh/config works). Remove the privileged_* fields if you do not use a
# restricted forced-command key.
name: prod
ssh:
  host: prod-vps
  user: deploy
managed_root: /srv/vps-deployer
""",
    "deployments/example-service/prod.yaml": """\
# Rename and adapt. The application repository provides .deployer/run.sh
# (see `vps-deployer init app`).
name: example-service-prod
host: prod

service:
  user: svc-example

release:
  source: ${PROJECTS_DIR}/example-service

runtime:
  restart: always

environment:
  APP_ENV: production
  APP_BIND: 127.0.0.1:8100
  APP_DATA_DIR: /var/lib/example-service

# Only the variable name is committed; the value comes from your environment.
secrets:
  API_TOKEN:
    from_env: EXAMPLE_SERVICE_PROD_API_TOKEN

storage:
  data:
    path: /var/lib/example-service

healthcheck:
  type: http
  url: http://127.0.0.1:8100/health
""",
    "configs/.gitkeep": "",
    "globals/.gitkeep": "",
    ".env.example": """\
# Names of secret variables referenced by deployments. Never commit values.
# Check which are unset: vps-deployer secrets check
EXAMPLE_SERVICE_PROD_API_TOKEN=
""",
    ".gitignore": """\
# Secrets and operator-local state
.env
.env.*
!.env.example
*.key
*.pem
*.secret

# Local output
*.log
.pytest_cache/
__pycache__/
""",
    "README.md": """\
# Infrastructure

Desired-state manifests for services managed with
[vps-deployer](https://github.com/anfit/vps-deployer). Keep this repository
private and commit secret references, never secret values.

```text
hosts/        SSH identity and managed root per host
deployments/  desired state, grouped by application
configs/      optional committed, non-secret application config
globals/      optional shared, non-secret values
```

Set `PROJECTS_DIR` (where application checkouts live) and `SERVICE_INFRA_DIR`
(this repository), then:

```console
vps-deployer validate
vps-deployer secrets check
vps-deployer host inspect prod
vps-deployer plan example-service-prod
```

For a fuller starting point with an operator guide, see
https://github.com/anfit/service-infra-template.
""",
}

APP_FILES: dict[str, str] = {
    ".deployer/run.sh": """\
#!/bin/sh
# Application-owned entrypoint. Run in the foreground; systemd supervises it.
# Write state to the persistent storage path set by the infrastructure manifest,
# never into the (immutable) release directory.
set -eu
cd "$(dirname "$0")/.."
exec ./bin/your-app
""",
    ".deployer/ignore.txt": """\
# Files matching these patterns are excluded from the release hash and upload.
# Keep local credentials, caches and build output out of managed releases.
.env.*
*.log
*.sqlite*
""",
    ".deployer/expect.yaml": """\
# Host assumptions checked before deployment (assertions only; nothing is installed).
# commands:
#   python3:
#     version: ">=3.10"
# architecture: x86_64
commands: {}
""",
}
GITATTRIBUTES_RULE = "*.sh text eol=lf\n"


def _write(root: Path, rel: str, content: str) -> str:
    path = root / rel
    if path.exists():
        return f"skip    {rel} (exists)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return f"create  {rel}"


def init_infra(root: Path) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    return [_write(root, rel, content) for rel, content in INFRA_FILES.items()]


def init_app(root: Path) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    actions = [_write(root, rel, content) for rel, content in APP_FILES.items()]
    try:
        os.chmod(root / ".deployer" / "run.sh", 0o755)
    except OSError:
        pass
    attrs = root / ".gitattributes"
    if attrs.is_file():
        text = attrs.read_text(encoding="utf-8")
        if "eol=lf" in text:
            actions.append("skip    .gitattributes (already has an eol=lf rule)")
        else:
            prefix = "" if text.endswith("\n") or not text else "\n"
            attrs.write_text(text + prefix + GITATTRIBUTES_RULE, encoding="utf-8", newline="\n")
            actions.append("update  .gitattributes (added *.sh eol=lf)")
    else:
        actions.append(_write(root, ".gitattributes", GITATTRIBUTES_RULE))
    return actions
