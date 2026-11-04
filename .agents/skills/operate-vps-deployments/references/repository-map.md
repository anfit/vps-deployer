# Repository map

Use the persistent environment roots; do not infer repositories by walking parent directories.

| Variable | Repository | Responsibility |
| --- | --- | --- |
| `VPS_DEPLOYER_DIR` | `vps-deployer` source checkout | Documentation, the bundled example, and development of the CLI. Operating deployments uses the installed tool (`pipx install vps-deployer`), not this checkout |
| `SERVICE_INFRA_DIR` | `service-infra` | Hosts, deployment manifests, committed non-secret configuration, and estate documentation |
| `PROJECTS_DIR` | parent of application repositories | Application source used to build releases |
| `SERVICE_INFRA_TEMPLATE_DIR` | `service-infra-template` | Public, generic starting point for infra repositories. Generic changes to `service-infra` (docs, tooling, layout) are mirrored here in the same working session; check with `tools/check-template-parity.py` |

Configuration that is safe to commit belongs in `service-infra`. Only genuine secret values belong in environment-backed `secrets.from_env` declarations. A `from_global` value is non-secret shared configuration and may be committed by name.

Deployment names identify environments, even when several deployments share one VPS. Each must have a unique deployment/proxy name, loopback port, storage, route, and secret namespace.
