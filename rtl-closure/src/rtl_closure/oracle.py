"""Provenance-tracked generation, consensus, review, and freezing of an oracle."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .contract import canonical_json, content_hash
from .errors import NonRepairableInfrastructureError, PreRtlGateError
from .extraction import extract_python
from .spec import DesignSpec, render_interface, render_question
from .verification import check_oracle_policy, generate_expected_trace


ORIGIN_BUNDLE_SCHEMA_VERSION = 2
QUORUM_EVIDENCE_SCHEMA_VERSION = 2
PYTHON_ORACLE_SYSTEM_PROMPT = """You are an expert hardware modeling assistant.
Write safe, deterministic Python that is a cycle-accurate reference model for
the supplied hardware contract. Use the exact class, method, signal names, and
widths in the skeleton. One eval() call represents one rising clock edge. Mask
all outputs and state to their declared widths. Return the complete Python model
inside <answer> tags and a python code fence."""


def _build_python_oracle_prompt(
    question: str, interface: str, python_skeleton: str
) -> str:
    """Render the Lane-owned oracle prompt against the pinned RTL-Closure API."""
    return f"""{question.strip()}

Use this exact SystemVerilog interface:
```systemverilog
{interface.strip()}
```

Complete this skeleton without renaming the class, method, signal keys, or
predefined state variables:
```python
{python_skeleton.strip()}
```

Do not import modules, access files, use eval/exec, or perform external actions.
Return the complete implementation inside <answer> and a python code fence.
"""


def backend_provenance(backend: Any) -> dict[str, Any]:
    """Describe a model source without treating completions as independent providers."""
    supplied = getattr(backend, "provenance", None)
    if callable(supplied):
        supplied = supplied()
    if not isinstance(supplied, dict):
        supplied = {}
    provider_value = supplied.get("provider", getattr(backend, "provider", None))
    model_value = supplied.get("model", getattr(backend, "model", None))
    declared = bool(provider_value and model_value)
    provider = (
        str(provider_value)
        if provider_value
        else f"{type(backend).__module__}.{type(backend).__qualname__}"
    )
    model = str(model_value) if model_value else "unspecified"
    source_id = content_hash({"provider": provider, "model": model})
    return {
        "provider": provider,
        "model": model,
        "source_id": source_id,
        "declared": declared,
    }


def oracle_diversity_report(
    spec: DesignSpec,
    origins: list[dict[str, Any]],
    *,
    quorum_origin_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected = origins
    if quorum_origin_ids is not None:
        selected_ids = set(quorum_origin_ids)
        selected = [row for row in origins if row.get("origin_id") in selected_ids]
    provenances = [
        row.get("provenance")
        for row in selected
        if isinstance(row.get("provenance"), dict)
    ]
    declared_source_ids = {
        row.get("source_id") for row in provenances if row.get("declared") is True
    }
    declared_source_ids.discard(None)
    mode = spec.verification["oracle_diversity_mode"]
    minimum = spec.verification["oracle_min_distinct_sources"]
    minimum_is_valid = mode != "required" or minimum >= 2
    satisfied = minimum_is_valid and len(declared_source_ids) >= minimum
    report = {
        "schema_version": 1,
        "scope": "winning_quorum" if quorum_origin_ids is not None else "configured_candidates",
        "mode": mode,
        "metric": "distinct_declared_provider_model_sources",
        "minimum_distinct_sources": minimum,
        "required_minimum_floor": 2,
        "minimum_configuration_valid": minimum_is_valid,
        "distinct_source_count": len(declared_source_ids),
        "distinct_source_ids": sorted(declared_source_ids),
        "source_count_in_scope": len(selected),
        "satisfied": satisfied,
        "warning": None,
    }
    if not minimum_is_valid:
        report["warning"] = (
            "required oracle diversity is misconfigured: "
            f"minimum_distinct_sources={minimum}, but required mode needs at least 2"
        )
    elif not satisfied:
        report["warning"] = (
            "oracle source diversity was not met: "
            f"{len(declared_source_ids)} distinct declared provider/model source(s) "
            f"< required {minimum}; multiple completions from one source are not "
            "provider-independent"
        )
    return report


def enforce_oracle_diversity(
    spec: DesignSpec,
    origins: list[dict[str, Any]],
    *,
    quorum_origin_ids: list[str] | None = None,
) -> dict[str, Any]:
    report = oracle_diversity_report(
        spec, origins, quorum_origin_ids=quorum_origin_ids
    )
    if report["mode"] == "required" and not report["satisfied"]:
        raise PreRtlGateError("oracle_diversity", str(report["warning"]))
    return report


def _trace_behavior_hash(trace: dict[str, Any]) -> str:
    behavior = {
        "trace": [
            {
                "scenario_index": row["scenario_index"],
                "scenario_id": row["scenario_id"],
                "cycle": row["cycle"],
                "expected": row["expected"],
            }
            for row in trace["trace"]
        ],
        "setup_observations": [
            {
                "scenario_index": row["scenario_index"],
                "scenario_id": row["scenario_id"],
                "prime_asserted_edge": row.get("prime_asserted_edge"),
                "prime_deasserted_edge": row.get("prime_deasserted_edge"),
                "dirty_edges": row.get("dirty_edges", []),
                "asynchronous_assertion": row.get("asynchronous_assertion"),
                "asserted_edges": row.get("asserted_edges", []),
                "deasserted_edge": row.get("deasserted_edge"),
            }
            for row in trace.get("setup_observations", [])
        ],
    }
    return content_hash(behavior)


def _candidate_quorum(
    valid: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        groups[row["behavior_sha256"]].append(row)
    ranked = sorted(
        groups.values(),
        key=lambda rows: (-len(rows), int(rows[0]["candidate_index"])),
    )
    winner = ranked[0] if ranked else []
    if len(winner) < 2:
        raise RuntimeError(
            "Python oracle candidate quorum was not met: fewer than two "
            "candidate completions agreed"
        )
    if len(winner) * 2 <= len(valid):
        raise RuntimeError(
            "Python oracle candidate quorum was ambiguous: no strict candidate majority"
        )
    origins = [str(row["origin_id"]) for row in winner]
    if len(origins) != len(set(origins)):
        raise RuntimeError("Python oracle candidate quorum reused an origin")
    return winner


def validate_oracle_origin_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate immutable source material and honest source provenance."""
    if not isinstance(bundle, dict) or set(bundle) != {"schema_version", "origins"}:
        raise RuntimeError("oracle origin bundle has an invalid schema")
    schema_version = bundle.get("schema_version")
    if schema_version != ORIGIN_BUNDLE_SCHEMA_VERSION:
        raise RuntimeError("oracle origin bundle has an unsupported schema version")
    origins = bundle.get("origins")
    if not isinstance(origins, list) or len(origins) < 2:
        raise RuntimeError("oracle origin bundle must contain at least two origins")
    required = {
        "origin_id",
        "candidate_index",
        "response_sha256",
        "oracle_sha256",
        "response",
        "oracle",
        "initial_valid",
        "initial_behavior_sha256",
        "initial_error",
    }
    required.add("provenance")
    seen_ids: set[str] = set()
    seen_indices: set[int] = set()
    for position, origin in enumerate(origins, 1):
        if not isinstance(origin, dict) or set(origin) != required:
            raise RuntimeError("oracle origin bundle contains an invalid origin record")
        origin_id = origin.get("origin_id")
        candidate_index = origin.get("candidate_index")
        if (
            not isinstance(origin_id, str)
            or isinstance(candidate_index, bool)
            or not isinstance(candidate_index, int)
            or candidate_index != position
            or origin_id != f"candidate-{candidate_index:02d}"
            or origin_id in seen_ids
            or candidate_index in seen_indices
        ):
            raise RuntimeError("oracle origin bundle identities are duplicate or misordered")
        seen_ids.add(origin_id)
        seen_indices.add(candidate_index)
        response = origin.get("response")
        code = origin.get("oracle")
        if not isinstance(response, str) or not isinstance(code, str):
            raise RuntimeError("oracle origin bundle source material is invalid")
        if content_hash(response) != origin.get("response_sha256"):
            raise RuntimeError(f"oracle origin response hash is invalid: {origin_id}")
        if content_hash(code) != origin.get("oracle_sha256"):
            raise RuntimeError(f"oracle origin code hash is invalid: {origin_id}")
        if not isinstance(origin.get("initial_valid"), bool):
            raise RuntimeError("oracle origin bundle initial validity is invalid")
        behavior = origin.get("initial_behavior_sha256")
        error = origin.get("initial_error")
        if behavior is not None and (
            not isinstance(behavior, str) or len(behavior) != 64
        ):
            raise RuntimeError("oracle origin bundle initial behavior hash is invalid")
        if error is not None and not isinstance(error, str):
            raise RuntimeError("oracle origin bundle initial error is invalid")
        provenance = origin.get("provenance")
        provenance_required = {"provider", "model", "source_id", "declared"}
        if not isinstance(provenance, dict) or set(provenance) != provenance_required:
            raise RuntimeError("oracle origin bundle provenance is invalid")
        provider = provenance.get("provider")
        model = provenance.get("model")
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
            or not isinstance(provenance.get("declared"), bool)
            or provenance.get("source_id")
            != content_hash({"provider": provider, "model": model})
        ):
            raise RuntimeError("oracle origin bundle provenance is invalid")
    return origins


def build_oracle_origin_bundle(
    oracle_metadata: dict[str, Any], candidate_artifact_dir: Path
) -> dict[str, Any]:
    """Freeze all candidate sources needed to replay quorum from each origin."""
    origins = oracle_metadata.get("candidate_origins")
    if not isinstance(origins, list) or not origins:
        raise RuntimeError("oracle origin metadata is missing")
    bundled: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            raise RuntimeError("oracle origin metadata contains an invalid record")
        origin_id = origin.get("origin_id")
        candidate_index = origin.get("candidate_index")
        if not isinstance(origin_id, str) or not isinstance(candidate_index, int):
            raise RuntimeError("oracle origin metadata has an invalid identity")
        source_dir = candidate_artifact_dir / f"candidate-{candidate_index:02d}"
        try:
            code = (source_dir / "oracle.py").read_text(encoding="utf-8")
            response = (source_dir / "response.txt").read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"cannot freeze oracle origin {origin_id}: {exc}"
            ) from exc
        if content_hash(code) != origin.get("oracle_sha256") or content_hash(
            response
        ) != origin.get("response_sha256"):
            raise RuntimeError(f"oracle origin changed before freezing: {origin_id}")
        provenance = origin.get("provenance")
        if not isinstance(provenance, dict):
            raise RuntimeError(f"oracle origin provenance is missing: {origin_id}")
        bundled.append(
            {
                "origin_id": origin_id,
                "candidate_index": candidate_index,
                "response_sha256": origin["response_sha256"],
                "oracle_sha256": origin["oracle_sha256"],
                "response": response,
                "oracle": code,
                "initial_valid": origin.get("valid") is True,
                "initial_behavior_sha256": origin.get("initial_behavior_sha256"),
                "initial_error": origin.get("error"),
                "provenance": provenance,
            }
        )
    bundle = {
        "schema_version": ORIGIN_BUNDLE_SCHEMA_VERSION,
        "origins": bundled,
    }
    validate_oracle_origin_bundle(bundle)
    return bundle


def _review_prompt(
    contract: dict[str, Any], candidates: list[str], selected_index: int
) -> str:
    rendered = "\n\n".join(
        f"CANDIDATE {index + 1}:\n```python\n{code}\n```"
        for index, code in enumerate(candidates)
    )
    return f"""Audit the Python reference model against the immutable hardware
contract. Candidate {selected_index + 1} currently has the strongest execution
consensus. Fix it only if the contract demands different behavior.

The model interface is fixed: class TopModule with eval(self, inputs) returning
a dict for every output. One eval call represents one rising clock edge. Reset,
priority, latency, state updates, widths, signedness, and wraparound must match
the contract exactly. Do not import modules, access files, use eval/exec, or
perform any external action.

IMMUTABLE CONTRACT:
{canonical_json(contract)}

{rendered}

Return only the complete reviewed Python model inside <answer> and a python code
fence.
"""


def _python_skeleton(spec: DesignSpec) -> str:
    outputs = [port for port in spec.ports if port.direction == "output"]
    inputs = [
        port
        for port in spec.ports
        if port.direction == "input" and port.name != spec.clock_port
    ]
    lines = ["class TopModule:", "    def __init__(self):"]
    for port in outputs:
        lines.append(f"        self.out_{port.name} = 0")
    lines.extend(["", "    def eval(self, inputs: dict) -> dict:"])
    for port in inputs:
        lines.append(
            f'        in_{port.name} = inputs.get("{port.name}", 0) & {(1 << port.width) - 1}'
        )
    lines.append("        # TODO: implement one rising-edge transition")
    returned = ", ".join(
        f'"{port.name}": self.out_{port.name} & {(1 << port.width) - 1}'
        for port in outputs
    )
    lines.append(f"        return {{{returned}}}")
    return "\n".join(lines)


def generate_frozen_oracle(
    spec: DesignSpec,
    backend,
    contract: dict[str, Any],
    plan: dict[str, Any],
    artifact_dir: Path,
    *,
    reviewer_backend=None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt = _build_python_oracle_prompt(
        render_question(spec) + "\n\nImmutable requirements:\n" + canonical_json(contract),
        render_interface(spec),
        _python_skeleton(spec),
    )
    backends = list(backend) if isinstance(backend, (list, tuple)) else [backend]
    if not backends:
        raise PreRtlGateError(
            "oracle_diversity", "no Python oracle generation backend was configured"
        )
    candidate_count = spec.generation["n"]
    assigned_backends = [backends[index % len(backends)] for index in range(candidate_count)]
    assigned_origins = [
        {
            "origin_id": f"candidate-{index + 1:02d}",
            "provenance": backend_provenance(candidate_backend),
        }
        for index, candidate_backend in enumerate(assigned_backends)
    ]
    # A required diversity gate can reject a single-source configuration before
    # spending any model calls. Advisory mode records the limitation instead.
    preflight_diversity = oracle_diversity_report(spec, assigned_origins)
    diversity_path = artifact_dir / "oracle-diversity.json"
    diversity_path.write_text(
        json.dumps(preflight_diversity, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if (
        preflight_diversity["mode"] == "required"
        and not preflight_diversity["satisfied"]
    ):
        raise PreRtlGateError(
            "oracle_diversity",
            f"{preflight_diversity['warning']}; artifact={diversity_path}",
        )
    if len(backends) == 1:
        raw = backends[0].generate(
            prompt,
            n=candidate_count,
            temperature=spec.generation["temperature"],
            max_tokens=8000,
            system_prompt=PYTHON_ORACLE_SYSTEM_PROMPT,
        )
        response_backends = [backends[0]] * len(raw)
    else:
        raw = []
        response_backends = []
        for candidate_backend in assigned_backends:
            responses = candidate_backend.generate(
                prompt,
                n=1,
                temperature=spec.generation["temperature"],
                max_tokens=8000,
                system_prompt=PYTHON_ORACLE_SYSTEM_PROMPT,
            )
            if not responses:
                raise RuntimeError("Python oracle backend returned no completion")
            raw.append(responses[0])
            response_backends.append(candidate_backend)
    candidates = [extract_python(response) for response in raw]
    valid: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    candidate_origins: list[dict[str, Any]] = []
    for index, code in enumerate(candidates):
        origin_id = f"candidate-{index + 1:02d}"
        candidate_dir = artifact_dir / f"candidate-{index + 1:02d}"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        (candidate_dir / "response.txt").write_text(raw[index], encoding="utf-8")
        (candidate_dir / "oracle.py").write_text(code, encoding="utf-8")
        origin = {
            "origin_id": origin_id,
            "candidate_index": index + 1,
            "response_sha256": content_hash(raw[index]),
            "oracle_sha256": content_hash(code),
            "provenance": backend_provenance(response_backends[index]),
            "valid": False,
        }
        try:
            check_oracle_policy(code)
            trace = generate_expected_trace(spec, code, plan, candidate_dir)
            behavior_hash = _trace_behavior_hash(trace)
            origin["valid"] = True
            origin["initial_behavior_sha256"] = behavior_hash
            valid.append(
                {
                    "origin_id": origin_id,
                    "candidate_index": index + 1,
                    "code": code,
                    "trace": trace,
                    "behavior_sha256": behavior_hash,
                }
            )
        except NonRepairableInfrastructureError:
            raise
        except Exception as exc:
            origin["error"] = str(exc)[:2000]
            failures.append({"candidate": index + 1, "error": str(exc)[:2000]})
        candidate_origins.append(origin)
    if not valid:
        raise RuntimeError(f"all Python oracle candidate completions failed: {failures}")

    consensus = _candidate_quorum(valid)
    diversity = oracle_diversity_report(
        spec,
        candidate_origins,
        quorum_origin_ids=[str(row["origin_id"]) for row in consensus],
    )
    diversity_path.write_text(
        json.dumps(diversity, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if diversity["mode"] == "required" and not diversity["satisfied"]:
        raise PreRtlGateError(
            "oracle_diversity", f"{diversity['warning']}; artifact={diversity_path}"
        )
    selected = consensus[0]
    selected_index = int(selected["candidate_index"])
    selected_code = str(selected["code"])
    selected_trace = selected["trace"]
    behavior_hash = str(selected["behavior_sha256"])

    reviewed = False
    review_error = None
    try:
        active_reviewer = reviewer_backend or backends[0]
        review_response = active_reviewer.generate(
            _review_prompt(
                contract,
                [str(row["code"]) for row in valid],
                valid.index(selected),
            ),
            n=1,
            temperature=0.1,
            max_tokens=8000,
            system_prompt=(
                "You are the golden-model reviewer for a hardware design. "
                "Return only a safe, deterministic TopModule Python implementation."
            ),
        )[0]
        (artifact_dir / "review-response.txt").write_text(
            review_response, encoding="utf-8"
        )
        reviewed_code = extract_python(review_response)
        (artifact_dir / "reviewed-oracle.py").write_text(
            reviewed_code, encoding="utf-8"
        )
        check_oracle_policy(reviewed_code)
        reviewed_trace = generate_expected_trace(
            spec, reviewed_code, plan, artifact_dir / "reviewed"
        )
        reviewed_hash = _trace_behavior_hash(reviewed_trace)
        if reviewed_hash != consensus[0]["behavior_sha256"]:
            review_error = (
                "reviewed oracle disagreed with the execution consensus; "
                "the consensus oracle was retained"
            )
        else:
            selected_code = reviewed_code
            selected_trace = reviewed_trace
            behavior_hash = reviewed_hash
            reviewed = True
    except NonRepairableInfrastructureError:
        raise
    except Exception as exc:
        review_error = str(exc)[:2000]

    execution_quorum = len(consensus)
    active_reviewer = reviewer_backend or backends[0]

    metadata = {
        "candidate_count": len(candidates),
        "valid_candidate_count": len(valid),
        "consensus_count": len(consensus),
        "consensus_behavior_sha256": consensus[0]["behavior_sha256"],
        "quorum_origin_ids": [row["origin_id"] for row in consensus],
        "selected_candidate": selected_index,
        "reviewed": reviewed,
        "review_error": review_error,
        "reviewer_provenance": backend_provenance(active_reviewer),
        "execution_quorum": execution_quorum,
        "quorum_met": True,
        "oracle_sha256": content_hash(selected_code),
        "expected_behavior_sha256": behavior_hash,
        "candidate_origins": candidate_origins,
        "candidate_failures": failures,
        "diversity": diversity,
    }
    (artifact_dir / "oracle-metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return selected_code, selected_trace, metadata


def validate_oracle_quorum_evidence(
    evidence: dict[str, Any],
    final_plan: dict[str, Any],
    selected_code: str,
    selected_trace: dict[str, Any],
    origin_bundle: dict[str, Any],
) -> None:
    """Validate exact-schema quorum evidence against its source trust roots."""
    required = {
        "schema_version",
        "quorum_met",
        "reviewer_counted",
        "final_testplan_sha256",
        "selected_oracle_sha256",
        "selected_expected_trace_sha256",
        "consensus_behavior_sha256",
        "consensus_count",
        "quorum_origin_ids",
        "valid_candidate_count",
        "origin_bundle_sha256",
        "origin_results",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise RuntimeError("final oracle quorum evidence has an invalid schema")
    if (
        evidence.get("schema_version") != QUORUM_EVIDENCE_SCHEMA_VERSION
        or evidence.get("quorum_met") is not True
        or evidence.get("reviewer_counted") is not False
    ):
        raise RuntimeError("final oracle quorum evidence is not authoritative")
    origins = validate_oracle_origin_bundle(origin_bundle)
    if evidence.get("origin_bundle_sha256") != content_hash(origin_bundle):
        raise RuntimeError("final oracle quorum references another origin bundle")
    if evidence.get("final_testplan_sha256") != content_hash(final_plan):
        raise RuntimeError("final oracle quorum references another test plan")
    if evidence.get("selected_oracle_sha256") != content_hash(selected_code):
        raise RuntimeError("final oracle quorum references another selected oracle")
    if evidence.get("selected_expected_trace_sha256") != content_hash(selected_trace):
        raise RuntimeError("final oracle quorum references another expected trace")
    selected_behavior = _trace_behavior_hash(selected_trace)
    if evidence.get("consensus_behavior_sha256") != selected_behavior:
        raise RuntimeError("final oracle quorum behavior disagrees with the selected trace")

    results = evidence.get("origin_results")
    result_required = {
        "origin_id",
        "candidate_index",
        "oracle_sha256",
        "valid",
        "behavior_sha256",
        "error",
    }
    if not isinstance(results, list) or len(results) != len(origins):
        raise RuntimeError("final oracle quorum origin result count is invalid")
    valid: list[dict[str, Any]] = []
    for origin, result in zip(origins, results):
        if not isinstance(result, dict) or set(result) != result_required:
            raise RuntimeError("final oracle quorum contains an invalid origin result")
        if any(
            result.get(key) != origin.get(key)
            for key in ("origin_id", "candidate_index", "oracle_sha256")
        ):
            raise RuntimeError("final oracle quorum origin result is misbound")
        is_valid = result.get("valid")
        behavior = result.get("behavior_sha256")
        error = result.get("error")
        if not isinstance(is_valid, bool):
            raise RuntimeError("final oracle quorum origin validity is invalid")
        if is_valid:
            if not isinstance(behavior, str) or len(behavior) != 64 or error is not None:
                raise RuntimeError("final oracle quorum valid result is malformed")
            valid.append(
                {
                    "origin_id": result["origin_id"],
                    "candidate_index": result["candidate_index"],
                    "behavior_sha256": behavior,
                }
            )
        elif behavior is not None or not isinstance(error, str) or not error:
            raise RuntimeError("final oracle quorum failed result is malformed")
    consensus = _candidate_quorum(valid)
    expected_ids = [row["origin_id"] for row in consensus]
    if (
        evidence.get("valid_candidate_count") != len(valid)
        or evidence.get("consensus_count") != len(consensus)
        or evidence.get("quorum_origin_ids") != expected_ids
        or consensus[0]["behavior_sha256"] != selected_behavior
    ):
        raise RuntimeError("final oracle quorum summary disagrees with origin results")


def revalidate_oracle_bundle_quorum(
    spec: DesignSpec,
    final_plan: dict[str, Any],
    selected_code: str,
    origin_bundle: dict[str, Any],
    artifact_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay every frozen candidate origin against a complete active suite."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    origins = validate_oracle_origin_bundle(origin_bundle)
    valid: list[dict[str, Any]] = []
    origin_results: list[dict[str, Any]] = []
    for origin in origins:
        origin_id = origin["origin_id"]
        candidate_index = origin["candidate_index"]
        code = origin["oracle"]
        result = {
            "origin_id": origin_id,
            "candidate_index": candidate_index,
            "oracle_sha256": origin["oracle_sha256"],
            "valid": False,
            "behavior_sha256": None,
            "error": None,
        }
        try:
            check_oracle_policy(code)
            trace = generate_expected_trace(
                spec,
                code,
                final_plan,
                artifact_dir / origin_id,
            )
            behavior = _trace_behavior_hash(trace)
            result.update(
                {"valid": True, "behavior_sha256": behavior, "error": None}
            )
            valid.append(
                {
                    "origin_id": origin_id,
                    "candidate_index": candidate_index,
                    "behavior_sha256": behavior,
                }
            )
        except NonRepairableInfrastructureError:
            raise
        except Exception as exc:
            result["error"] = str(exc)[:2000] or type(exc).__name__
        origin_results.append(result)

    consensus = _candidate_quorum(valid)
    enforce_oracle_diversity(
        spec,
        origins,
        quorum_origin_ids=[str(row["origin_id"]) for row in consensus],
    )
    check_oracle_policy(selected_code)
    selected_trace = generate_expected_trace(
        spec,
        selected_code,
        final_plan,
        artifact_dir / "selected",
    )
    selected_behavior = _trace_behavior_hash(selected_trace)
    winning_behavior = str(consensus[0]["behavior_sha256"])
    if selected_behavior != winning_behavior:
        raise RuntimeError(
            "selected Python oracle disagrees with candidate quorum on the final test plan"
        )
    final_metadata = {
        "schema_version": QUORUM_EVIDENCE_SCHEMA_VERSION,
        "quorum_met": True,
        "reviewer_counted": False,
        "final_testplan_sha256": content_hash(final_plan),
        "selected_oracle_sha256": content_hash(selected_code),
        "selected_expected_trace_sha256": content_hash(selected_trace),
        "consensus_behavior_sha256": winning_behavior,
        "consensus_count": len(consensus),
        "quorum_origin_ids": [row["origin_id"] for row in consensus],
        "valid_candidate_count": len(valid),
        "origin_bundle_sha256": content_hash(origin_bundle),
        "origin_results": origin_results,
    }
    validate_oracle_quorum_evidence(
        final_metadata,
        final_plan,
        selected_code,
        selected_trace,
        origin_bundle,
    )
    (artifact_dir / "final-quorum.json").write_text(
        json.dumps(final_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return selected_trace, final_metadata


def revalidate_oracle_quorum(
    spec: DesignSpec,
    final_plan: dict[str, Any],
    selected_code: str,
    oracle_metadata: dict[str, Any],
    candidate_artifact_dir: Path,
    artifact_dir: Path,
    *,
    origin_bundle: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-establish candidate-only execution consensus on the final test plan."""
    bundle = origin_bundle or build_oracle_origin_bundle(
        oracle_metadata, candidate_artifact_dir
    )
    return revalidate_oracle_bundle_quorum(
        spec,
        final_plan,
        selected_code,
        bundle,
        artifact_dir,
    )
