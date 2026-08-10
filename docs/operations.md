# Operations

Examples use PowerShell and an explicit infrastructure repository.

```powershell
$infra = Join-Path $env:PROJECTS_DIR 'service-infra'
$deployer = '.\.venv\Scripts\vps-deployer.exe'  # from the vps-deployer checkout
```

## Local prerequisites

- Python environment containing `vps-deployer` and PyYAML.
- OpenSSH client with non-interactive access to each host.
- Persistent User environment roots such as `PROJECTS_DIR` and
  `VPS_DEPLOYER_DIR`.
- Required deployment secrets in the current process environment. A new terminal
  is normally required after changing persistent Windows User variables.

## Validate and inspect

```powershell
& $deployer --repo $infra validate
& $deployer --repo $infra host inspect prod
& $deployer --repo $infra host onboard prod
```

`inspect` is read-only. `onboard` creates the managed root and deployer
configuration directory. Run onboarding once per host or after repairing host
permissions.

## Plan and apply

```powershell
& $deployer --repo $infra plan example-prod
& $deployer --repo $infra apply example-prod
& $deployer --repo $infra status example-prod
```

Review every plan. A repeated plan after a successful apply should report
`No changes.` Status is successful only when the service is active, the health
check passes, and the active `build.properties` matches the desired release and
Git commit.

Use `--release-id ID` only for deliberate operator-controlled release naming.
Content-addressed IDs are safer for ordinary deployments.

## Logs

```powershell
& $deployer --repo $infra logs example-prod --lines 200
& $deployer --repo $infra logs example-prod --since '1 hour ago'
& $deployer --repo $infra logs example-prod --follow
```

If Windows cannot render a journal character, inspect directly with SSH and
`journalctl -u vps-deployer-example-prod.service`.

## Rollback

```powershell
& $deployer --repo $infra rollback example-prod
& $deployer --repo $infra rollback example-prod --release RELEASE_ID
```

Rollback selects an existing retained release, switches `current`, and restarts
the service. It does not change the local desired state, so a subsequent plan
will propose reactivating the current desired release.

## Remove

```powershell
& $deployer --repo $infra remove example-prod
```

Removal disables and stops the service and deletes its generated systemd unit,
environment file, and declared nginx site. Releases, storage, users, application
data, certificates, and unrelated host configuration are retained.

## Shared-host checklist

Before adding another deployment to an existing host, verify that it has:

- a unique deployment and proxy name;
- a unique loopback port;
- distinct storage paths;
- the intended secret-variable names;
- a valid certificate for its domain;
- an application health endpoint that does not require external routing.

## Troubleshooting

### Health check failed; previous release restored

Confirm `status`, then inspect logs. Common causes are an invalid shebang or line
ending, a missing runtime dependency, a port collision, incorrect environment,
or an application startup time longer than the health retry window. The previous
release remains active after a successful automatic rollback.

### Status reports stale or missing manifest

The active release predates generic manifest support or was modified outside the
deployer. Apply the deployment again to install a generated manifest. Do not edit
release contents in place.

### Plan repeatedly updates environment

Ensure the running deployer includes redacted-secret comparison support and that
all expected secret keys exist. Apply with actual secrets loaded into the process.

### Nginx or TLS failure

Run `nginx -t` on the host, verify certificate paths and expiration, and test the
loopback health URL separately from the public URL. `vps-deployer` reconciles
routes but does not issue certificates.
