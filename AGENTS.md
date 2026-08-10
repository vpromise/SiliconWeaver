# SiliconWeaver Repository Policy

SiliconWeaver is an Agent-only Spec-to-RTL-to-GDS monorepo. `pi-lane/` owns
interactive orchestration and managed LibreLane physical runs;
`rtl-closure/` owns confirmed specification handling, RTL generation,
verification, repair, and publication.

The project is isolated from ORFS integrations and external RTL-generation
runtimes. Do not import, invoke, edit, or publish into those projects. Use only
reviewed open PDK identifiers and synthetic or redistributable fixtures.

Generated RTL must pass every configured RTL-Closure, independent random,
policy, lint, formal, synthesis, equivalence, publication, and integrity gate
before handoff. Do not weaken a required gate or mutate frozen expected behavior
to make a candidate pass.

Long LibreLane flows must use `lane-agent start` in the background. Interactive
sessions use the private read-only Lane monitor; do not add a second shell watch
loop. Never enable network access, download images or PDKs, start or stop a run,
delete artifacts, or change a design without an explicit request.

Never commit credentials, private endpoints, proprietary RTL, commercial PDK
material, user state, run artifacts, or NDA logs. Treat status/report commands
as reads and start/stop/retry/reconcile operations as explicit mutations.

Run the component-specific lint and test suites for every code change. Keep the
Pi-Lane 0.5.x and RTL-Closure 0.6.x compatibility boundary documented whenever
a CLI, protocol, schema, state, or evidence contract changes.
