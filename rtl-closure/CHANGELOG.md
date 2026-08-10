# Changelog

## 0.6.1 - Unreleased

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
