# Operations

Examples use PowerShell and an explicit infrastructure repository.

```powershell
$infra = $env:SERVICE_INFRA_DIR
$deployer = Join-Path $env:VPS_DEPLOYER_DIR '.venv\Scripts\vps-deployer.exe'
```

## Local prerequisites

- Python environment containing `vps-deployer` and PyYAML.
- OpenSSH client with non-interactive access to each host.
- Persistent User environment roots: `PROJECTS_DIR`, `VPS_DEPLOYER_DIR`, and
  `SERVICE_INFRA_DIR`.
- Required deployment secrets in the current process environment. A new terminal
  is normally required after changing persistent Windows User variables.

When a host uses `privileged_host`, its dedicated key must use the current
`tools/vps-deployer-ssh-gate` as a forced command. Pass `--managed-root` and one
`--storage PATH` for every storage path assigned to deployments using that key.
The gate protocol is versioned by its `vps-deployer-op` command prefix; update
the server-side script and authorized-key policy together with client changes.

## Validate and inspect

```powershell
& $deployer --repo $infra validate
& $deployer --repo $infra check example-prod
& $deployer --repo $infra host inspect prod
& $deployer --repo $infra host onboard prod
```

`inspect` is read-only. `onboard` creates the managed root and deployer
configuration directory. Run onboarding once per host or after repairing host
permissions.

`check` evaluates the deployment's merged application, infrastructure, and
implicit host expectations without changing the host. `plan` and `apply` enforce
the same preflight automatically.

## Plan and apply

```powershell
& $deployer --repo $infra plan example-prod
& $deployer --repo $infra apply example-prod
& $deployer --repo $infra status example-prod
```

Review every plan. A repeated plan after a successful apply should report
`No changes.` Status is successful only when the service is active, the health
check passes, and the active `build.properties` deployment namespace matches the desired release and
Git commit.

For timer deployments, status requires the timer to be active instead of the
oneshot service and reports the last job completion time, systemd result, and
exit status. A failed last invocation makes status unhealthy. Logs still come
from the associated `.service` unit.

Use `--release-id ID` only for deliberate operator-controlled release naming.
Source-state-addressed IDs are safer for ordinary deployments. Automatic IDs use
16 hexadecimal characters; upgrading from older clients creates one new release
because the former default used seven characters.

The first apply with release-completion markers may report `FINALIZE` for an
already active release. The deployer adopts it only when its provenance matches
and its service is healthy. Inactive incomplete release directories are rebuilt;
an incomplete unhealthy active release stops with an error for explicit recovery.

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

Rollback selects the release recorded by `previous` (or an explicitly requested
retained release), switches `current`, updates `previous`, and restarts the service.
It does not change the local desired state, so a subsequent plan
will propose reactivating the current desired release. Successful deployments
retain one rollback target: the release that was active immediately beforehand.
All older release directories are pruned after health and proxy checks succeed.

## Adopting deployments created before resource state metadata

Older releases of vps-deployer did not write `/etc/vps-deployer/<name>.state`.
When upgrading such a deployment, do not remove a timer or HTTP proxy in the same
first apply: without historical metadata the client cannot safely distinguish a
formerly managed resource from unrelated host configuration.

Use a two-step migration:

1. Keep the existing timer and `http_proxy` declarations unchanged and apply once
   with the upgraded client. Verify status and a no-op plan. This records their
   exact ownership in the state file.
2. Commit the manifest change that removes or renames the resource, then plan and
   apply again. The recorded ownership allows transactional cleanup.

If the old desired manifest is unavailable, inspect and reconcile the host
manually before the first upgraded apply; do not guess a proxy name on a shared
host.

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
