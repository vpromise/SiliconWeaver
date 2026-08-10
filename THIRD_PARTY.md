# Third-Party Components

SiliconWeaver is Apache-2.0 software. Its two Python wheels do not vendor their
runtime or development dependencies.

The direct dependency declarations and recorded licenses are maintained in:

- [`pi-lane/THIRD_PARTY.md`](pi-lane/THIRD_PARTY.md)
- [`rtl-closure/THIRD_PARTY.md`](rtl-closure/THIRD_PARTY.md)

The Pi-Lane lock file pins the Pi coding agent, Pi TUI, TypeScript, and Node.js
type definitions used by the interactive profile and monitor tests. RTL-Closure
depends directly on the OpenAI Python library and PyYAML.

The verification Dockerfile derives from a digest-pinned LibreLane image. It is
source configuration, not a prebuilt image bundled by this repository. Before
publishing a derived image, generate and review an SBOM, retain upstream license
and notice files, and review the complete resolved operating-system and Python
dependency graph.

Dependency versions and licenses can change independently of this document.
Regenerate the resolved inventory whenever a lock file, base image, or release
dependency changes.
