# RTL-Closure Open Source Scope

RTL-Closure is the verified RTL engine in the Pi-Lane Agent stack. It turns a
user-confirmed behavioral specification into a publication bundle that may be
handed to `lane-agent` for open-PDK physical implementation.

## Included

- specification validation and intent-closure hashing;
- Python oracle generation, quorum selection, and immutable expected behavior;
- Verilog candidate generation, counterexample-guided repair, and rollback;
- Cocotb simulation, lint, bounded formal checks, universal properties,
  independent randomized verification, architecture audit, and synthesis
  equivalence;
- immutable publication manifests, release integrity, events, reports, and the
  explicit `lane-agent` handoff;
- small public examples that document or smoke-test supported specification
  behavior.

## Excluded

- benchmark datasets, large comparison cases, and hand-authored baselines;
- hidden evaluation suites and evaluator-only verification;
- benchmark run identities, experiment tracks, budgets, score aggregation, and
  leaderboards;
- signed evaluator exchange, result upload, and evaluator credentials.

## Public Snapshot Provenance

This repository starts from an audited Agent-only source snapshot. Its public
history is intentionally rooted at that snapshot so comparison inputs, hidden
evaluation logic, credentials, and private engineering history are not
reachable from public commits or tags. Post-snapshot changes enter through
normal public review and must remain independent of benchmark identity and
evaluation state.

## Public Contract

The supported external surface is the `rtl-closure` CLI, its validated JSON
specification, explicit run IDs, JSON status/report responses, durable events,
the read-only publication bundle, and its Lane handoff manifest. A future
benchmark project must consume those outputs as an external client and must not
write RTL-Closure state or publication directories.

RTL-Closure supports only reviewed open PDK values in its schema. Its formal,
simulation, equivalence, and open-source physical-flow evidence must not be
represented as complete behavioral proof, commercial signoff, or tapeout
readiness.
