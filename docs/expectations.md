# Host expectations

Native applications depend on a host environment that is not packaged into a
container image. Expectations make that environment contract explicit and check
it before deployment mutation.

Expectations are assertions only:

```text
expectation fails -> deployment stops -> operator repairs the host separately
```

They never install packages, create unrelated files, run arbitrary predicates,
or otherwise remediate a host.

## Ownership

Application runtime assumptions live at `.deployer/expect.yaml`:

```yaml
commands:
  python3:
    version: ">=3.12"
  ffmpeg: {}
architecture: x86_64
```

Infrastructure-specific assumptions live in the deployment manifest:

```yaml
expect:
  paths:
    - /etc/letsencrypt/live/example.org/fullchain.pem
    - /etc/letsencrypt/live/example.org/privkey.pem
```

The contracts are merged. Duplicate command or architecture declarations must
agree; conflicts are configuration errors. Paths are combined.

## Supported checks

- `commands` may be a list of executable names or a mapping. A mapping may add a
  `version` constraint using `>=`, `<=`, `==`, `>`, or `<` and dotted numeric
  versions. Version checks execute `<command> --version` as the ordinary SSH user
  and compare the first numeric version in its output.
- `paths` is a list of absolute paths checked as the ordinary SSH user.
- `architecture` is compared exactly with `uname -m`.

Every deployment also implicitly expects `systemctl` and `tar`; HTTP health checks
add `curl`, and nginx proxies perform the existing privileged, read-only `nginx -t`
validation. These deployer prerequisites do not need to be repeated in application
or infrastructure contracts.

Use `vps-deployer check DEPLOYMENT` to display the complete host contract. `plan`
and `apply` enforce the same checks; a failure occurs before deployment files,
releases, services, or routes are changed.

Deliberate limitations preserve scope: no package/version databases, shell
expressions, scripts, network probes, package installation, or provider-specific
remediation.
