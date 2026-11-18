# Security

Report a vulnerability privately through
[GitHub's private vulnerability reporting](https://github.com/CBIIT/nci-sip-mcp/security/advisories/new).
Please do not open a public issue for it. The NCI SIP MCP project coordinator receives the
report and answers it there.

Only the latest release is supported. Response targets are to acknowledge a report within
5 business days and provide an initial assessment (validity and severity) within 10 business
days. For a confirmed High or Critical finding, the target is a fix or published advisory
within 30 days of confirmation; lower severities are scheduled into a milestone.

If GitHub private reporting is unavailable, contact the NCI SIP MCP project coordinator at
[hannes.niedner@nih.gov](mailto:hannes.niedner@nih.gov).

What the server holds and sends upstream (the EVS licence key, the caDSR credential) and how
it keeps them out of logs, errors and results is specified in
[docs/specification.md](docs/specification.md) (A7.5).

The [content-injection threat model](docs/threat-model.md) maps trust boundaries, existing
controls and their tests, and the client host's responsibilities when an assistant reads results.
