# pi-lane repository policy

This project is completely isolated from pi-orfs. Do not import, invoke, edit,
or inspect ORFS integrations while operating pi-lane. Only open LibreLane PDKs
listed by `lane-agent doctor` are permitted.

Long LibreLane flows must use `lane-agent start` in the background. Interactive
`pi-lane` sessions use only the private `lane-monitor` extension for continuous
managed-state polling and transition/error notifications; do not start a second
shell watch loop. Never delete a run, stop a process, change a design, or enable
network access without an explicit user request.

Each managed run has a separate offline visualization sidecar. It records the
Step timeline and notebook-style KLayout previews under the run's
`visualizations/` directory. A visualization failure must be reported from the
`visualization.errors` field, but it does not change the Classic Flow result.
