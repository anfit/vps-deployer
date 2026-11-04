# Operations

Examples use PowerShell and an explicit infrastructure repository. With the
tool installed on PATH (for example via `pipx`) and `SERVICE_INFRA_DIR` set,
`--repo` may be omitted entirely.

```powershell
$infra = $env:SERVICE_INFRA_DIR
$deployer = 'vps-deployer'    # or a checkout: Join-Path $env:VPS_DEPLOYER_DIR '.venv\Scripts\vps-deployer.exe'
```

POSIX equivalent:

```bash
infra="$SERVICE_INFRA_DIR"
deployer=vps-deployer         # or "$VPS_DEPLOYER_DIR/.venv/bin/vps-deployer"
"$deployer" --repo "$infra" validate
```

## Local prerequisites

- Python environment containing `vps-deployer` and PyYAML.
- OpenSSH client with non-interactive access to each host.
- Persistent User environment roots: `PROJECTS_DIR`, `VPS_DEPLOYER_DIR`, and
  `SERVICE_INFRA_DIR`.
- Required deployment secrets, from the process environment or another secret source
  (see [Secret sources](#secret-sources)). A new terminal is normally required after
  changing persistent Windows User variables.

When a host uses `privileged_host`, its dedicated key must use the current
the packaged `vps-deployer-ssh-gate` (`vps_deployer/data/`; `vps-deployer host bootstrap HOST` prints or installs it) as a forced command. Pass `--managed-root` and one
`--storage PATH` for every storage path assigned to deployments using that key.
The gate protocol is versioned by its `vps-deployer-op` command prefix; update
the server-side script and authorized-key policy together with client changes.

## Secret sources

Manifests do not change: a secret still names a variable. Where that variable is
found is a per-operator choice, so you do not need one persistent User variable per
secret. Sources are tried in order and the first hit wins:

| Source | Where | Notes |
|---|---|---|
| `env` | process environment | always first by default; the original behavior |
| `file` | `NAME=value` file | default `%APPDATA%\vps-deployer\secrets.env` on Windows, `~/.config/vps-deployer/secrets.env` elsewhere; override with `VPS_DEPLOYER_SECRETS_FILE`. On POSIX the file must be mode 600 |
| `keyring` | OS credential store | optional: `pipx inject vps-deployer keyring` |

Set the order with `VPS_DEPLOYER_SECRET_SOURCES` (default `env,file`), for example
`env,file,keyring`. Store and inspect secrets without printing values:

```powershell
& $deployer secrets set LIBRUS_SYNC_PROD_PASSWORD            # prompts; stores in the file
& $deployer secrets set LIBRUS_SYNC_PROD_PASSWORD --store keyring
Get-Content secret.txt | & $deployer secrets set NAME --stdin
& $deployer secrets sources                                    # which source supplies each variable
& $deployer secrets check
```

`secrets sources` exits 1 when any variable is unset. Because the environment wins,
a stale environment variable overrides a newer file entry; `secrets set` warns when
that is the case.

### Managing stored secrets

Storing is only half the job. These commands cover the rest of the lifecycle; none
prints a value.

```powershell
& $deployer secrets list                     # every source holding each variable, who uses it
& $deployer secrets list invoicer-prod       # one deployment
& $deployer secrets rotate NAME              # new value, then which deployments need an apply
& $deployer secrets rotate NAME --store keyring
& $deployer secrets unset NAME               # delete from the file (default)
& $deployer secrets unset NAME --store keyring
```

- **`list`** shows all sources that hold a variable, not only the winning one, so it
  exposes shadowed or duplicated copies. Without a deployment name it also lists
  names in the secrets file that no deployment references (leftovers). It exits 1
  when a referenced variable is unset.
- **`rotate`** is `set` plus a reminder: it names the deployments in the current
  repository that reference the variable. They keep running the old value until you
  `apply` them, and `plan` does not show secret values, so `rotate` is the prompt to do it.
- **`unset`** removes a value from one store, exits 1 if it was not there, and warns
  if another source still supplies it or if deployments are left without it.

**Where entries live and how to inspect them outside the tool**

- *File*: plain text, one `NAME=value` per line, user-only. Comments and unrelated
  lines are preserved when entries change. Back it up like any credential file, or do
  not: treat the secrets manager as the source of truth and the file as a local cache.
- *Windows Credential Manager* (`keyring`): Generic credentials named
  named after the variable and the service `vps-deployer` (typically
  `NAME@vps-deployer`; the exact form depends on the keyring version) under Windows
  Credentials. View or edit them in Control Panel
  (`control /name Microsoft.CredentialManager`); `cmdkey /list | findstr vps-deployer`
  lists them. macOS Keychain and the Linux Secret Service use the same service name,
  `vps-deployer`.
- The credential store cannot be enumerated portably, so `list` shows keyring
  entries only for variables the manifests reference.

**What this does and does not protect**

Stores are per user and per machine. They keep secrets out of persistent environment
variables and out of process listings, but any process running as you can read them,
the same as User environment variables. They are not shared or backed up across
machines. Rotate a secret at its source (the provider), update the password manager,
then run `rotate` here on each machine that deploys.

## Validate and inspect

```powershell
& $deployer --repo $infra validate
& $deployer --repo $infra secrets check
& $deployer --repo $infra check example-prod
& $deployer --repo $infra host inspect prod
& $deployer --repo $infra host onboard prod
```

```powershell
& $deployer --repo $infra host bootstrap prod            # print gate setup and authorized_keys line
& $deployer --repo $infra host bootstrap prod --install  # also copy the gate over sudo SSH
```

`host bootstrap` derives the `--managed-root` and `--storage` flags from the host
and the manifests that target it. `host inspect` reports the gate protocol
(`gate: ok|mismatch|outdated|none`) and exits 1 on a mismatch, so an out-of-date
gate is found before `apply` fails.

After a manual `rollback`, `plan` marks the pending `ACTIVATE` as reversing it.

`inspect` is read-only. `onboard` creates the managed root and deployer
configuration directory. Run onboarding once per host or after repairing host
permissions.

`check` evaluates the deployment's merged application, infrastructure, and
implicit host expectations without changing the host. `plan` and `apply` enforce
the same preflight automatically.

## Scaffold and preflight

```powershell
& $deployer init ./infra                 # new infrastructure repository skeleton
& $deployer init app ./my-service        # .deployer/ skeleton for an application
& $deployer doctor ./my-service          # offline checks; exit 1 on errors
```

`init` never overwrites existing files. `doctor` verifies the application
contract without touching a host: entrypoint scripts have an LF-ended `#!` line,
`expect.yaml` parses, an ignore file exists, no credential-like file would be
uploaded, and (when the infra repository is found) manifests pointing at the
application find their runtime entrypoint. Warnings do not fail the command.

## Inventory and fleet commands

```powershell
& $deployer list                        # offline table of every deployment
& $deployer list --format markdown      # includes storage and secret variable names
& $deployer status --all                # one row per deployment (needs SSH); --jobs N sets parallelism
& $deployer plan --all --exit-code      # exit 1 if any deployment has changes
```

`status --all` reports `state` (`ok` when the service or timer is active and its health check passes,
otherwise `down`) separately from `source` (`in sync`, or `deploy pending` when the local source differs
from the active release). It exits 1 only when a deployment is down or cannot be queried; pending
deploys are visible in the table and in `plan --all --exit-code`.

`list` reads manifests only. A repository's `docs/services.md` can embed the
markdown form between generated markers (see service-infra-template's
`tools/update-services-doc.py`) so the inventory cannot drift.

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

### apply: "secret environment variables not set"

Run `secrets check DEPLOYMENT` to list the missing variable names. A terminal
opened before a persistent variable was set will not see it; open a new one or
copy the User value into the process.

### Plan repeatedly updates environment

Ensure the running deployer includes redacted-secret comparison support and that
all expected secret keys exist. Apply with actual secrets loaded into the process.

### Nginx or TLS failure

Run `nginx -t` on the host, verify certificate paths and expiration, and test the
loopback health URL separately from the public URL. `vps-deployer` reconciles
routes but does not issue certificates.
