# Security

Athena 0.1 is bounded beta software and should be run with the least
privilege needed for the workspace. The bounded beta native certification cell
is Linux x86_64 with an X11 display and Openbox; broader Linux desktop, macOS,
and Windows behavior is compatibility-targeted, not a parity claim. Agent code
executes only through the configured capability/execution policy and workspace
sandbox boundaries, but operators should still treat a project workspace as
sensitive.

The operator HTTP API is a local single-user convenience boundary, not
authentication against other processes or users on the same host. It accepts
loopback clients only, but any local process that can reach the bound port may
be able to request operator actions, including task execution and provider
outcome reconciliation. Do not expose the port beyond the host or treat
loopback as a substitute for a bearer token, Unix-domain-socket peer policy, or
multi-user isolation. A multi-user or remotely reachable deployment requires
an additional authenticated transport boundary before it is considered safe.

Do not report suspected vulnerabilities, credentials, or private reproductions
in a public issue. Include the affected release version or commit, host/backend,
reproduction, and impact in a private report.

Please use this repository's private GitHub Security Advisory reporting flow.
If that flow is unavailable, contact the maintainers privately and include a
reproduction, affected commit, and impact summary. Allow time for a fix before
public disclosure.
