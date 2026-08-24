# Changelog

## 0.6.0 - Unreleased

- add a durable, tool-driven physical planner with sealed candidate domains,
  bounded geometry probes, schema-v2 plans and policies, geometry locks, and an
  explicit final Lane handoff;
- bind managed runs to immutable LibreLane image-lock history and strengthen
  runtime, input, process, QoR, mapped-equivalence, GDS, and delivery evidence;
- keep the public bootstrap planning policy non-publishable until complete
  Human reference calibration is supplied explicitly;
- harden orchestrator and monitor identity/error projections without adding
  private evaluation or benchmark state;
- update the pinned Pi coding agent and Pi TUI to 0.84.2.

## 0.5.1 - Initial public snapshot

- define and package the Agent-only open-source scope;
- remove benchmark datasets, baseline RTL, and comparison-only resources;
- document the Pi-Lane, RTL-Closure, and LibreLane ownership boundaries;
- fix Linux process identity parsing for process names containing spaces or
  closing parentheses;
- make every packaged example valid under the paired RTL-Closure release;
- make every public CLI report the Pi-Lane distribution version without
  starting the orchestrator or probing the separately installed Pi executable;
- make the canonical smoke specification inherit the deployment-selected model;
- remove private extraction-history references and checkout-relative companion
  repository links from public documentation;
- align the declared Python floor with the tested Python 3.12 runtime and add
  public package classifiers and dependency-update configuration;
- update the pinned Pi stack to 0.84.1 and the test runner to pytest 9.1.1 so
  the initial public dependency graph contains their published security fixes;
- add contribution, security, development, and release documentation.
