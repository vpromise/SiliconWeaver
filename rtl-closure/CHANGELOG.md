# Changelog

## 0.7.0 - Unreleased

- add explicit `off`, `auto_safe`, and `reference_locked` physical-planning
  modes, content-bound policy/geometry inputs, durable planning status, and a
  fail-closed final Lane handoff;
- preserve independent differential evidence as canonical four-state values in
  transcript schema 2 instead of collapsing unknown or high-impedance bits;
- fail closed when required bounded-formal replay cannot bind resetless initial
  state;
- strengthen mapped-equivalence source, snapshot, Step, verdict, and run
  identity attestations;
- update Ruff to 0.16.4 while explicitly preserving the established
  correctness-focused lint selection;
- migrate the model transport to OpenAI Python 3.3.1 and its explicit HTTPX2
  client while retaining credential-safe, environment-independent requests;
- classify physical-planning failures as non-RTL-repairable.

## 0.6.1 - Initial public snapshot

- define and package the Agent-only open-source scope;
- remove comparison cases from the distribution;
- render bounded-formal constants as fixed-width Yosys bit vectors so values
  wider than 32 bits cannot be sign-extended or truncated;
- add a stable `rtl-closure --version` interface;
- make the canonical counter example inherit the deployment-selected model;
- remove internal extraction wording, machine-local provenance, and
  checkout-relative companion repository links from public documentation;
- add public package classifiers and dependency-update configuration;
- update the test runner to pytest 9.1.1 for its published security fixes;
- add contribution and security documentation.
