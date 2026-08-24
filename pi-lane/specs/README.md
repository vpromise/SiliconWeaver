# Runnable specifications

`simple_counter.json` is the canonical, small end-to-end smoke specification.
It deliberately omits `generation.model` and therefore uses the deployment's
`RTL_CLOSURE_MODEL` value.
Run it through the orchestrator from any checkout location:

```bash
pi-lanectl start --spec "$(pwd)/specs/simple_counter.json" --json
```

The files under `examples/` are broader regression and stress cases. Evaluation
datasets and comparison baselines are intentionally maintained outside the
Pi-Lane Agent repository.

`physical-plan-v2.schema.json` defines the frozen physical-plan evidence
contract. `physical-planning-policy-bootstrap-v2.json` is the bundled,
uncalibrated open-PDK bootstrap policy. It is intentionally marked
`publishable: false`; calibrated policies require separately reviewed human
reference evidence.
