# RTL-Closure repository policy

This project is isolated from ORFS integrations and external RTL-generation
runtimes. Do not import, invoke, edit, or publish into those projects. Generated
RTL must pass all RTL-Closure, independent random, policy, and Yosys gates before
publication. Only the reviewed open PDKs in the specification schema are allowed.

Legacy product names may appear only in the versioned rename migration document
and in `NOTICE` when required for legal attribution.

Physical planning must remain explicitly selected by the confirmed
specification and defaults to `off`. Planning failures are physical or
infrastructure evidence, never permission to mutate RTL. Preserve four-state
transcript values, resetless-formal fail-closed behavior, content-bound planning
inputs, and the exact Pi-Lane 0.6.x / RTL-Closure 0.7.x handoff.
