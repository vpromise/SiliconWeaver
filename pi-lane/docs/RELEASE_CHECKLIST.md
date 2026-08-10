# Agent-Only Release Checklist

## Repository Scope

- [ ] `OSS_SCOPE.md` matches the release contents.
- [ ] No benchmark dataset, baseline RTL, hidden evaluation, score, budget, or
      evaluator-key implementation is present or packaged.
- [ ] Generic verification, QoR, attestation, and integrity evidence remains.
- [ ] Only open-PDK examples and documentation are included.

## Compatibility

- [ ] Pi-Lane and RTL-Closure versions are paired in both READMEs.
- [ ] Pi, Python, Node, LibreLane, verification image, and schema versions are
      pinned or documented.
- [ ] A clean sibling-checkout installation exposes `pi-lane`, `pi-lanectl`,
      `lane-agent`, and `rtl-closure` from the same environment.

## Verification

- [ ] Python lint and unit tests pass in both repositories.
- [ ] Pi monitor syntax, type, and unit checks pass.
- [ ] Both wheels build and contain only the declared public resources.
- [ ] `specs/simple_counter.json` validates against the released RTL-Closure.
- [ ] One clean open-PDK Spec-to-GDS smoke run completes and records its exact
      image, PDK, spec, RTL, QoR, and GDS identities.

## Security And Legal

- [ ] Full tracked history and release archives pass secret scanning.
- [ ] Dependency license reports have been reviewed.
- [ ] Any published container image has an SBOM and complete upstream notices.
- [ ] `LICENSE`, `NOTICE`, `SECURITY.md`, and contribution terms are current.
- [ ] No commercial PDK, NDA material, private endpoint, token, or user-specific
      state path is present.

## GitHub Publication

- [ ] Public `main` starts from the audited Agent-only snapshot or an equivalently
      filtered history; internal commits and tags containing benchmark material
      are not reachable from the public repository.
- [ ] Repository owner and final URLs are selected and added to package metadata.
- [ ] Default branch protection requires CI and review.
- [ ] Private vulnerability reporting and secret scanning are enabled.
- [ ] Release notes state the supported boundary and known limitations.
- [ ] Signed tags and generated wheel checksums are published.
