# Third-Party Components

RTL-Closure is Apache-2.0 software. Its Python wheel does not vendor its runtime
dependencies.

Direct Python dependencies in the Agent-only release are:

| Component | Version range | License recorded by the installed distribution |
|---|---|---|
| OpenAI Python library | `>=3.3.1,<4` | Apache-2.0 |
| PyYAML | `>=5,<7` | MIT |

`docker/verify.Dockerfile` derives a verification environment from a pinned
LibreLane image and installs additional verification tools. The Dockerfile is
source configuration, not a prebuilt image bundled in the Python wheel. Before
publishing a derived verification image, generate an SBOM, preserve all
upstream license and notice files, and review every package present in that
image independently of this wheel-level list.
