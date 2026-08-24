# RTL-Closure

> RTL-Closure is the verified RTL generation and repair component of
> [SiliconWeaver](../README.md). [简体中文项目说明](../README.zh-CN.md)

RTL-Closure is a self-contained natural-language RTL generation, verification,
repair, and LibreLane handoff controller. It does not import an external
RTL-generation runtime or invoke an ORFS integration. The current package
version is 0.7.0.

It is the RTL engine in the Pi-Lane Agent stack. Benchmark datasets, comparison
cases, hidden evaluation, scoring, and leaderboards are intentionally excluded;
see [`OSS_SCOPE.md`](OSS_SCOPE.md).

Detailed Chinese pipeline and LibreLane Classic Flow documentation:
[`docs/PIPELINE_FLOW_CN.md`](docs/PIPELINE_FLOW_CN.md).

The current end-to-end boundary uses Pi-Lane orchestrator protocol 3, database
schema 5, monitor checkpoint schema 6, Lane run schema 6, Lane event schema 3,
and Lane visualization manifest schema 3. Other persistent or wire formats are
rejected; neither controller converts state or infers a run from another state
tree. The `PI_LANE_*` run tags and environment fields remain part of the current
Pi-Lane/Lane physical handoff protocol.

RTL-Closure is released under Apache-2.0. See [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE) for license and inference-design provenance.

Pipeline (0.7):

1. bind the explicit behavioral/interface contract, architecture constraints, requirements, and assumptions to a user-confirmed intent-closure hash;
2. validate the bound JSON specification, then plan declaration-only directed scenarios;
3. generate provenance-tracked Python oracle candidates and require a candidate-only majority quorum;
4. measure Python-oracle executable-line proxy coverage and add validated input-only scenarios before freezing;
5. rerun every candidate origin on the final plan, then hash and freeze the bundle;
6. generate an initial Verilog candidate while all expected behavior stays immutable;
7. run a fixed Cocotb driver with Icarus, reset/dirty probes, internal-state snapshots, and coverage summaries;
8. normalize simulation/formal failures into temporal traces and RTL dependency slices;
9. append deterministic counterexample mutations as hash-chained suite epochs, rerunning the oracle quorum for every epoch;
10. evaluate bounded local repair candidates with full counterexample replay and rollback;
11. run Verilator lint and deterministic Yosys bounded frozen-trace properties;
12. prove explicitly declared safe port-level properties with symbolic inputs and Yosys temporal induction;
13. run two release randomized passes plus exact-interface, policy, and Yosys gates; any behavioral counterexample re-enters the same bounded repair loop and full regression instead of bypassing closure;
14. audit explicit architecture constraints and prove the RTL equivalent to its generic Yosys-synthesized netlist;
15. replay the final active-suite quorum, stage and rehash every release artifact, publish read-only records, optionally complete evidence-bound physical planning, then start the final `lane-agent` run.

The repair model cannot generate or edit Cocotb, expected outputs, the Python
oracle, the contract, or an existing test-suite epoch. Coverage refinement may
return input-only scenario declarations before freezing; repair-time scenarios
are generated deterministically by the controller and appended to a hash chain.
A candidate becomes the new champion
only when it preserves every previously passing check and strictly improves the
lexicographic verification score. After repeated stalls, the controller may
regenerate the full RTL, but it still passes through the same immutable gates.
Historical champion counterexamples, coverage gaps, internal state deltas,
formal counterexamples, and rejected-candidate reasons are fed back into later
repair prompts.

This is a native implementation of ideas used by AutoVeriFix/AutoVeriFix+ and
Veri-Sure; neither external orchestrator is imported. The model backend remains
deployment-configurable. The canonical counter example inherits
`RTL_CLOSURE_MODEL`; the advanced closed-loop example pins `gpt-5.6-sol` to
demonstrate an explicit reproducible configuration. `reasoning_effort` defaults
to `xhigh`. Deterministically generated formal
bounded-trace checks are authoritative only for their selected frozen input
traces; they are not universal properties over arbitrary inputs. The separate
universal layer accepts a safe JSON expression DSL over current/previous
top-level data and status ports, keeps clock/reset harness-controlled, and proves
each requirement-bound `always_implication` independently with Yosys SAT temporal
induction. A bounded or universal `fail` always blocks the candidate and becomes
repair feedback; `unknown/error` is retried once. In either layer's `required`
mode, inconclusive or incomplete evidence blocks release. Advisory evidence is
retained without pretending that a proof passed.

Bounded checkpoints are canonicalized by the exact Yosys obligation while all
scenario/requirement aliases and provenance are retained. There is no global
selection cap: every supported, eligible unique obligation is scheduled in
initial batches of 32. A Yosys rc137/OOM retries
only the affected range as 16 and then 8 obligations; the required manifest is
never shortened. `formal_timeout_seconds` is per process and
`formal_total_timeout_seconds` (default 3600) bounds the complete manifest.
Unsupported frozen scenarios/checkpoints are explicitly excluded and make
required selection incomplete. Required mode also fails closed when any batch
or the final evidence aggregate is incomplete. Typed
configuration, capacity, deadline, infrastructure, and evidence failures are
marked non-repairable and stop before another RTL repair request.

For `universal_mode`, `off` requires an empty property list and records
`not_requested`; `advisory` permits retry-exhausted `unknown/error` evidence but
still rejects any proved counterexample; `required` needs at least one property
and requires every property to pass. Required inconclusive, missing, count/hash
mismatched, or otherwise incomplete evidence blocks publication. Every property
must reference declared requirement IDs, and the published
`universal-properties.json` is bound to both the final RTL and normalized
property hashes. It also declares the supported proof scope and limitations,
and separates covered, uncovered, fully proved, failed, and inconclusive
requirement IDs. Requirement links are traceability rather than proof of the
complete natural-language requirement, so complete behavioral/requirement
proof is always reported as false.

The runtime may be installed in any Python 3.12 virtual environment. RTL
generation, Python-model generation, response extraction, feedback, and
best-of-N selection are implemented in this package; no sibling checkout or
separately installed inference package is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --editable ".[test]"
.venv/bin/ruff check src tests
.venv/bin/python -m pytest
source .venv/bin/activate
```

Repository and wheel installs expose the examples, documentation,
licenses, and verification-image Dockerfile below `share/rtl-closure`. Resolve
the installed resource root once so the same commands work outside a source
checkout:

```bash
RTL_CLOSURE_RESOURCES="$(python - <<'PY'
from importlib.metadata import distribution
from pathlib import Path

import rtl_closure

source_root = Path(rtl_closure.__file__).resolve().parents[2]
if (source_root / "examples/counter.json").is_file():
    resource_root = source_root
else:
    dist = distribution("rtl-closure")
    marker = next(
        item
        for item in dist.files or ()
        if str(item).replace("\\", "/").endswith("share/rtl-closure/README.md")
    )
    resource_root = dist.locate_file(marker).parent
print(resource_root)
PY
)"
```

```bash
rtl-closure doctor
rtl-closure validate --spec "$RTL_CLOSURE_RESOURCES/examples/counter.json"
rtl-closure intent-hash \
  --spec "$RTL_CLOSURE_RESOURCES/examples/closed_loop_sat_counter.json"
rtl-closure start --spec "$RTL_CLOSURE_RESOURCES/examples/counter.json"
rtl-closure list --active --limit 1 --json
rtl-closure reconcile --run-id <run-id> --json
rtl-closure report --json
rtl-closure status --json
rtl-closure stop --run-id <run-id> --json
```

`list`, `status`, and `report` are pure reads. `list --active` returns compact
newest-first summaries for only `created`, `queued`, and `running` runs. The durable orchestrator uses
`reconcile` to advance child-derived state and append terminal events. An
orchestrated launch may also pass `--workflow-id` and `--idempotency-key`;
repeating the same key and specification returns the existing run.

`stop --json` is a confirmation protocol, not a fire-and-forget signal. For a
Lane child, a confirmed stop requires valid identity-attested JSON reporting
`stopped`; a still-running child is an error, while a terminal result that wins
the race is preserved and reconciled instead of being overwritten. For the
local worker, the controller verifies the recorded process identity and process
group, sends `SIGTERM`, and reports `stopped` only after that process is gone; a
confirmation timeout remains `running/stopping`. CLI exit code 0 means the stop
operation and response protocol completed, not necessarily that the top-level
workflow verdict is `stopped`. In particular, release-integrity failures remain
`failed` or `incomplete` even when their identified child was successfully
reclaimed; inspect the returned `status`, `phase`, `failure_code`, and active
Lane attempt status.

Infrastructure recovery can retry only the already-published physical design:

```bash
rtl-closure retry-lane --run-id <run-id> \
  --idempotency-key <workflow-scoped-retry-key> --json
```

For one RTL-Closure run, repeating the same retry key returns the recorded outcome
and never creates another LibreLane child. An uncertain launch reuses the same
Lane launch identity. Use a new key only for an intentional new physical
attempt; omitting the option retains the non-idempotent interactive behavior.

Use `examples/closed_loop_sat_counter.json` for an explicit requirements and
directed-scenario example. `examples/counter.json` is the minimal schema-v1
example; when `verification` is omitted, the controller plans it
at runtime and falls back to deterministic boundary scenarios if planning fails.
`verification.random_regression_cycles` is the number of cycles per frozen
random seed; the release plan generates two independently seeded scenarios.
`generation.n` must be at least 2 because a reviewer is never counted as an
oracle vote. Multiple completions from one provider/model are not described as
provider-independent. Every origin records provider/model provenance;
`verification.oracle_diversity_mode=required` additionally requires the configured number of
distinct declared sources in the winning quorum and fails before RTL generation
when that evidence is absent.

`intent_closure.mode=required` accepts only explicit user requirements, an empty
`unresolved_questions` list, `user_confirmed=true`, and the digest printed by
`rtl-closure intent-hash`. That digest covers the description, complete
interface, normalized architecture constraints, requirements, and assumptions, so changing any of them invalidates
the confirmation. `verification.oracle_line_coverage_mode=required` turns the configured
Python executable-line target into a pre-RTL gate. Its report explicitly marks
the metric as a proxy, never as RTL functional coverage.

The configured ARM64 verification image is the separately versioned 0.4.0
runtime image and is built before, never during, a design run:

```bash
docker build \
  -f "$RTL_CLOSURE_RESOURCES/docker/verify.Dockerfile" \
  -t rtl-closure/verify:0.4.0 "$RTL_CLOSURE_RESOURCES"
rtl-closure doctor
```

At run start the controller resolves both the dedicated verification image and
the general no-network sandbox image to immutable Docker image IDs. It records
both identities in state and release evidence, then uses only those IDs for the
run's containers. Treat rebuilding or retargeting either tag as an
operator-controlled environment change that applies only to a new run.

Each run records the contract, final test plan, oracle, expected trace,
coverage report, immutable oracle-origin bundle, base/final oracle quorums,
adaptive corpus with per-epoch quorum evidence, repair history, and
counterexamples. Attempts that reach simulation retain their internal-state
trace and Cocotb log; an FST/VCD is listed when the simulator produced one.
`formal-report.json`, `formal-properties.json`, `formal-manifest.json`,
`formal-checkpoint.json`, `formal-execution-evidence.json`,
and `verilator-lint.json` are created only when their configured gates run.
Successful release also records
the two seeded passes in schema-3 `independent-verification.json`, the structural checks
in `architecture-audit.json`, the universal proof summary and per-property
artifacts in `universal-properties.json`, the reconstructed harness/script/log
manifest in `universal-execution-evidence.json`, and RTL-versus-generic-synthesis evidence in
`synthesis-equivalence.json` plus `synthesis-equivalence.log`. Publication requires
the fixed evidence manifest to be complete and hash-consistent; simulation,
formal, lint, and independent reports are bound to the exact RTL hash. The
publisher reconstructs the active-suite formal obligation map and every
universal harness/script, then reclassifies persisted solver transcripts before
copying them to the design's `verification/` directory; failed attempts remain
in the run state directory. Publication manifest schema 2 also hashes the Python
reference model and every file below `verification/` (including adaptive epoch
evidence), while its separate Lane input manifest binds exactly the files that
LibreLane consumes.
Each independent run keeps controller-owned `run`, `seed`, `num_tests`, and
`passed` fields outside the nested raw verifier transcript. Publication derives
the seeds, limits, ordered input widths, and exact stimulus sequence again from
the frozen specification. It rejects missing, unknown, non-integer, or
out-of-range output samples and recomputes every Verilog/Python mismatch from
the complete concrete traces. Repair prompts receive only a deterministic
first-failure slice bounded to 32 KiB; the complete transcript remains on disk.
Every failed attempt also keeps its normalized `failure-packet.json`; the
append-only `events.jsonl` records candidate completion, acceptance, rollback,
phase transitions, and terminal errors for the Pi monitor. State loading
recomputes every event digest, sequence, and previous-event link from the beginning
of the bounded journal; a valid tail cannot hide an edited historical event.
Each schema-v3 record binds the prior event through
`previous_event_sha256` and carries its own `event_sha256`. The persisted
`event_sequence` must name the verified tail, and any persisted
`last_event_sha256` must match its digest. For a schema-v3 tail, `status`,
`phase`, and failure class/gate/code must also match the corresponding state
projection. A hand-edited state verdict therefore fails closed even when the
event file itself is intact.

Floorplanning defaults to LibreLane relative sizing, so small designs are sized
from the synthesized design and `FP_CORE_UTIL` instead of receiving a fixed
300-by-300 area. A specification that requests absolute sizing must set
`floorplan_sizing=absolute` and provide both `die_area` and `core_area`. Every publication also emits a stable,
bus-grouped `pin_order.cfg` and references it through `IO_PIN_ORDER_CFG`.

Physical planning is opt-in. `flow.physical_planning_mode` defaults to `off`;
`auto_safe` starts the Pi-Lane planner before final physical implementation, and
`reference_locked` additionally requires an absolute, regular, content-bound
`reference_geometry_lock`. An optional absolute
`physical_planning_policy` is hashed into the validated specification. Planning
failures are classified as physical/infrastructure failures and cannot trigger
another RTL repair request. The bundled Pi-Lane bootstrap policy is explicitly
non-publishable and is not evidence of calibrated QoR, signoff, or tapeout
readiness.

Independent differential evidence now uses transcript schema 2 with canonical
four-state values. Known Verilog bits are compared exactly; `X`/`Z` bits are
retained with an explicit compare mask rather than coerced into ordinary binary
values. Required bounded-formal replay fails closed for resetless state whose
initial value cannot be bound, instead of treating an unconstrained initial
state as a completed proof.

Coverage reports deliberately distinguish planning association from observed
or machine-evaluated evidence: attaching a requirement ID to a scenario is not
a requirement hit. `oracle_line_coverage` is Python-oracle executable-line
coverage; it is not RTL branch, toggle, FSM, or functional coverage.

For full `sky130A` Classic delivery, `flow.mapped_equivalence_mode=required`
renders `RUN_EQY: true` and makes the filesystem-backed `Yosys.EQY` evidence a
child-delivery gate. Its scope is RTL versus the synthesized PDK-mapped
netlist, not post-layout LEC. Schema v2 binds the child run to the exact
published design/config/hash, unique EQY Step, state/PASS files, and all direct
log hashes; exactly one log must carry the PASS verdict marker. Full delivery additionally requires the exact
Classic endpoint, a canonical `.gds` that passes basic GDSII record/structure
validation, and final QoR pass;
open-PDK checks are never described as commercial foundry signoff or a tapeout
guarantee. Reconciliation revalidates required EQY files even after a completed
parent state, and also revalidates the schema-v2 publication bundle before
trusting any terminal snapshot. Missing, added, replaced, symlinked, or changed
publication evidence degrades the parent to `incomplete` with
`release_integrity/publication_invalid`; changed required EQY evidence degrades
it with `release_integrity/mapped_equivalence_evidence`. `status` and `report`
remain pure reads, so the durable orchestrator must continue calling
`reconcile` for this post-completion integrity check.

The 0.7 release uses an OpenAI-compatible streaming API backend configured only
through `RTL_CLOSURE_PROVIDER`, `RTL_CLOSURE_MODEL`, `RTL_CLOSURE_BASE_URL`, and
`RTL_CLOSURE_API_KEY`. Standalone RTL-Closure does not read Pi state. When it is
launched through `pi-lane`, the Pi-Lane launcher injects missing values into the
orchestrator subprocess environment; explicit variables take precedence.
Credentials are never copied to specs, run state, logs, generated designs, or
containers.

The runtime requires OpenAI Python 3.3.1 or newer within the 3.x release line
and explicitly uses its HTTPX2 client. Ambient proxy and certificate environment
variables are ignored (`trust_env=False`); the model endpoint's CA must therefore
be available through the operating-system trust store. This keeps deployment
transport policy outside specifications and prevents ambient shell proxy state
from silently changing credential-bearing requests.

`generation.model` is optional. When it is present, it deliberately pins the
run to that model and overrides `RTL_CLOSURE_MODEL`; when it is absent, the
backend uses `RTL_CLOSURE_MODEL`. There is no schema-level model default, so a
missing model in both places is a typed, non-retryable configuration error.

Model calls use streamed Chat Completions so long `xhigh` generations produce
an early response instead of waiting for one large non-streamed body behind a
proxy timeout. Each request writes a content-free progress summary to
`state.json` and `events.jsonl` at connection, completion, retry/failure, and
every 30 seconds while active. Candidate concurrency defaults to two; one
failed candidate does not discard successful siblings. Transport tuning is
available without putting credentials in a spec:

```bash
export RTL_CLOSURE_MODEL_TIMEOUT_SECONDS=900  # inactivity timeout, 30..3600
export RTL_CLOSURE_MODEL_MAX_RETRIES=2        # retries after first attempt, 0..8
export RTL_CLOSURE_MODEL_CONCURRENCY=2         # simultaneous candidates, 1..8
```

Retries are limited to connection/timeouts, HTTP 408/409/425/429, and 5xx
responses. A gateway `retry-after` value is honored, and no delay occurs after
the final failed attempt. The request idempotency key includes the RTL-Closure run
scope and logical request ID, while transport retries of that one request keep
the same key. API base URLs must be absolute HTTP(S) origins without userinfo,
query credentials, or fragments; persisted/diagnostic model provenance exposes
only the credential-free origin.

New generated designs default to
`~/.local/state/rtl-closure/published-designs/`; set
`RTL_CLOSURE_DESIGN_ROOT` only for an intentional external publication root.
