# Security Policy

## Reporting a Vulnerability

Do not disclose suspected vulnerabilities, credentials, private endpoints,
proprietary designs, commercial PDK material, or NDA logs in a public issue.
Use [GitHub private vulnerability reporting](https://github.com/vpromise/SiliconWeaver/security/advisories/new)
and include only the minimum synthetic reproducer needed to explain the issue.

Please identify:

- the affected component, command, and version;
- the expected trust boundary and observed behavior;
- whether the issue involves model transport, generated code, container
  isolation, path/symlink handling, run identity, state mutation, verification
  bypass, or artifact integrity;
- safe reproduction steps and any known mitigations.

## Supported Releases

Security fixes are applied to the latest public release line. Development
branches and obsolete persisted schemas are not promised automatic migration.

Model quality, benchmark scores, commercial signoff, and unsupported private
EDA integrations are outside this security policy.
