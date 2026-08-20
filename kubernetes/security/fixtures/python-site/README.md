# Python Security Research Fixture

This purpose-built site contains one synthetic authorization defect. The
`viewer-fixture` identity can read `/api/admin-canary`, which returns only the
SHA-256 of a root-equivalent canary. It does not provide a shell, host access,
real credential, persistence, or external callback.

The fixture exists to validate source-to-runtime correlation, safe proof-state
handling, report generation, and post-remediation regression recommendations.
It must not be presented as a production application or an undisclosed issue.
