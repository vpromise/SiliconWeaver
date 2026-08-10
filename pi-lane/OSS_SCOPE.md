# Pi-Lane Open Source Scope

Pi-Lane is published as an Agent-only Spec-to-GDS system. Benchmark datasets,
comparison baselines, hidden evaluation logic, score aggregation, leaderboards,
and evaluator credentials are intentionally outside this repository.

## Included

- the isolated Pi profile, prompt, and monitor extension;
- the durable `pi-lanectl` workflow orchestrator;
- the `lane-agent` managed LibreLane Classic controller;
- immutable input capture, resumable state, events, QoR, and delivery integrity;
- public specifications and examples used to explain or smoke-test the Agent;
- the command boundary to the separately versioned RTL-Closure engine.

RTL-Closure is part of the usable Agent stack even though it remains a separate
Python distribution. Pi-Lane without RTL-Closure is a physical-flow controller,
not a complete natural-language Spec-to-GDS system.

## Excluded

- benchmark case corpora and hand-authored comparison RTL;
- one-shot, blind, closure, or other evaluation tracks;
- benchmark run identities, budgets, cost ledgers, and score normalization;
- hidden tests, evaluator signing keys, signed evaluation exchange, and upload
  bridges;
- claims about model quality, rankings, commercial signoff, or tapeout
  readiness.

Generic evidence is not benchmark code. Run identity, event-chain integrity,
RTL publication manifests, mapped-equivalence evidence, QoR reports, GDS hashes,
delivery attestations, and post-completion integrity audits remain in scope.

## Public Snapshot Provenance

This repository starts from an audited Agent-only source snapshot. Its public
history is intentionally rooted at that snapshot so benchmark data, comparison
material, hidden evaluation logic, credentials, and private engineering history
are not reachable from public commits or tags.

The public snapshot contains the following release surface:

| Path or component | Public disposition |
|---|---|
| `src/pi_lane/` | Included |
| `extensions/` and `prompts/` | Included |
| `specs/simple_counter.json` and `examples/` | Included |
| `docs/PIPELINE_FLOW_CN.md` and orchestrator docs | Included |
| benchmark corpora, bridges, evaluators, exports, and exchanges | Excluded |
| benchmark schemas, fixtures, services, and CI jobs | Excluded |

Post-snapshot fixes are accepted only through normal public review and the full
Agent-only test suite. A change with benchmark identity, evaluator secrets,
hidden cases, budgets, scores, or external evidence-exchange dependencies must
remain outside this repository.

## Public Integration Surface

A future benchmark project must consume Pi-Lane as an external client. The
stable integration surface is deliberately small:

1. a user-confirmed RTL-Closure JSON specification;
2. `pi-lanectl start`, `status`, `events`, `list`, and `reconcile` JSON
   envelopes;
3. explicit workflow, RTL-Closure, and Lane run identities;
4. RTL publication manifests and verification reports;
5. Lane final metrics, GDS identity, and delivery attestation.

Benchmark-specific fields must not be added to Agent state or runtime protocols.
New evaluation needs should be implemented by translating these public outputs
inside the benchmark project.

## Supported Boundary

Pi-Lane supports only the open PDKs accepted by `lane-agent doctor` and the
pinned LibreLane Classic runtime. It does not inspect or invoke ORFS, accept
commercial PDK material, or represent its checks as foundry signoff.
