# vps-deployer

Manage isolated systemd deployments on existing Linux hosts over OpenSSH.

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
