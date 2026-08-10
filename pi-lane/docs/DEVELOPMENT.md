# Development Setup

Pi-Lane and RTL-Closure are separate distributions but one runtime stack. For a
source installation, place their checkouts next to each other:

```text
workspace/
  pi-lane/
  rtl-closure/
```

Pi-Lane 0.5.x is paired with RTL-Closure 0.6.x, Pi 0.84.1, and LibreLane 3.0.5.
The Python environment should use Python 3.12 so both packages share one
interpreter.

```bash
cd workspace/pi-lane
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python --editable "../rtl-closure[test]"
uv pip install --python .venv/bin/python --editable ".[test]"
npm ci --ignore-scripts --no-audit --no-fund
source .venv/bin/activate
```

Use the repository-pinned Pi executable for an interactive development session:

```bash
export PI_LANE_PI_EXECUTABLE="$PWD/node_modules/.bin/pi"
pi-lane
```

Configure the RTL model backend through `RTL_CLOSURE_PROVIDER`,
`RTL_CLOSURE_MODEL`, `RTL_CLOSURE_BASE_URL`, and `RTL_CLOSURE_API_KEY`. Never put
credentials in a specification, command argument, fixture, or log.

## Fast Checks

These checks do not start a model request or physical-design run:

```bash
ruff check src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
npm run check
rtl-closure validate --spec "$PWD/specs/simple_counter.json"
```

Run the RTL-Closure tests from its checkout as a separate gate:

```bash
cd ../rtl-closure
ruff check src tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

## End-to-End Smoke

The following steps download or build tool images and an open PDK. Run them only
as an intentional integration test on a machine with Docker resources available:

```bash
cd workspace/pi-lane
lane-agent doctor --pdk sky130A --json
lane-agent image-install
lane-agent pdk-install --pdk sky130A
pi-lanectl start --spec "$PWD/specs/simple_counter.json" --json
```

Use the returned workflow ID for every status or event read. Long LibreLane work
is managed in the background by `lane-agent`; do not wrap it in another polling
loop. Do not place proprietary RTL, PDK files, constraints, reports, or logs in
an issue or test fixture.
