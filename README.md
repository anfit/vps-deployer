# vps-deployer

Manage isolated systemd deployments on existing Linux hosts over OpenSSH.

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
