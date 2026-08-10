# Contributing

Contributions should preserve Pi-Lane's Agent-only boundary and its separation
of durable workflow, RTL generation, and physical implementation state.

Before opening a pull request:

1. keep benchmark cases, hidden evaluation logic, and score handling outside
   this repository;
2. use only open PDK identifiers and synthetic or redistributable RTL fixtures;
3. do not include credentials, private endpoints, user state, or NDA material;
4. run the checks in `docs/DEVELOPMENT.md` for every affected repository;
5. update protocol and schema documentation when a persisted contract changes;
6. include focused tests for failure, recovery, and identity validation paths.

Do not make generated RTL publishable by weakening a required verification,
formal, equivalence, QoR, or integrity gate. Changes to physical-design policy
should describe their open-PDK evidence and limitations explicitly.

By submitting a contribution, you agree that it is provided under the
Apache License 2.0 terms in `LICENSE`.
