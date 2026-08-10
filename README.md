# vps-deployer

Manage isolated systemd deployments on existing Linux hosts over OpenSSH.

Detailed documentation:

- [`docs/architecture.md`](docs/architecture.md) — ownership model and remote layout.
- [`docs/manifest-reference.md`](docs/manifest-reference.md) — complete host and deployment schema.
- [`docs/operations.md`](docs/operations.md) — onboarding, deployment, status, rollback, removal, and troubleshooting.

Detailed documentation:

- [`docs/architecture.md`](docs/architecture.md) — ownership model and remote layout.
- [`docs/manifest-reference.md`](docs/manifest-reference.md) — complete host and deployment schema.
- [`docs/operations.md`](docs/operations.md) — onboarding, deployment, status, rollback, removal, and troubleshooting.

## Repository layout

Keep host identity separate from application deployment identity. Deployment
files may be grouped recursively by application:

```text
infra/
  hosts/
    prod.yaml
  deployments/
    invoicer/
      prod.yaml
  globals/                 # optional; only genuinely shared non-secret values
  artifacts/               # optional local release inputs
```

See [`examples/hosts/prod.yaml`](examples/hosts/prod.yaml) and
[`examples/deployments/invoicer/prod.yaml`](examples/deployments/invoicer/prod.yaml).
Ordinary configuration should be committed directly in a deployment. Use
`from_global` only for values intentionally shared by multiple deployments, and
use `secrets.from_env` only for secret material.

Multiple deployments may target the same host. An optional `http_proxy` block
manages a deployment's nginx site, TLS certificate references, loopback upstream,
configuration validation, and reload independently from other routes on that host.

Local artifact and include paths support explicit `${NAME}` environment roots.
Unresolved variables and `..` traversal are rejected. For example:

```yaml
release:
  source: ${PROJECTS_DIR}/my-service
  include:
    - source: ${VPS_DEPLOYER_DIR}/configs/my-service/prod.yaml
      target: config/production.yaml
```

Directory artifacts may contain a `.vps-deployer-ignore` file with gitignore-like
glob patterns. Matching developer-local files are excluded from both the release
hash and uploaded archive; use it to prevent local credentials and helper files
from entering managed releases.

Every release receives a generated `build.properties` manifest. It records the
release ID, exact Git commit and commit time when available, source branch,
deployment time, and deployer identity without modifying the checkout. `status`
checks the active manifest against the desired release and commit; stale or
missing metadata makes the deployment unhealthy.

## Commands

```console
vps-deployer validate
vps-deployer host inspect HOST
vps-deployer host onboard HOST
vps-deployer plan DEPLOYMENT
vps-deployer apply DEPLOYMENT
vps-deployer status DEPLOYMENT
vps-deployer logs DEPLOYMENT
vps-deployer rollback DEPLOYMENT
vps-deployer remove DEPLOYMENT
```

Run from the infra repository, or select it explicitly:

```console
vps-deployer --repo ./examples validate
vps-deployer --repo ./examples plan invoicer-prod
```

`remove` stops and disables a deployment and removes its systemd unit and
environment file. It deliberately retains releases, writable storage, and service
users. Privileged deployments require `--allow-privileged` for both apply and
removal.

Hosts may set `ssh.privileged_host`, `ssh.privileged_user`, and
`ssh.privileged_identity_file` to route privileged operations through a separate
SSH identity. Use `tools/vps-deployer-ssh-gate` as that key's server-side forced
command so it cannot open an interactive root shell.

Secrets are accepted only through explicitly declared process-environment
references and are never printed by planning or validation.
