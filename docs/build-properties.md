# Build provenance contract

`build.properties` is an application-facing provenance file at the release root.
It is not a vps-deployer configuration file. Applications, CI systems, artifact
builders, and deployment tools may all implement this contract.

The format is UTF-8, one `key=value` pair per line. Blank lines and lines beginning
with `#` are ignored. Keys must start with a letter and may contain letters,
digits, `.`, `_`, and `-`. Keys must be unique; values must not contain control
characters.

Application and build producers own all keys outside the `deployment.` namespace.
Common examples are `build.version`, `build.timestamp`, `commit.hash`, and
`commit.timestamp`. Applications should expose the fields useful for operational
version accountability.

A deployment producer may append the reserved `deployment.` namespace. When
vps-deployer installs a directory source, it preserves every application/build
property and adds:

```properties
deployment.release=<content-addressed release ID>
deployment.commit=<full Git SHA, when available>
deployment.commit-time=<ISO-8601 Git commit time, when available>
deployment.timestamp=<UTC deployment time>
deployment.branch=<source branch, detached, or unversioned>
deployment.actor=vps-deployer
```

Source files must not predefine `deployment.` keys that the deployer supplies;
doing so is rejected instead of silently replacing provenance. `status` verifies
the active `deployment.release` and, for Git sources, `deployment.commit`.

Tar artifacts should already contain their application build properties. At
present vps-deployer can safely merge application properties only for directory
sources; tar deployments receive the deployment namespace in the installed file.
