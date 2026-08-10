# Contributing

RTL-Closure is a fail-closed generation and verification controller. A change
must preserve the immutability of the confirmed specification, oracle evidence,
test-suite epochs, and published artifacts.

Before opening a pull request:

1. use only synthetic or redistributable designs and reviewed open PDK names;
2. keep benchmark cases, hidden evaluation, scoring, and evaluator exchange out
   of this repository;
3. never include credentials, private endpoints, user state, commercial PDK
   material, or NDA logs;
4. run `ruff check src tests` and
   `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest`;
5. add focused tests for every validation, failure, retry, or integrity change;
6. update format documentation when a persisted schema or release gate changes.

Generated RTL must pass every required RTL-Closure, independent random, policy,
formal, lint, synthesis, and publication gate before release. Do not make a test
pass by weakening a required gate or changing frozen expected behavior.

By submitting a contribution, you agree that it is provided under the
Apache License 2.0 terms in `LICENSE`.
