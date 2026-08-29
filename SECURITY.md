# Security

Athena is beta software and should be run with the least privilege needed for
the workspace. The supported stable-beta host is Linux; macOS and Windows are
compatibility targets, not parity claims. Agent code executes only through the
configured capability/execution policy and workspace sandbox boundaries, but
operators should still treat a project workspace as sensitive.

Do not report suspected vulnerabilities, credentials, or private reproductions
in a public issue. Include the affected beta version or commit, host/backend,
reproduction, and impact in a private report.

Please use this repository's private GitHub Security Advisory reporting flow.
If that flow is unavailable, contact the maintainers privately and include a
reproduction, affected commit, and impact summary. Allow time for a fix before
public disclosure.
