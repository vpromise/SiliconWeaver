# Third-Party Components

Pi-Lane is Apache-2.0 software. Its Python wheel does not vendor its runtime or
development dependencies.

Direct dependencies in the Agent-only release are:

| Component | Use | License recorded by the pinned package |
|---|---|---|
| PyYAML | Python runtime | MIT |
| Pi coding agent 0.84.2 | interactive Agent, installed by npm | MIT |
| Pi TUI 0.84.2 | monitor development and tests | MIT |
| TypeScript 5.9.3 | development and type checks | Apache-2.0 |
| Node.js type definitions 22.19.19 | development and type checks | MIT |

RTL-Closure and LibreLane are separately installed projects and are not bundled
in the Pi-Lane wheel. Their distributions and generated container images retain
their own license and notice obligations. Review the complete resolved dependency
graph again when updating either lock file or publishing a prebuilt image.
