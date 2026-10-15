# Security reporting

If GitHub private vulnerability reporting is enabled for this repository, use
**Security > Report a vulnerability**. Availability depends on repository
settings; this document does not imply that the feature is enabled.

If that option is unavailable, open a minimal issue asking the maintainer for a
private reporting channel. Do not include exploit details, credentials, personal
data, or raw logs in that public issue.

A private report should describe the affected revision, expected and observed
behavior, impact, and a minimal reproduction using synthetic data. Do not attach
real bot tokens, account credentials, paired account IDs, runtime databases,
session transcripts, or unredacted screenshots. If a credential was exposed,
revoke or rotate it through its provider.

The bridge can continue local Codex tasks with their existing permissions.
Owner authentication, exact conversation routing, original execution settings,
delivery deduplication, and private runtime-file boundaries are security-sensitive.
Tests and reports should not start another poller for a live bot or execute a
real task without explicit owner authorization.
