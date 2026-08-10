# Pi-Lane

> Pi-Lane is the interactive orchestration and physical-flow control component
> of [SiliconWeaver](../README.md). [简体中文项目说明](../README.zh-CN.md)

Pi-Lane is an open Spec-to-RTL-to-GDS Agent built around an isolated Pi profile,
a durable workflow controller, verified RTL generation, and managed LibreLane
Classic physical implementation.

```text
natural-language intent -> confirmed spec -> verified RTL -> GDS + QoR + attestation
```

Pi-Lane owns interaction and end-to-end orchestration. The separately versioned
RTL-Closure package owns RTL generation, verification, and repair; `lane-agent`
owns the physical run. Their commands, state roots, releases, and artifacts stay
independent, and the orchestrator persists only explicit workflow identities and
bounded status projections.

This component contains the Agent only. Datasets, comparison baselines, hidden
evaluation, scoring, and leaderboards are excluded; see [`OSS_SCOPE.md`](OSS_SCOPE.md).
Only open PDKs are supported. Pi-Lane does not import, invoke, or share state with
ORFS integrations, and its results are not commercial foundry signoff.

Pi-Lane is released under Apache-2.0. See [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).

## Requirements

- Linux, Docker, Python 3.12, and Node.js 24;
- RTL-Closure 0.6.x installed in the same Python environment;
- Pi 0.84.1, pinned by this repository's `package-lock.json`;
- LibreLane 3.0.5 and an open PDK managed through `lane-agent`;
- an OpenAI-compatible model endpoint for RTL-Closure generation.

## Install And Start

For a source install, use sibling `pi-lane` and `rtl-closure` checkouts:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python --editable "../rtl-closure[test]"
uv pip install --python .venv/bin/python --editable ".[test]"
npm ci --ignore-scripts --no-audit --no-fund
source .venv/bin/activate
export PI_LANE_PI_EXECUTABLE="$PWD/node_modules/.bin/pi"

rtl-closure validate --spec "$PWD/specs/simple_counter.json"
lane-agent doctor --pdk sky130A --json
pi-lane
```

`lane-agent image-install` and `lane-agent pdk-install --pdk sky130A` perform
intentional environment setup and may download substantial open-source assets.
Run them before the first physical flow. Configure the model backend with the
`RTL_CLOSURE_*` variables described below before starting a generation run.

The installed `pi-lane` command resolves its monitor and prompt from the source
tree for an editable install or from the wheel's `share/pi-lane` directory. It
uses the Python interpreter that owns the console script, including for all
monitor control-plane calls. `bin/pi-lane` is the source-tree wrapper.
`pi-lane --help` and `-h` are forwarded to the pinned Pi executable without
starting or probing the orchestrator daemon. `pi-lane --version` reports the
Pi-Lane distribution version locally and does not require Pi to be installed.

Set `PI_LANE_WORKSPACE`, `PI_LANE_RESOURCE_ROOT`, or
`PI_LANE_PI_EXECUTABLE` only to override launcher defaults intentionally.

Repository and wheel installs expose the same resource tree. Resolve it before
using a packaged smoke specification or opening installed documentation:

```bash
PI_LANE_RESOURCES="$(python -c \
  'from pi_lane.launcher import resource_root; print(resource_root())')"
pi-lanectl start --spec "$PI_LANE_RESOURCES/specs/simple_counter.json" --json
```

The wheel installs README, licenses, documentation, specifications, examples,
monitor sources, and the Agent prompt below `share/pi-lane`.

The canonical end-to-end smoke input is
[`specs/simple_counter.json`](specs/simple_counter.json). Broader cases are in
[`examples/`](examples/README.md).

Detailed documentation:

- [Agent architecture](docs/ARCHITECTURE.md)
- [Development setup](docs/DEVELOPMENT.md)
- [Agent-only release checklist](docs/RELEASE_CHECKLIST.md)
- [中文整体流程说明](docs/PIPELINE_FLOW_CN.md)
- [全局编排器 v3 说明](docs/ORCHESTRATOR_V3_CN.md)

## Current Contracts

Pi-Lane 0.5.1 accepts current formats only:

| Boundary | Current format |
|---|---|
| Orchestrator wire protocol | protocol 3 |
| Orchestrator database | schema 5 |
| Monitor session checkpoint | schema 6 |
| Lane managed run | schema 6 |
| Lane durable event | schema 3 |
| Lane visualization manifest | schema 3 |

The daemon rejects a database whose metadata or required table layout does not
match schema 5. The monitor rejects non-protocol-3 envelopes and non-schema-6
checkpoints. Lane reconciliation likewise fails closed on unsupported run,
event, or visualization records. There is no automatic state conversion or
alternate-product discovery.

The SQLite schema has eight workflow business tables:
`workflows`, `attempts`, `events`, `artifacts`, `leases`, `client_cursors`,
`terminal_integrity_audits`, and `operations`. The internal `metadata` table
stores the database schema version.

The `operations` table is a durable outbox for `start`, `stop`, and physical
`retry`. A requested mutation is committed before the external command is
dispatched. Its stable idempotency key, request, attempt count, retry deadline,
result, and failure survive daemon termination. Reconciliation resumes pending
or interrupted operations and reuses the same external launch identity, so a
crash cannot silently duplicate a child run.

Each workflow reconciliation uses an expiring lease bound to a random
`lease_token`, owner, PID, and Linux process start time. Heartbeat and release
must present the exact token; an expired lease is replaced only after the
recorded process identity is no longer valid. This fences delayed workers from
releasing or advancing a newer owner's lease.

Completed workflows remain under integrity observation. Schema 5 stores a
bounded attestation in `terminal_integrity_audits`; every eligible completed
workflow is reconciled again on schedule. Valid evidence leaves the projection
unchanged. Confirmed child identity or evidence degradation produces the
controlled transition `completed -> failed` and a durable
`workflow.integrity_degraded` event; a terminal result is never reopened as
running.

## Model Configuration

RTL-Closure consumes its own runtime variables:

```text
RTL_CLOSURE_PROVIDER
RTL_CLOSURE_MODEL
RTL_CLOSURE_BASE_URL
RTL_CLOSURE_API_KEY
```

The Pi-Lane launcher and `pi-lanectl` use the same integration adapter. Before
starting or validating the orchestrator, they read only bounded, owner-only,
non-symlink JSON files from the isolated Pi agent directory and inject any
missing model, base URL, and API key into the orchestrator subprocess
environment. Pi and monitor processes never receive the API key. Explicit
`RTL_CLOSURE_*` environment values take precedence. Credentials are not placed in a workflow
specification, run state, startup record, prompt, model progress event, or
container environment.

Direct standalone `rtl-closure` use must provide its own `RTL_CLOSURE_*`
configuration; RTL-Closure does not read Pi state.

The canonical `specs/simple_counter.json` smoke input omits
`generation.model`, so it inherits `RTL_CLOSURE_MODEL` from the deployment.
Broader examples may pin a model intentionally to document a reproducible run;
remove that field when testing the same example against another configured
backend.

## Monitor

Interactive sessions load only the private Lane monitor. It stays enabled,
claims the newest active orchestrated workflow, and follows child IDs only from
that workflow's `rtl_closure_run_id` and `lane_run_id`. If no active workflow
exists, the widget becomes `idle`; a new workflow is claimed on the next poll.
An explicit `/lane-monitor follow ...` pin remains non-preemptible until
`/lane-monitor auto ...` restores discovery.

Checkpoint schema 6 persists the exact workflow claim, explicit pins, bounded
terminal watch, per-run event cursors, and durable orchestrator `client_id`.
Concurrent polls use claim ownership so a slower response cannot overwrite a
newer attachment. Initial attachment acknowledges the current durable event
tail without replaying it; events created after the claim are emitted in order.

Workflow observation uses protocol-3 requests only:

```text
pi-lanectl status --run-id ... --compact --json
pi-lanectl events --run-id ... --after-sequence ... \
  --client-id ... --acknowledge --json
```

Malformed envelopes, event hash discontinuity, cursor regression, child
identity mismatch, and acknowledgement behind the delivered cursor fail closed
as source-specific monitor errors. A failed poll marks only that source
`degraded`, explicitly warns that its snapshot may be stale, and emits a
recovery notification when reads resume.

Use `/lane-monitor status`, `/lane-monitor latest`, `/lane-monitor off`, and
`/lane-monitor on`. Pin a source with `/lane-monitor follow
<workflow|rtl_closure|lane|pdk|image> <run-id>` and return to discovery with
`/lane-monitor auto [source|all]`. Poll and heartbeat intervals are controlled
by `PI_LANE_MONITOR_INTERVAL_MS` and `PI_LANE_HEARTBEAT_INTERVAL_MS`.

## Verification And Physical Delivery

RTL-Closure freezes the user-confirmed contract and test plan, establishes an
oracle quorum, measures oracle line-coverage evidence, generates multiple RTL
candidates, and runs the closed verification/repair loop. Each failure is
normalized into a counterexample and dependency slice; candidate fixes are
evaluated against the immutable suite, independently verified, accepted only on
strict improvement, and rolled back otherwise. Required lint, bounded formal,
universal properties, independent randomized verification, architecture audit,
generic synthesis equivalence, and publication integrity must all satisfy their
configured gates before physical handoff.

`lane-agent` then runs LibreLane 3.0.5 `Classic` against an immutable input
snapshot. The current physical protocol deliberately retains `PI_LANE_*` run
tags and environment fields because these identify the active Pi-Lane/Lane
handoff; they are not a cross-product state dependency. Lane run schema 6 binds
the snapshot, image digest, open-PDK content, Step registry, QoR, GDS, and
delivery evidence. Durable Step reporting uses event schema 3.

A passing full delivery seals a `delivery_attestation` over the exact final
metrics and canonical GDS bytes, the bound QoR and mapped-equivalence evidence,
and the run/input/image/PDK identities. The terminal event independently binds
that attestation hash. Every explicit reconcile of a sealed completed run records
a fresh `release_integrity` audit: a deterministic mismatch changes the Lane
result from `completed` to `incomplete`, while a transient hash-read failure is
`unknown` and leaves the sealed completion unchanged for a later retry.

Every managed physical run starts an isolated visualization sidecar.
Visualization manifest schema 3 records Step execution, render attempts,
images, errors, and the latest image under:

```text
<design>/runs/PI_LANE_<run-id>/visualizations/
  manifest.json
  timeline.jsonl
  <ordinal>-<step>/
```

The timeline is visualization evidence, not a substitute for the authoritative
Lane event journal. Render failures are reported but do not change a successful
Classic Flow result. Full delivery additionally requires the exact Classic
endpoint, a valid canonical GDSII structure, required mapped equivalence when
configured, and passing QoR/delivery gates. Open-PDK checks are not commercial
foundry signoff or a tapeout guarantee.

## Control Plane

```bash
pi-lanectl daemon ensure --json
pi-lanectl start --spec /absolute/path/spec.json \
  --idempotency-key <stable-key> --json
pi-lanectl status --run-id <workflow-id> --compact --json
pi-lanectl events --run-id <workflow-id> --after-sequence 0 \
  --wait-seconds 25 --client-id <stable-client-id> --acknowledge --json
pi-lanectl list --active --json
```

Diagnose a child only by the ID bound in the workflow:

```bash
rtl-closure report --run-id <rtl-closure-run-id> --json
rtl-closure logs --run-id <rtl-closure-run-id> --lines 80
lane-agent status --run-id <lane-run-id> --json
lane-agent logs --run-id <lane-run-id> --lines 80
lane-agent artifacts --run-id <lane-run-id>
```

`status`, `report`, `list`, and event reads are non-mutating. `reconcile`,
`start`, `stop`, `retry`, and visualization retry mutate durable state. Stop,
input changes, physical overrides, and artifact cleanup require explicit user
authorization.
