"""Deterministic, tool-verified physical planning for managed Lane runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .classic import CLASSIC_REGISTRY_VERSION, LIBRELANE_VERSION
from .integrity import canonical_regular_file


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
PHYSICAL_PLAN_SCHEMA_VERSION = 2
PHYSICAL_POLICY_SCHEMA_VERSION = 2
GEOMETRY_LOCK_DOCUMENT_SCHEMA_VERSION = 2
GEOMETRY_KEYS = frozenset({"FP_SIZING", "DIE_AREA", "CORE_AREA"})
SOURCE_POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "physical-planning-policy-bootstrap-v2.json"
)


def default_policy_path() -> Path:
    candidates = (
        SOURCE_POLICY_PATH,
        Path(sys.prefix)
        / "share/pi-lane/specs/physical-planning-policy-bootstrap-v2.json",
        Path(
            "/usr/local/share/pi-lane/specs/physical-planning-policy-bootstrap-v2.json"
        ),
        Path("/usr/share/pi-lane/specs/physical-planning-policy-bootstrap-v2.json"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("the bundled physical planning policy is unavailable")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# The candidate domain is sealed before any tool probes run.  Probe context and
# evidence are bound elsewhere in the plan, so they must never affect this hash.
_CANDIDATE_DOMAIN_FIELDS = (
    "candidate_id",
    "core_width_um",
    "core_height_um",
    "core_area_um2",
    "aspect_ratio",
    "requested_core_area",
    "requested_die_area",
    "scale_factor",
    "reference_source_plan_sha256",
    "reference_geometry_lock_sha256",
    "expansion_parent_id",
    "expansion_index",
    "expansion_reason",
)


def candidate_domain_projection(
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the immutable candidate geometry used for candidate-domain binding."""

    domain: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, Mapping):
            raise ValueError("physical planning candidate evidence is malformed")
        domain.append(
            {key: row[key] for key in _CANDIDATE_DOMAIN_FIELDS if key in row}
        )
    return domain


def _finite_number(value: Any, *, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum):
        raise ValueError(f"{label} is outside its allowed range")
    return parsed


def _positive_int(value: Any, *, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{label} must be an integer from 1 to {maximum}")
    return value


def _area(value: Any, *, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must contain four coordinates")
    parsed = [_finite_number(item, label=label, minimum=0) for item in value]
    if parsed[0] >= parsed[2] or parsed[1] >= parsed[3]:
        raise ValueError(f"{label} has a non-positive width or height")
    return parsed


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = (path or default_policy_path()).expanduser().resolve(strict=True)
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("physical planning policy must be a JSON object")
    validate_policy(raw)
    return raw


def _nearest_rank_percentile(values: Iterable[float], percentile: int) -> float:
    ordered = sorted(
        _finite_number(value, label="calibration ratio", minimum=0.001)
        for value in values
    )
    if not ordered:
        raise ValueError("physical planning calibration group is empty")
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def calibrate_policy(
    bootstrap: Mapping[str, Any], evidence: Mapping[str, Any], *, policy_id: str
) -> dict[str, Any]:
    """Create a publishable policy from complete Human reference implementations."""

    validate_policy(bootstrap)
    if bootstrap["status"] != "bootstrap" or bootstrap["publishable"] is not False:
        raise ValueError("physical planning calibration requires a bootstrap policy")
    if not isinstance(policy_id, str) or PLAN_ID_RE.fullmatch(policy_id) is None:
        raise ValueError("calibrated physical planning policy_id is malformed")
    required = {
        "schema_version",
        "kind",
        "pdk",
        "pdk_revision",
        "pdk_integrity_sha256",
        "image_digest",
        "librelane_version",
        "flow_registry_version",
        "references",
    }
    if (
        set(evidence) != required
        or evidence["schema_version"] != 1
        or evidence["kind"] != "pi-lane-human-planning-calibration"
    ):
        raise ValueError("physical planning calibration evidence identity is invalid")
    if (
        evidence["pdk"] != bootstrap["pdk"]
        or evidence["librelane_version"] != bootstrap["librelane_version"]
        or evidence["flow_registry_version"] != bootstrap["flow_registry_version"]
    ):
        raise ValueError("physical planning calibration tool/PDK identity drifted")
    if (
        not isinstance(evidence["pdk_integrity_sha256"], str)
        or HASH_RE.fullmatch(evidence["pdk_integrity_sha256"]) is None
    ):
        raise ValueError("physical planning calibration PDK integrity is malformed")
    if (
        not isinstance(evidence["image_digest"], str)
        or DIGEST_RE.fullmatch(evidence["image_digest"]) is None
    ):
        raise ValueError("physical planning calibration image digest is malformed")
    references = evidence["references"]
    minimum = int(bootstrap["calibration"]["minimum_complete_human_references"])
    if not isinstance(references, list) or len(references) < minimum:
        raise ValueError(
            f"physical planning calibration requires at least {minimum} Human references"
        )
    reference_fields = {
        "case_id",
        "complete",
        "delivery_gate",
        "clocked",
        "mapped_area_um2",
        "final_cell_area_um2",
        "plan_sha256",
        "gds_sha256",
    }
    case_ids: set[str] = set()
    groups: dict[bool, list[float]] = {False: [], True: []}
    for row in references:
        if not isinstance(row, Mapping) or set(row) != reference_fields:
            raise ValueError("physical planning calibration reference is malformed")
        case_id = row["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise ValueError(
                "physical planning calibration case identities are invalid"
            )
        case_ids.add(case_id)
        if (
            row["complete"] is not True
            or row["delivery_gate"] != "pass"
            or type(row["clocked"]) is not bool
        ):
            raise ValueError(
                "physical planning calibration contains an incomplete Human reference"
            )
        for key in ("plan_sha256", "gds_sha256"):
            if not isinstance(row[key], str) or HASH_RE.fullmatch(row[key]) is None:
                raise ValueError(
                    f"physical planning calibration reference {key} is malformed"
                )
        mapped = _finite_number(
            row["mapped_area_um2"], label="mapped area", minimum=0.001
        )
        final = _finite_number(
            row["final_cell_area_um2"], label="final cell area", minimum=0.001
        )
        groups[row["clocked"]].append(final / mapped)
    if any(len(values) < 3 for values in groups.values()):
        raise ValueError(
            "physical planning calibration requires at least three references per clock class"
        )
    percentile_name = str(bootstrap["calibration"]["growth_percentile"])
    percentile = int(percentile_name.removeprefix("p"))
    output = json.loads(json.dumps(bootstrap))
    output["policy_id"] = policy_id
    output["status"] = "calibrated"
    output["publishable"] = True
    output["growth_reserve"] = {
        "combinational_multiplier": round(
            max(1.0, _nearest_rank_percentile(groups[False], percentile)), 9
        ),
        "clocked_multiplier": round(
            max(1.0, _nearest_rank_percentile(groups[True], percentile)), 9
        ),
        "source": f"human-reference-{percentile_name}-v1",
    }
    output["calibration"] = {
        **output["calibration"],
        "calibration_evidence_sha256": canonical_sha256(evidence),
    }
    validate_policy(output)
    return output


def validate_policy(policy: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "policy_id",
        "status",
        "publishable",
        "pdk",
        "librelane_version",
        "flow_registry_version",
        "planning_utilization_pct",
        "maximum_post_probe_utilization_pct",
        "growth_reserve",
        "geometry",
        "search",
        "calibration",
    }
    if set(policy) != required:
        raise ValueError("physical planning policy fields are incomplete or unknown")
    if policy["schema_version"] != PHYSICAL_POLICY_SCHEMA_VERSION:
        raise ValueError("physical planning policy schema is unsupported")
    if (
        not isinstance(policy["policy_id"], str)
        or PLAN_ID_RE.fullmatch(policy["policy_id"]) is None
    ):
        raise ValueError("physical planning policy_id is malformed")
    if (
        policy["status"] not in {"bootstrap", "calibrated"}
        or type(policy["publishable"]) is not bool
    ):
        raise ValueError("physical planning policy status is malformed")
    if policy["publishable"] and policy["status"] != "calibrated":
        raise ValueError(
            "only calibrated physical planning policies may be publishable"
        )
    if policy["pdk"] not in {"sky130A", "gf180mcuD", "ihp-sg13g2"}:
        raise ValueError("physical planning policy PDK is unsupported")
    if (
        policy["librelane_version"] != LIBRELANE_VERSION
        or policy["flow_registry_version"] != CLASSIC_REGISTRY_VERSION
    ):
        raise ValueError("physical planning policy tool identity is incompatible")
    planning_util = _finite_number(
        policy["planning_utilization_pct"], label="planning utilization", minimum=1
    )
    maximum_util = _finite_number(
        policy["maximum_post_probe_utilization_pct"],
        label="maximum post-probe utilization",
        minimum=1,
    )
    if planning_util > 80 or not planning_util < maximum_util <= 95:
        raise ValueError("physical planning utilization bounds are invalid")
    growth = policy["growth_reserve"]
    if not isinstance(growth, Mapping) or set(growth) != {
        "combinational_multiplier",
        "clocked_multiplier",
        "source",
    }:
        raise ValueError("physical planning growth reserve is malformed")
    for key in ("combinational_multiplier", "clocked_multiplier"):
        if _finite_number(growth[key], label=key, minimum=1) > 10:
            raise ValueError("physical planning growth multiplier is unreasonable")
    if not isinstance(growth["source"], str) or not growth["source"]:
        raise ValueError("physical planning growth source is missing")
    geometry = policy["geometry"]
    geometry_fields = {
        "minimum_core_width_um",
        "minimum_core_height_um",
        "core_margin_um",
        "width_quantum_um",
        "height_quantum_um",
        "aspect_ratios",
        "scale_factors",
        "heuristic_io_pin_pitch_um",
        "heuristic_io_corner_keepout_um",
        "io_headroom_fixed_slots",
        "io_headroom_percent",
    }
    if not isinstance(geometry, Mapping) or set(geometry) != geometry_fields:
        raise ValueError("physical planning geometry policy is malformed")
    for key in geometry_fields - {"aspect_ratios", "scale_factors", "io_headroom_fixed_slots"}:
        _finite_number(geometry[key], label=key, minimum=0.001)
    _positive_int(
        geometry["io_headroom_fixed_slots"],
        label="I/O headroom fixed slots",
        maximum=1024,
    )
    if _finite_number(
        geometry["io_headroom_percent"], label="I/O headroom percent", minimum=0
    ) > 100:
        raise ValueError("physical planning I/O headroom percent is unreasonable")
    for key in ("aspect_ratios", "scale_factors"):
        values = geometry[key]
        if not isinstance(values, list) or not values:
            raise ValueError(f"physical planning {key} must be non-empty")
        parsed = [_finite_number(item, label=key, minimum=0.05) for item in values]
        if len(set(parsed)) != len(parsed):
            raise ValueError(f"physical planning {key} must be unique")
    if min(float(item) for item in geometry["scale_factors"]) != 1:
        raise ValueError("physical planning scale_factors must start at 1")
    search = policy["search"]
    search_fields = {
        "maximum_candidates",
        "pdn_probe_to",
        "io_preflight_to",
        "first_probe_to",
        "robust_probe_to",
        "candidate_timeout_seconds",
        "poll_interval_seconds",
    }
    optional_search_fields = {
        "maximum_total_candidates",
        "maximum_adaptive_expansions",
        "adaptive_max_dimension_um",
        "minimum_midpnr_setup_wns_ns",
    }
    if (
        not isinstance(search, Mapping)
        or not search_fields.issubset(search)
        or set(search) - search_fields - optional_search_fields
    ):
        raise ValueError("physical planning search policy is malformed")
    _positive_int(
        search["maximum_candidates"], label="maximum candidates", maximum=256
    )
    total_candidates = search.get("maximum_total_candidates", search["maximum_candidates"])
    _positive_int(
        total_candidates, label="maximum total candidates", maximum=256
    )
    if int(total_candidates) < int(search["maximum_candidates"]):
        raise ValueError("maximum total candidates cannot be below the seed domain")
    adaptive_expansions = search.get("maximum_adaptive_expansions", 0)
    if (
        isinstance(adaptive_expansions, bool)
        or not isinstance(adaptive_expansions, int)
        or not 0 <= adaptive_expansions <= 256
    ):
        raise ValueError("maximum adaptive expansions is invalid")
    if "adaptive_max_dimension_um" in search:
        _finite_number(
            search["adaptive_max_dimension_um"],
            label="adaptive maximum dimension",
            minimum=0.001,
        )
    if "minimum_midpnr_setup_wns_ns" in search:
        minimum_midpnr_setup_wns = _finite_number(
            search["minimum_midpnr_setup_wns_ns"],
            label="minimum mid-PnR setup WNS",
            minimum=0,
        )
        if minimum_midpnr_setup_wns > 100:
            raise ValueError("minimum mid-PnR setup WNS is unreasonable")
    if (
        search["pdn_probe_to"] != "OpenROAD.GeneratePDN"
        or search["io_preflight_to"] != "Odb.CustomIOPlacement"
        or
        search["first_probe_to"] != "OpenROAD.DetailedPlacement"
        or search["robust_probe_to"] != "OpenROAD.STAMidPNR-3"
    ):
        raise ValueError("physical planning probe endpoints are incompatible")
    _positive_int(
        search["candidate_timeout_seconds"], label="candidate timeout", maximum=86400
    )
    _positive_int(search["poll_interval_seconds"], label="poll interval", maximum=300)
    calibration = policy["calibration"]
    calibration_fields = {
        "required_for_publishable",
        "minimum_complete_human_references",
        "growth_percentile",
        "calibration_evidence_sha256",
    }
    if not isinstance(calibration, Mapping) or set(calibration) != calibration_fields:
        raise ValueError("physical planning calibration policy is malformed")
    if calibration["required_for_publishable"] is not True:
        raise ValueError("physical planning calibration must be fail-closed")
    _positive_int(
        calibration["minimum_complete_human_references"],
        label="minimum calibration references",
        maximum=100000,
    )
    if calibration["growth_percentile"] not in {"p90", "p95", "p99"}:
        raise ValueError("physical planning growth percentile is unsupported")
    evidence = calibration["calibration_evidence_sha256"]
    if policy["publishable"]:
        if not isinstance(evidence, str) or HASH_RE.fullmatch(evidence) is None:
            raise ValueError("publishable planning policy lacks calibration evidence")
    elif evidence is not None and (
        not isinstance(evidence, str) or HASH_RE.fullmatch(evidence) is None
    ):
        raise ValueError("physical planning calibration evidence hash is malformed")


@dataclass(frozen=True)
class PlanningInputs:
    mapped_area_um2: float
    port_bits: int
    side_pin_bits: Mapping[str, int]
    clocked: bool


def _ceil_quantum(value: float, quantum: float) -> float:
    return round(math.ceil((value - 1e-12) / quantum) * quantum, 9)


def derive_lower_bounds(
    inputs: PlanningInputs, policy: Mapping[str, Any]
) -> dict[str, Any]:
    if inputs.mapped_area_um2 <= 0 or not math.isfinite(inputs.mapped_area_um2):
        raise ValueError("mapped synthesis area must be positive and finite")
    if isinstance(inputs.port_bits, bool) or inputs.port_bits < 0:
        raise ValueError("port bit count is malformed")
    geometry = policy["geometry"]
    growth_key = "clocked_multiplier" if inputs.clocked else "combinational_multiplier"
    growth_multiplier = float(policy["growth_reserve"][growth_key])
    planned_cell_area = inputs.mapped_area_um2 * growth_multiplier
    capacity_area = planned_cell_area / (
        float(policy["planning_utilization_pct"]) / 100.0
    )
    base_width = math.sqrt(capacity_area)
    base_height = base_width
    # This is only a bounded-domain seed.  It is never an I/O feasibility
    # verdict: that verdict comes from the pinned CustomIOPlacement Step.
    pitch = float(geometry["heuristic_io_pin_pitch_um"])
    keepout = 2 * float(geometry["heuristic_io_corner_keepout_um"])
    side = {key: int(inputs.side_pin_bits.get(key, 0)) for key in "NSEW"}
    io_width = max(side["N"], side["S"]) * pitch + keepout
    io_height = max(side["E"], side["W"]) * pitch + keepout
    width = max(base_width, io_width, float(geometry["minimum_core_width_um"]))
    height = max(base_height, io_height, float(geometry["minimum_core_height_um"]))
    width = _ceil_quantum(width, float(geometry["width_quantum_um"]))
    height = _ceil_quantum(height, float(geometry["height_quantum_um"]))
    dominant = "logic"
    if io_width > base_width or io_height > base_height:
        dominant = "io"
    if width == float(geometry["minimum_core_width_um"]) or height == float(
        geometry["minimum_core_height_um"]
    ):
        dominant = "grid" if dominant == "logic" else "mixed"
    return {
        "mapped_area_um2": inputs.mapped_area_um2,
        "growth_multiplier": growth_multiplier,
        "planned_cell_area_um2": planned_cell_area,
        "capacity_area_um2": capacity_area,
        "port_bits": inputs.port_bits,
        "side_pin_bits": side,
        "logic_width_um": base_width,
        "logic_height_um": base_height,
        "io_width_um": io_width,
        "io_height_um": io_height,
        "minimum_core_width_um": width,
        "minimum_core_height_um": height,
        "dominant_constraint_estimate": dominant,
        "io_seed_estimate": {
            "status": "heuristic_only",
            "pin_pitch_um": pitch,
            "corner_keepout_um": keepout / 2,
        },
    }


def generate_candidates(
    bounds: Mapping[str, Any], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    geometry = policy["geometry"]
    margin = float(geometry["core_margin_um"])
    width_quantum = float(geometry["width_quantum_um"])
    height_quantum = float(geometry["height_quantum_um"])
    minimum_width = float(bounds["minimum_core_width_um"])
    minimum_height = float(bounds["minimum_core_height_um"])
    minimum_area = minimum_width * minimum_height
    candidates: dict[tuple[float, float], dict[str, Any]] = {}
    for scale in geometry["scale_factors"]:
        target_area = minimum_area * float(scale)
        for aspect in geometry["aspect_ratios"]:
            ratio = float(aspect)
            width = max(minimum_width, math.sqrt(target_area / ratio))
            height = max(minimum_height, width * ratio)
            width = _ceil_quantum(width, width_quantum)
            height = _ceil_quantum(height, height_quantum)
            key = (width, height)
            core = [margin, margin, margin + width, margin + height]
            die = [0.0, 0.0, width + 2 * margin, height + 2 * margin]
            candidates[key] = {
                "candidate_id": "candidate-pending",
                "core_width_um": width,
                "core_height_um": height,
                "core_area_um2": width * height,
                "aspect_ratio": height / width,
                "requested_core_area": core,
                "requested_die_area": die,
                "scale_factor": float(scale),
            }
    ordered = sorted(
        candidates.values(),
        key=lambda row: (
            row["core_area_um2"],
            abs(math.log(row["aspect_ratio"])),
            row["core_width_um"],
            row["core_height_um"],
        ),
    )[: int(policy["search"]["maximum_candidates"])]
    for index, row in enumerate(ordered, start=1):
        row["candidate_id"] = f"candidate-{index:03d}"
    return ordered


def select_minimum_robust_candidate(
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, Mapping):
            raise ValueError("physical planning candidate evidence is malformed")
        rows.append(dict(row))
    passing = [
        row
        for row in rows
        if row.get("pdn_gate") == "pass"
        and row.get("io_preflight_gate") == "pass"
        and row.get("detailed_placement_gate") == "pass"
        and row.get("robustness_gate") == "pass"
        and row.get("timing_qualification_gate", "pass") == "pass"
    ]
    if not passing:
        raise ValueError("no physical planning candidate passed all robustness gates")
    return min(
        passing,
        key=lambda row: (
            _finite_number(
                row.get("core_area_um2"), label="candidate core area", minimum=0.001
            ),
            (
                _finite_number(
                    row["congestion_score"],
                    label="candidate congestion",
                    minimum=0,
                )
                if row.get("congestion_score") is not None
                else math.inf
            ),
            abs(
                math.log(
                    _finite_number(
                        row.get("aspect_ratio"),
                        label="candidate aspect ratio",
                        minimum=0.001,
                    )
                )
            ),
            str(row.get("candidate_id")),
        ),
    )


def minimum_geometry_classification(
    selected: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    candidate_domain_sha256: str,
    search_complete: bool,
) -> dict[str, Any]:
    """Classify density only from sealed, tool-backed candidate evidence."""

    if HASH_RE.fullmatch(candidate_domain_sha256) is None:
        raise ValueError("physical planning candidate domain hash is malformed")
    rows: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, Mapping):
            raise ValueError("physical planning candidate evidence is malformed")
        rows.append(dict(row))
    selected_id = selected.get("candidate_id")
    selected_area = _finite_number(
        selected.get("core_area_um2"), label="selected core area", minimum=0.001
    )
    selected_is_minimum = False
    try:
        selected_is_minimum = (
            select_minimum_robust_candidate(rows).get("candidate_id") == selected_id
        )
    except ValueError:
        pass
    smaller = [
        row
        for row in rows
        if _finite_number(row.get("core_area_um2"), label="candidate core area", minimum=0.001)
        < selected_area - 1e-9
    ]
    gate_names = (
        "pdn_gate",
        "io_preflight_gate",
        "detailed_placement_gate",
        "robustness_gate",
        "timing_qualification_gate",
    )
    smaller_accounted = all(
        all(
            (
                row.get(gate, "pass")
                if gate == "timing_qualification_gate"
                else row.get(gate)
            )
            in {"pass", "fail", "not_run"}
            for gate in gate_names
        )
        and any(row.get(gate) == "fail" for gate in gate_names)
        for row in smaller
    )
    constraints: list[str] = []
    if any(row.get("pdn_gate") == "fail" for row in smaller):
        constraints.append("pdn")
    if any(row.get("io_preflight_gate") == "fail" for row in smaller):
        constraints.append("io")
    if any(row.get("timing_qualification_gate") == "fail" for row in smaller):
        constraints.append("timing")
    if not smaller:
        constraints.append("grid")
    utilization = selected.get("post_probe_utilization_pct")
    metrics_valid = (
        isinstance(utilization, (int, float))
        and not isinstance(utilization, bool)
        and math.isfinite(float(utilization))
        and 0 <= float(utilization) <= 100
    )
    if not search_complete or not selected_is_minimum or not smaller_accounted:
        status = "minimum_geometry_unproven"
    elif constraints and metrics_valid:
        status = "minimum_geometry_dominated"
    else:
        status = "not_minimum_geometry_dominated"
    dominant = "mixed" if len(constraints) > 1 else (constraints[0] if constraints else "logic")
    return {
        "status": status,
        "candidate_domain_sha256": candidate_domain_sha256,
        "search_complete": search_complete,
        "selected_is_minimum_robust": selected_is_minimum,
        "smaller_candidates_accounted": smaller_accounted,
        "metric_consistency": "valid" if metrics_valid else "unavailable",
        "tool_constraints": constraints,
        "dominant_constraint": dominant,
    }


def geometry_lock(
    selected: Mapping[str, Any], *, classification: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(classification, Mapping):
        raise ValueError("physical planning geometry classification is malformed")
    classification = dict(classification)
    if classification.get("status") not in {
        "minimum_geometry_dominated",
        "minimum_geometry_unproven",
        "not_minimum_geometry_dominated",
    }:
        raise ValueError("physical planning minimum geometry status is invalid")
    dominant_constraint = classification.get("dominant_constraint")
    if dominant_constraint not in {
        "logic", "pdn", "io", "macro", "grid", "timing", "mixed"
    }:
        raise ValueError("physical planning dominant constraint is invalid")
    requested_die = _area(
        selected.get("requested_die_area"), label="requested die area"
    )
    requested_core = _area(
        selected.get("requested_core_area"), label="requested core area"
    )
    observed_die = _area(
        selected.get("observed_die_bbox", requested_die), label="observed die bbox"
    )
    observed_core = _area(
        selected.get("observed_core_bbox", requested_core), label="observed core bbox"
    )
    return {
        "schema_version": 2,
        "floorplan_sizing": "absolute",
        "die_area": requested_die,
        "core_area": requested_core,
        "observed_die_bbox": observed_die,
        "observed_core_bbox": observed_core,
        "dominant_constraint": dominant_constraint,
        "minimum_geometry_classification": classification,
        "selection_rule": "sealed-domain-timing-qualified-minimum-robust-v3",
    }


def finalize_plan(body: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(body)
    value["schema_version"] = PHYSICAL_PLAN_SCHEMA_VERSION
    value["kind"] = "pi-lane-physical-plan"
    value["status"] = "completed"
    lock = value.get("geometry_lock")
    if not isinstance(lock, Mapping):
        raise ValueError("physical plan lacks a geometry lock")
    value["geometry_lock_sha256"] = canonical_sha256(lock)
    value.pop("plan_sha256", None)
    value["plan_sha256"] = canonical_sha256(value)
    validate_plan(value)
    return value


def validate_plan(plan: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "status",
        "publishable",
        "plan_id",
        "created_at",
        "config_sha256",
        "input_bundle_sha256",
        "pdk",
        "pdk_revision",
        "pdk_integrity_sha256",
        "image_digest",
        "librelane_version",
        "flow_registry_version",
        "policy",
        "policy_sha256",
        "synthesis_baseline",
        "candidates",
        "candidate_domain_sha256",
        "candidate_domain_count",
        "search_complete",
        "selected_candidate_id",
        "io_lock_sha256",
        "geometry_lock",
        "geometry_lock_sha256",
        "plan_sha256",
    }
    if set(plan) != required:
        raise ValueError("physical plan fields are incomplete or unknown")
    if (
        plan["schema_version"] != PHYSICAL_PLAN_SCHEMA_VERSION
        or plan["kind"] != "pi-lane-physical-plan"
        or plan["status"] != "completed"
    ):
        raise ValueError("physical plan identity is invalid")
    if type(plan["publishable"]) is not bool or not isinstance(plan["created_at"], str):
        raise ValueError("physical plan publication metadata is malformed")
    if (
        not isinstance(plan["plan_id"], str)
        or PLAN_ID_RE.fullmatch(plan["plan_id"]) is None
    ):
        raise ValueError("physical plan ID is malformed")
    for key in (
        "config_sha256",
        "input_bundle_sha256",
        "pdk_integrity_sha256",
        "policy_sha256",
        "io_lock_sha256",
        "geometry_lock_sha256",
        "plan_sha256",
    ):
        if not isinstance(plan[key], str) or HASH_RE.fullmatch(plan[key]) is None:
            raise ValueError(f"physical plan {key} is malformed")
    if (
        not isinstance(plan["candidate_domain_count"], int)
        or isinstance(plan["candidate_domain_count"], bool)
        or plan["candidate_domain_count"] < 1
        or type(plan["search_complete"]) is not bool
    ):
        raise ValueError("physical plan candidate-domain metadata is malformed")
    if (
        not isinstance(plan["image_digest"], str)
        or DIGEST_RE.fullmatch(plan["image_digest"]) is None
    ):
        raise ValueError("physical plan image digest is malformed")
    if (
        plan["librelane_version"] != LIBRELANE_VERSION
        or plan["flow_registry_version"] != CLASSIC_REGISTRY_VERSION
    ):
        raise ValueError("physical plan tool identity is incompatible")
    policy = plan["policy"]
    if not isinstance(policy, Mapping):
        raise ValueError("physical plan policy is malformed")
    validate_policy(policy)
    if (
        canonical_sha256(policy) != plan["policy_sha256"]
        or bool(policy["publishable"]) != plan["publishable"]
    ):
        raise ValueError("physical plan policy binding is invalid")
    lock = plan["geometry_lock"]
    if (
        not isinstance(lock, Mapping)
        or canonical_sha256(lock) != plan["geometry_lock_sha256"]
    ):
        raise ValueError("physical plan geometry lock binding is invalid")
    expected_lock_fields = {
        "schema_version",
        "floorplan_sizing",
        "die_area",
        "core_area",
        "observed_die_bbox",
        "observed_core_bbox",
        "dominant_constraint",
        "minimum_geometry_classification",
        "selection_rule",
    }
    if set(lock) != expected_lock_fields:
        raise ValueError("physical plan geometry lock fields are incomplete or unknown")
    if lock["schema_version"] != 2 or lock["floorplan_sizing"] != "absolute":
        raise ValueError("physical plan geometry lock identity is invalid")
    if lock["selection_rule"] not in {
        "sealed-domain-minimum-robust-v2",
        "sealed-domain-timing-qualified-minimum-robust-v3",
    }:
        raise ValueError("physical plan selection rule is unsupported")
    if lock["dominant_constraint"] not in {
        "logic",
        "pdn",
        "io",
        "macro",
        "grid",
        "timing",
        "mixed",
    }:
        raise ValueError("physical plan dominant constraint is invalid")
    for key in ("die_area", "core_area", "observed_die_bbox", "observed_core_bbox"):
        _area(lock[key], label=f"physical plan {key}")
    candidates = plan["candidates"]
    if (
        not isinstance(candidates, list)
        or len(candidates) != plan["candidate_domain_count"]
        or not candidates
        or any(not isinstance(row, Mapping) for row in candidates)
    ):
        raise ValueError("physical plan candidate evidence is missing")
    domain = candidate_domain_projection(candidates)
    if canonical_sha256(domain) != plan["candidate_domain_sha256"]:
        raise ValueError("physical plan candidate domain binding is invalid")
    selected = [
        row
        for row in candidates
        if isinstance(row, Mapping)
        and row.get("candidate_id") == plan["selected_candidate_id"]
    ]
    if (
        len(selected) != 1
        or selected[0].get("pdn_gate") != "pass"
        or selected[0].get("io_preflight_gate") != "pass"
        or selected[0].get("detailed_placement_gate") != "pass"
        or selected[0].get("robustness_gate") != "pass"
    ):
        raise ValueError("physical plan selected candidate is not robustly passing")
    reference_lock_sha256 = selected[0].get("reference_geometry_lock_sha256")
    if reference_lock_sha256 is not None:
        if reference_lock_sha256 != plan["geometry_lock_sha256"]:
            raise ValueError(
                "reference-locked candidate does not match its Human geometry lock"
            )
    else:
        classification = minimum_geometry_classification(
            selected[0],
            candidates,
            candidate_domain_sha256=plan["candidate_domain_sha256"],
            search_complete=plan["search_complete"],
        )
        if geometry_lock(selected[0], classification=classification) != lock:
            raise ValueError(
                "physical plan selected candidate does not match its geometry lock"
            )
    without_hash = dict(plan)
    observed_hash = without_hash.pop("plan_sha256")
    if canonical_sha256(without_hash) != observed_hash:
        raise ValueError("physical plan content hash is invalid")


def load_plan(path: Path) -> dict[str, Any]:
    resolved = canonical_regular_file(
        path.expanduser().resolve(),
        root=path.expanduser().resolve().parent,
        label="physical plan",
        minimum_size=2,
        maximum_size=16 * 1024 * 1024,
    )
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("physical plan must be a JSON object")
    validate_plan(value)
    return value


def export_geometry_lock_document(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Project a completed plan to the only Human evidence an LLM arm may reuse."""

    validate_plan(plan)
    body = {
        "schema_version": GEOMETRY_LOCK_DOCUMENT_SCHEMA_VERSION,
        "kind": "pi-lane-geometry-lock",
        "source_plan_id": plan["plan_id"],
        "source_plan_sha256": plan["plan_sha256"],
        "pdk": plan["pdk"],
        "pdk_revision": plan["pdk_revision"],
        "pdk_integrity_sha256": plan["pdk_integrity_sha256"],
        "image_digest": plan["image_digest"],
        "librelane_version": plan["librelane_version"],
        "flow_registry_version": plan["flow_registry_version"],
        "policy_id": plan["policy"]["policy_id"],
        "policy_sha256": plan["policy_sha256"],
        "publishable": plan["publishable"],
        "io_lock_sha256": plan["io_lock_sha256"],
        "geometry_lock": dict(plan["geometry_lock"]),
        "geometry_lock_sha256": plan["geometry_lock_sha256"],
    }
    return {**body, "document_sha256": canonical_sha256(body)}


def validate_geometry_lock_document(document: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "source_plan_id",
        "source_plan_sha256",
        "pdk",
        "pdk_revision",
        "pdk_integrity_sha256",
        "image_digest",
        "librelane_version",
        "flow_registry_version",
        "policy_id",
        "policy_sha256",
        "publishable",
        "io_lock_sha256",
        "geometry_lock",
        "geometry_lock_sha256",
        "document_sha256",
    }
    if set(document) != required:
        raise ValueError("geometry lock document fields are incomplete or unknown")
    if (
        document["schema_version"] != GEOMETRY_LOCK_DOCUMENT_SCHEMA_VERSION
        or document["kind"] != "pi-lane-geometry-lock"
    ):
        raise ValueError("geometry lock document identity is invalid")
    if (
        not isinstance(document["source_plan_id"], str)
        or PLAN_ID_RE.fullmatch(document["source_plan_id"]) is None
    ):
        raise ValueError("geometry lock source plan ID is malformed")
    for key in (
        "source_plan_sha256",
        "pdk_integrity_sha256",
        "policy_sha256",
        "io_lock_sha256",
        "geometry_lock_sha256",
        "document_sha256",
    ):
        if (
            not isinstance(document[key], str)
            or HASH_RE.fullmatch(document[key]) is None
        ):
            raise ValueError(f"geometry lock {key} is malformed")
    if (
        not isinstance(document["image_digest"], str)
        or DIGEST_RE.fullmatch(document["image_digest"]) is None
    ):
        raise ValueError("geometry lock image digest is malformed")
    if (
        document["librelane_version"] != LIBRELANE_VERSION
        or document["flow_registry_version"] != CLASSIC_REGISTRY_VERSION
    ):
        raise ValueError("geometry lock tool identity is incompatible")
    if type(document["publishable"]) is not bool:
        raise ValueError("geometry lock publishable flag is malformed")
    lock = document["geometry_lock"]
    if not isinstance(lock, Mapping):
        raise ValueError("geometry lock payload is malformed")
    expected_lock_fields = {
        "schema_version",
        "floorplan_sizing",
        "die_area",
        "core_area",
        "observed_die_bbox",
        "observed_core_bbox",
        "dominant_constraint",
        "minimum_geometry_classification",
        "selection_rule",
    }
    if (
        set(lock) != expected_lock_fields
        or canonical_sha256(lock) != document["geometry_lock_sha256"]
    ):
        raise ValueError("geometry lock payload binding is invalid")
    if lock["schema_version"] != 2 or lock["floorplan_sizing"] != "absolute":
        raise ValueError("geometry lock payload identity is invalid")
    if lock["selection_rule"] not in {
        "sealed-domain-minimum-robust-v2",
        "sealed-domain-timing-qualified-minimum-robust-v3",
    }:
        raise ValueError("geometry lock selection rule is unsupported")
    if lock["dominant_constraint"] not in {
        "logic",
        "pdn",
        "io",
        "macro",
        "grid",
        "timing",
        "mixed",
    }:
        raise ValueError("geometry lock constraint classification is malformed")
    classification = lock["minimum_geometry_classification"]
    if not isinstance(classification, Mapping) or set(classification) != {
        "status",
        "candidate_domain_sha256",
        "search_complete",
        "selected_is_minimum_robust",
        "smaller_candidates_accounted",
        "metric_consistency",
        "tool_constraints",
        "dominant_constraint",
    }:
        raise ValueError("geometry lock minimum classification is malformed")
    if (
        classification["status"]
        not in {
            "minimum_geometry_dominated",
            "minimum_geometry_unproven",
            "not_minimum_geometry_dominated",
        }
        or HASH_RE.fullmatch(str(classification["candidate_domain_sha256"])) is None
        or type(classification["search_complete"]) is not bool
        or type(classification["selected_is_minimum_robust"]) is not bool
        or type(classification["smaller_candidates_accounted"]) is not bool
        or classification["metric_consistency"] not in {"valid", "unavailable"}
        or not isinstance(classification["tool_constraints"], list)
        or any(
            item not in {"pdn", "io", "grid", "timing"}
            for item in classification["tool_constraints"]
        )
        or classification["dominant_constraint"] != lock["dominant_constraint"]
    ):
        raise ValueError("geometry lock minimum classification is invalid")
    for key in ("die_area", "core_area", "observed_die_bbox", "observed_core_bbox"):
        _area(lock[key], label=f"geometry lock {key}")
    without_hash = dict(document)
    observed_hash = without_hash.pop("document_sha256")
    if canonical_sha256(without_hash) != observed_hash:
        raise ValueError("geometry lock document content hash is invalid")


def load_geometry_lock_document(path: Path) -> dict[str, Any]:
    resolved = canonical_regular_file(
        path.expanduser().resolve(),
        root=path.expanduser().resolve().parent,
        label="geometry lock document",
        minimum_size=2,
        maximum_size=1024 * 1024,
    )
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("geometry lock document must be a JSON object")
    validate_geometry_lock_document(value)
    return value


def plan_overrides(plan: Mapping[str, Any]) -> list[str]:
    validate_plan(plan)
    lock = plan["geometry_lock"]
    return [
        "FP_SIZING=absolute",
        "DIE_AREA=" + " ".join(format(value, "g") for value in lock["die_area"]),
        "CORE_AREA=" + " ".join(format(value, "g") for value in lock["core_area"]),
    ]


def validate_planning_overrides(values: Iterable[str]) -> list[str]:
    parsed: dict[str, Any] = {}
    for value in values:
        if (
            not isinstance(value, str)
            or "=" not in value
            or any(character in value for character in "\r\n\0")
        ):
            raise ValueError("physical planning override is malformed")
        key, encoded = value.split("=", 1)
        if key not in GEOMETRY_KEYS or key in parsed:
            raise ValueError(
                "planning probes may override only one copy of each geometry key"
            )
        encoded = encoded.strip()
        if key == "FP_SIZING":
            if encoded in {'"absolute"', "absolute"}:
                parsed[key] = "absolute"
                continue
            raise ValueError("physical planning override FP_SIZING must be absolute")
        try:
            parsed[key] = json.loads(encoded)
        except json.JSONDecodeError:
            parts = encoded.split()
            if len(parts) != 4:
                raise ValueError(
                    f"physical planning override {key} must contain four coordinates"
                ) from None
            parsed[key] = parts
    if set(parsed) != GEOMETRY_KEYS or parsed["FP_SIZING"] != "absolute":
        raise ValueError(
            "physical planning probe requires one complete absolute geometry"
        )
    parsed["DIE_AREA"] = _area(parsed["DIE_AREA"], label="planning die area")
    parsed["CORE_AREA"] = _area(parsed["CORE_AREA"], label="planning core area")
    return [
        "FP_SIZING=absolute",
        "DIE_AREA=" + " ".join(format(value, "g") for value in parsed["DIE_AREA"]),
        "CORE_AREA=" + " ".join(format(value, "g") for value in parsed["CORE_AREA"]),
    ]


def bind_plan_to_run(
    plan: Mapping[str, Any],
    *,
    config_sha256: str,
    input_bundle_sha256: str,
    pdk: str,
    pdk_revision: str,
    pdk_integrity_sha256: str,
    image_digest: str,
) -> dict[str, Any]:
    validate_plan(plan)
    expected = {
        "config_sha256": config_sha256,
        "input_bundle_sha256": input_bundle_sha256,
        "pdk": pdk,
        "pdk_revision": pdk_revision,
        "pdk_integrity_sha256": pdk_integrity_sha256,
        "image_digest": image_digest,
    }
    mismatches = [key for key, value in expected.items() if plan.get(key) != value]
    if mismatches:
        raise ValueError(
            "physical plan does not match run identity: " + ", ".join(mismatches)
        )
    return {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["plan_sha256"],
        "geometry_lock_sha256": plan["geometry_lock_sha256"],
        "io_lock_sha256": plan["io_lock_sha256"],
        "publishable": plan["publishable"],
        "policy_id": plan["policy"]["policy_id"],
        "policy_sha256": plan["policy_sha256"],
        "geometry_lock": dict(plan["geometry_lock"]),
    }


def parse_bbox(value: Any) -> list[float] | None:
    if isinstance(value, str):
        parts = value.split()
        if len(parts) != 4:
            return None
        try:
            return _area([float(part) for part in parts], label="reported bbox")
        except ValueError:
            return None
    if isinstance(value, list):
        try:
            return _area(value, label="reported bbox")
        except ValueError:
            return None
    return None


def minimum_geometry_density_qualification(
    plan: Mapping[str, Any] | None, metrics: Mapping[str, Any]
) -> dict[str, Any]:
    """Fail closed unless a final run still matches a proven minimum plan.

    This qualification is deliberately independent from the raw density
    thresholds.  It may explain a low-density result caused by a hard geometry
    floor, but it cannot repair missing evidence, a changed floorplan, or an
    unproven candidate search.
    """

    result: dict[str, Any] = {
        "gate": "not_applicable",
        "classification": "not_applicable",
        "reasons": [],
        "integrity_checks": {
            "plan": "not_applicable",
            "search": "not_applicable",
            "geometry": "not_applicable",
        },
    }
    if plan is None:
        result["reasons"] = ["no frozen physical plan is bound to this run"]
        return result
    try:
        validate_plan(plan)
    except (TypeError, ValueError) as exc:
        result.update(
            {
                "gate": "fail",
                "classification": "unavailable",
                "reasons": [f"physical plan is invalid: {exc}"],
                "integrity_checks": {
                    "plan": "fail",
                    "search": "unknown",
                    "geometry": "unknown",
                },
            }
        )
        return result

    lock = plan["geometry_lock"]
    classification = lock["minimum_geometry_classification"]
    status = str(classification.get("status"))
    result["classification"] = status
    result["integrity_checks"]["plan"] = "pass"
    if plan["search_complete"] is not True or status != "minimum_geometry_dominated":
        result.update(
            {
                "gate": "fail",
                "reasons": [
                    "physical planning did not prove a minimum-geometry-dominated "
                    "selected candidate"
                ],
                "integrity_checks": {
                    **result["integrity_checks"],
                    "search": "fail",
                    "geometry": "unknown",
                },
            }
        )
        return result

    result["integrity_checks"]["search"] = "pass"
    geometry_errors: list[str] = []
    for label, metric_key, expected_key in (
        ("die", "design__die__bbox", "observed_die_bbox"),
        ("core", "design__core__bbox", "observed_core_bbox"),
    ):
        observed = parse_bbox(metrics.get(metric_key))
        expected = parse_bbox(lock.get(expected_key))
        if observed is None or expected is None:
            geometry_errors.append(f"{label} bbox evidence is unavailable")
        elif any(abs(left - right) > 0.001 for left, right in zip(observed, expected)):
            geometry_errors.append(f"{label} bbox drifted from the frozen geometry")
    if geometry_errors:
        result.update(
            {
                "gate": "fail",
                "reasons": geometry_errors,
                "integrity_checks": {
                    **result["integrity_checks"],
                    "geometry": "fail",
                },
            }
        )
        return result
    result.update(
        {
            "gate": "pass",
            "reasons": [
                "sealed physical planning evidence proves that the final geometry "
                "is the minimum robust candidate"
            ],
            "integrity_checks": {
                **result["integrity_checks"],
                "geometry": "pass",
            },
        }
    )
    return result


def geometry_drift_errors(
    binding: Mapping[str, Any] | None, metrics: Mapping[str, Any]
) -> list[str]:
    if binding is None:
        return []
    lock = binding.get("geometry_lock")
    if not isinstance(lock, Mapping):
        return ["physical plan binding has no geometry lock"]
    errors: list[str] = []
    for label, metric_key, expected_key in (
        ("die", "design__die__bbox", "observed_die_bbox"),
        ("core", "design__core__bbox", "observed_core_bbox"),
    ):
        observed = parse_bbox(metrics.get(metric_key))
        expected = parse_bbox(lock.get(expected_key))
        if observed is None or expected is None:
            errors.append(f"physical plan {label} bbox evidence is unavailable")
        elif any(abs(left - right) > 0.001 for left, right in zip(observed, expected)):
            errors.append(
                f"physical plan {label} bbox drifted from the frozen geometry"
            )
    return errors
