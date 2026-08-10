# Contributing to SiliconWeaver

Thank you for helping improve SiliconWeaver. Contributions must preserve the
separation between confirmed intent, RTL generation and verification, durable
workflow state, and physical implementation evidence.

## Before Opening an Issue

- Search existing issues and component documentation first.
- Use a synthetic, redistributable reproducer.
- Remove credentials, private endpoints, proprietary designs, commercial PDK
  material, NDA logs, and machine-specific state.
- Report security problems privately according to [`SECURITY.md`](SECURITY.md).

## Development Setup

Install both Python distributions into one Python 3.12 environment:

```bash
cd pi-lane
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python --editable "../rtl-closure[test]"
uv pip install --python .venv/bin/python --editable ".[test]"
npm ci --ignore-scripts --no-audit --no-fund
source .venv/bin/activate
```

## Required Fast Checks

```bash
cd pi-lane
ruff check src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
npm run check

cd ../rtl-closure
ruff check src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

These checks do not authorize or require a model request, image download, PDK
installation, or physical-design run. If a change needs an integration run,
record the exact spec, model configuration without secrets, image digest, PDK
revision, commands, run IDs, pass criteria, and evidence paths.

## Pull Request Expectations

1. Keep each change focused and explain which pipeline boundary it affects.
2. Add tests for behavior, validation, failure, retry, recovery, and identity
   changes.
3. Update protocol, schema, CLI, or evidence documentation with the code.
4. Do not weaken a required simulation, formal, synthesis, equivalence, QoR, or
   integrity gate to make a generated candidate pass.
5. Keep benchmark datasets, hidden evaluation, scoring, and evaluator exchange
   outside this repository.
6. Preserve Apache-2.0 notices for modified or redistributed source.

By submitting a contribution, you agree that it is provided under the
[Apache License 2.0](LICENSE).
