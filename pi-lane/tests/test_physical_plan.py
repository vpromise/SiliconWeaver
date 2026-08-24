from __future__ import annotations

import copy
import json
import math
import unittest

from pi_lane.physical_plan import (
    PlanningInputs,
    bind_plan_to_run,
    candidate_domain_projection,
    calibrate_policy,
    canonical_sha256,
    derive_lower_bounds,
    finalize_plan,
    generate_candidates,
    geometry_drift_errors,
    geometry_lock,
    export_geometry_lock_document,
    load_policy,
    minimum_geometry_classification,
    minimum_geometry_density_qualification,
    validate_plan,
    validate_geometry_lock_document,
    plan_overrides,
    select_minimum_robust_candidate,
    validate_policy,
    validate_planning_overrides,
)


class PhysicalPlanningContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy()

    def test_bootstrap_policy_is_explicitly_non_publishable(self):
        self.assertEqual(self.policy["status"], "bootstrap")
        self.assertFalse(self.policy["publishable"])
        validate_policy(self.policy)
        invalid = copy.deepcopy(self.policy)
        invalid["publishable"] = True
        with self.assertRaisesRegex(ValueError, "calibrated"):
            validate_policy(invalid)

    def test_io_and_logic_both_constrain_candidate_lower_bound(self):
        inputs = PlanningInputs(
            mapped_area_um2=100,
            port_bits=80,
            side_pin_bits={"N": 40, "S": 40, "E": 0, "W": 0},
            clocked=False,
        )
        bounds = derive_lower_bounds(inputs, self.policy)
        self.assertGreaterEqual(bounds["minimum_core_width_um"], 45)
        self.assertGreaterEqual(bounds["minimum_core_height_um"], 20)
        candidates = generate_candidates(bounds, self.policy)
        self.assertGreater(len(candidates), 1)
        self.assertEqual(
            candidates,
            sorted(
                candidates,
                key=lambda row: (
                    row["core_area_um2"],
                    abs(math.log(row["aspect_ratio"])),
                    row["core_width_um"],
                    row["core_height_um"],
                ),
            ),
        )

    def test_selection_requires_both_probe_gates_and_uses_minimum_area(self):
        rows = [
            {
                "candidate_id": "a",
                "core_area_um2": 400,
                "aspect_ratio": 1,
                "pdn_gate": "pass",
                "io_preflight_gate": "pass",
                "detailed_placement_gate": "pass",
                "robustness_gate": "fail",
            },
            {
                "candidate_id": "b",
                "core_area_um2": 500,
                "aspect_ratio": 1,
                "pdn_gate": "pass",
                "io_preflight_gate": "pass",
                "detailed_placement_gate": "pass",
                "robustness_gate": "pass",
            },
            {
                "candidate_id": "c",
                "core_area_um2": 600,
                "aspect_ratio": 1,
                "pdn_gate": "pass",
                "io_preflight_gate": "pass",
                "detailed_placement_gate": "pass",
                "robustness_gate": "pass",
            },
        ]
        self.assertEqual(select_minimum_robust_candidate(rows)["candidate_id"], "b")

    def test_selection_rejects_robust_candidates_below_timing_margin(self):
        rows = [
            {
                "candidate_id": "small",
                "core_area_um2": 400,
                "aspect_ratio": 1,
                "pdn_gate": "pass",
                "io_preflight_gate": "pass",
                "detailed_placement_gate": "pass",
                "robustness_gate": "pass",
                "timing_qualification_gate": "fail",
                "midpnr_setup_wns_ns": 3.1,
            },
            {
                "candidate_id": "qualified",
                "core_area_um2": 500,
                "aspect_ratio": 1,
                "pdn_gate": "pass",
                "io_preflight_gate": "pass",
                "detailed_placement_gate": "pass",
                "robustness_gate": "pass",
                "timing_qualification_gate": "pass",
                "midpnr_setup_wns_ns": 4.2,
            },
        ]
        self.assertEqual(
            select_minimum_robust_candidate(rows)["candidate_id"], "qualified"
        )

    def test_calibration_requires_complete_balanced_human_references(self):
        references = []
        for index in range(20):
            references.append(
                {
                    "case_id": f"case-{index}",
                    "complete": True,
                    "delivery_gate": "pass",
                    "clocked": index >= 10,
                    "mapped_area_um2": 100.0,
                    "final_cell_area_um2": 120.0 + index,
                    "plan_sha256": f"{index + 1:064x}",
                    "gds_sha256": f"{index + 101:064x}",
                }
            )
        evidence = {
            "schema_version": 1,
            "kind": "pi-lane-human-planning-calibration",
            "pdk": self.policy["pdk"],
            "pdk_revision": "test-revision",
            "pdk_integrity_sha256": "a" * 64,
            "image_digest": "sha256:" + "b" * 64,
            "librelane_version": self.policy["librelane_version"],
            "flow_registry_version": self.policy["flow_registry_version"],
            "references": references,
        }
        calibrated = calibrate_policy(
            self.policy, evidence, policy_id="sky130-human-p95-v1"
        )
        self.assertTrue(calibrated["publishable"])
        self.assertEqual(calibrated["status"], "calibrated")
        self.assertGreater(calibrated["growth_reserve"]["clocked_multiplier"], 1)
        broken = json.loads(json.dumps(evidence))
        broken["references"][0]["delivery_gate"] = "fail"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            calibrate_policy(self.policy, broken, policy_id="broken")

    def _plan(self) -> dict:
        seed = {
            "candidate_id": "candidate-001",
            "core_area_um2": 1600,
            "aspect_ratio": 1,
            "requested_die_area": [0, 0, 60, 60],
            "requested_core_area": [10, 10, 50, 50],
        }
        selected = {
            **seed,
            "observed_die_bbox": [0, 0, 60, 60],
            "observed_core_bbox": [10, 10, 50, 50],
            "pdn_gate": "pass",
            "io_preflight_gate": "pass",
            "detailed_placement_gate": "pass",
            "robustness_gate": "pass",
            "post_probe_utilization_pct": 40,
        }
        domain_sha256 = canonical_sha256(candidate_domain_projection([seed]))
        classification = minimum_geometry_classification(
            selected,
            [selected],
            candidate_domain_sha256=domain_sha256,
            search_complete=True,
        )
        return finalize_plan(
            {
                "publishable": False,
                "plan_id": "plan-1",
                "created_at": "2026-08-12T00:00:00+08:00",
                "config_sha256": "a" * 64,
                "input_bundle_sha256": "b" * 64,
                "pdk": "sky130A",
                "pdk_revision": "c" * 40,
                "pdk_integrity_sha256": "d" * 64,
                "image_digest": "sha256:" + "e" * 64,
                "librelane_version": "3.0.5",
                "flow_registry_version": "librelane-3.0.5-classic-v1",
                "policy": self.policy,
                "policy_sha256": canonical_sha256(self.policy),
                "synthesis_baseline": {"mapped_area_um2": 100},
                "candidates": [selected],
                "candidate_domain_sha256": domain_sha256,
                "candidate_domain_count": 1,
                "search_complete": True,
                "selected_candidate_id": selected["candidate_id"],
                "io_lock_sha256": "f" * 64,
                "geometry_lock": geometry_lock(
                    selected, classification=classification
                ),
            }
        )

    def test_plan_binding_and_geometry_drift_are_fail_closed(self):
        plan = self._plan()
        binding = bind_plan_to_run(
            plan,
            config_sha256="a" * 64,
            input_bundle_sha256="b" * 64,
            pdk="sky130A",
            pdk_revision="c" * 40,
            pdk_integrity_sha256="d" * 64,
            image_digest="sha256:" + "e" * 64,
        )
        self.assertEqual(binding["geometry_lock_sha256"], plan["geometry_lock_sha256"])
        self.assertEqual(binding["io_lock_sha256"], plan["io_lock_sha256"])
        document = export_geometry_lock_document(plan)
        validate_geometry_lock_document(document)
        self.assertEqual(document["geometry_lock_sha256"], plan["geometry_lock_sha256"])
        self.assertEqual(document["io_lock_sha256"], plan["io_lock_sha256"])
        self.assertNotIn("synthesis_baseline", document)
        self.assertNotIn("candidates", document)
        self.assertEqual(plan_overrides(plan)[0], "FP_SIZING=absolute")
        self.assertEqual(plan_overrides(plan)[1], "DIE_AREA=0 0 60 60")
        self.assertEqual(plan_overrides(plan)[2], "CORE_AREA=10 10 50 50")
        self.assertEqual(
            geometry_drift_errors(
                binding,
                {"design__die__bbox": "0 0 60 60", "design__core__bbox": "10 10 50 50"},
            ),
            [],
        )
        self.assertIn(
            "core bbox drifted",
            geometry_drift_errors(
                binding,
                {"design__die__bbox": "0 0 60 60", "design__core__bbox": "10 10 55 50"},
            )[0],
        )

    def test_planning_probe_geometry_is_complete_and_controller_owned(self):
        self.assertEqual(
            validate_planning_overrides(
                [
                    'FP_SIZING="absolute"',
                    "DIE_AREA=[0,0,60,60]",
                    "CORE_AREA=[10,10,50,50]",
                ]
            ),
            ["FP_SIZING=absolute", "DIE_AREA=0 0 60 60", "CORE_AREA=10 10 50 50"],
        )
        self.assertEqual(
            validate_planning_overrides(
                ["FP_SIZING=absolute", "DIE_AREA=0 0 60 60", "CORE_AREA=10 10 50 50"]
            ),
            ["FP_SIZING=absolute", "DIE_AREA=0 0 60 60", "CORE_AREA=10 10 50 50"],
        )
        with self.assertRaisesRegex(ValueError, "complete absolute geometry"):
            validate_planning_overrides(["FP_SIZING=absolute"])

    def test_incomplete_search_cannot_claim_minimum_geometry_dominance(self):
        selected = {
            "candidate_id": "candidate-002",
            "core_area_um2": 900,
            "aspect_ratio": 1,
            "pdn_gate": "pass",
            "io_preflight_gate": "pass",
            "detailed_placement_gate": "pass",
            "robustness_gate": "pass",
            "post_probe_utilization_pct": 30,
        }
        smaller = {
            "candidate_id": "candidate-001",
            "core_area_um2": 400,
            "aspect_ratio": 1,
            "pdn_gate": "fail",
            "io_preflight_gate": "not_run",
            "detailed_placement_gate": "not_run",
            "robustness_gate": "not_run",
        }
        classification = minimum_geometry_classification(
            selected,
            [smaller, selected],
            candidate_domain_sha256=canonical_sha256([smaller, selected]),
            search_complete=False,
        )
        self.assertEqual(classification["status"], "minimum_geometry_unproven")
        self.assertFalse(classification["search_complete"])

    def test_timing_limited_geometry_round_trips_through_current_contract(self):
        plan = self._plan()
        selected = plan["candidates"][0]
        selected["timing_qualification_gate"] = "pass"
        smaller = {
            **selected,
            "candidate_id": "candidate-000",
            "core_area_um2": 900,
            "requested_die_area": [0, 0, 50, 50],
            "requested_core_area": [10, 10, 40, 40],
            "observed_die_bbox": [0, 0, 50, 50],
            "observed_core_bbox": [10, 10, 40, 40],
            "timing_qualification_gate": "fail",
        }
        plan["candidates"] = [smaller, selected]
        plan["candidate_domain_count"] = 2
        domain_sha256 = canonical_sha256(candidate_domain_projection(plan["candidates"]))
        plan["candidate_domain_sha256"] = domain_sha256
        classification = minimum_geometry_classification(
            selected,
            plan["candidates"],
            candidate_domain_sha256=domain_sha256,
            search_complete=True,
        )
        self.assertEqual(classification["tool_constraints"], ["timing"])
        plan["geometry_lock"] = geometry_lock(selected, classification=classification)
        plan["geometry_lock_sha256"] = canonical_sha256(plan["geometry_lock"])
        plan.pop("plan_sha256")
        plan["plan_sha256"] = canonical_sha256(plan)

        validate_plan(plan)
        validate_geometry_lock_document(export_geometry_lock_document(plan))

    def test_minimum_geometry_density_qualification_requires_matching_final_geometry(self):
        plan = self._plan()
        metrics = {
            "design__die__bbox": "0 0 60 60",
            "design__core__bbox": "10 10 50 50",
        }
        qualification = minimum_geometry_density_qualification(plan, metrics)
        self.assertEqual(qualification["gate"], "pass")
        self.assertEqual(
            qualification["classification"], "minimum_geometry_dominated"
        )
        drifted = minimum_geometry_density_qualification(
            plan,
            {**metrics, "design__core__bbox": "10 10 55 50"},
        )
        self.assertEqual(drifted["gate"], "fail")
        self.assertIn("core bbox drifted", drifted["reasons"][0])

    def test_malformed_candidate_evidence_fails_closed(self):
        malformed = self._plan()
        malformed["candidates"] = ["not-a-candidate"]
        with self.assertRaisesRegex(ValueError, "candidate evidence"):
            validate_plan(malformed)

    def test_candidate_domain_excludes_probe_context_and_binds_geometry(self):
        seed = {
            "candidate_id": "candidate-001",
            "core_width_um": 40,
            "core_height_um": 40,
            "core_area_um2": 1600,
            "aspect_ratio": 1,
            "requested_die_area": [0, 0, 60, 60],
            "requested_core_area": [10, 10, 50, 50],
            "scale_factor": 1,
        }
        probed = {
            **seed,
            "expected_side_pin_bits": {"N": 4, "S": 0, "E": 0, "W": 0},
            "pdn_gate": "pass",
            "pdn_evidence": {"tool": "OpenROAD.GeneratePDN"},
            "observed_core_bbox": [10, 10, 50, 50],
        }
        self.assertEqual(
            canonical_sha256(candidate_domain_projection([seed])),
            canonical_sha256(candidate_domain_projection([probed])),
        )
        changed_geometry = {**probed, "core_width_um": 45}
        self.assertNotEqual(
            canonical_sha256(candidate_domain_projection([seed])),
            canonical_sha256(candidate_domain_projection([changed_geometry])),
        )


if __name__ == "__main__":
    unittest.main()
