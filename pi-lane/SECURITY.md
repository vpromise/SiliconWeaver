# Security Policy

## Reporting A Vulnerability

Do not disclose a suspected vulnerability, credential, private endpoint, or
proprietary design in a public issue. Use the repository's private GitHub
security-advisory reporting channel and include only the minimum synthetic
reproducer needed to explain the issue.

Reports should identify the affected command and version, expected trust
boundary, observed behavior, and whether a model, container, state directory,
or artifact boundary is involved.

## Supported Releases

Security fixes are applied to the latest Agent-only release line. Development
branches and old state-schema versions are not promised automatic migration.

## Scope

Relevant reports include credential exposure, command injection, unsafe path or
symlink handling, container escape assumptions, workflow identity confusion,
artifact-integrity bypass, and unauthorized mutation through a read interface.
Commercial PDK signoff, model-output quality, and benchmark scores are outside
this security policy.
