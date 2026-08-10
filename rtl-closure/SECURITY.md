# Security Policy

## Reporting A Vulnerability

Do not disclose a suspected vulnerability, credential, private endpoint, or
proprietary design in a public issue. Use the repository's private GitHub
security-advisory reporting channel with a minimal synthetic reproducer.

Include the affected command and version, expected trust boundary, observed
behavior, and whether the issue involves model transport, generated code,
container isolation, state identity, or publication integrity.

## Supported Releases

Security fixes are applied to the latest Agent-only release line. Development
branches and obsolete persisted schemas are not promised automatic migration.

Relevant reports include credential disclosure, unsafe URL or path handling,
container escape assumptions, verification bypass, run-identity confusion,
artifact mutation, and publication-integrity bypass. Model quality, benchmark
scores, and commercial signoff are outside this security policy.
