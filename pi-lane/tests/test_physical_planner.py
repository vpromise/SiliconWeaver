from __future__ import annotations

import unittest
from unittest.mock import patch

from pi_lane.physical_plan import load_policy
from pi_lane.physical_planner import (
    PROBE_SPECS,
    _adaptive_io_successor,
    _io_slot_evidence,
    _probe_result,
    _side_pin_bits_from_pin_order,
)


def _io_log(*, slots: int) -> str:
    parts: list[str] = []
    for side in "NSEW":
        parts.extend(
            [
                f"Placement details for the {side} side",
                "Actual pin count:  4",
                "Total pin count:  4",
                f"Tracks count:  {slots}",
            ]
        )
    return "\n".join(parts)


def _expected_side_bits() -> dict[str, int]:
    return {side: 4 for side in "NSEW"}


class PhysicalPlannerTests(unittest.TestCase):
    def test_pin_order_evidence_counts_concrete_bus_bits(self):
        text = (
            "@bus_major\n\n#S\n"
            + "\n".join(rf"in\[{index}\]" for index in range(4, 8))
            + "\n\n#W\n"
            + "\n".join(rf"in\[{index}\]" for index in range(4))
            + "\n\n#E\nout\n"
        )
        self.assertEqual(
            _side_pin_bits_from_pin_order({"in": 8, "out": 1}, text),
            {"N": 0, "S": 4, "E": 1, "W": 4},
        )

    def test_pin_order_evidence_supports_whole_bus_regex(self):
        self.assertEqual(
            _side_pin_bits_from_pin_order(
                {"in": 8, "out": 1},
                "@bus_major\n\n#W\nin\\[\\d+\\]\n\n#E\nout\n",
            ),
            {"N": 0, "S": 0, "E": 1, "W": 8},
        )

    def test_probe_order_includes_exact_io_preflight_before_placement(self):
        self.assertEqual(
            [row[0] for row in PROBE_SPECS],
            [
                "pdn_gate",
                "io_preflight_gate",
                "detailed_placement_gate",
                "robustness_gate",
            ],
        )
        self.assertEqual(PROBE_SPECS[1][1], "Odb.CustomIOPlacement")

    def test_io_slot_evidence_requires_configured_headroom(self):
        passing = _io_slot_evidence(
            _io_log(slots=6),
            fixed_headroom=2,
            headroom_percent=5,
            expected_side_pin_bits=_expected_side_bits(),
        )
        self.assertIsNotNone(passing)
        assert passing is not None
        self.assertEqual(passing["status"], "pass")
        self.assertEqual(passing["sides"]["N"]["required_slots"], 6)

        tight = _io_slot_evidence(
            _io_log(slots=5),
            fixed_headroom=2,
            headroom_percent=5,
            expected_side_pin_bits=_expected_side_bits(),
        )
        self.assertIsNotNone(tight)
        assert tight is not None
        self.assertEqual(tight["status"], "feasible_not_robust")

    def test_io_slot_evidence_rejects_incomplete_tool_log(self):
        self.assertIsNone(
            _io_slot_evidence(
                "Placement details for the N side\nTracks count:  10\n",
                fixed_headroom=2,
                headroom_percent=5,
                expected_side_pin_bits={"N": 4, "E": 0, "W": 0, "S": 0},
            )
        )

    def test_io_slot_evidence_accepts_omitted_zero_demand_sides(self):
        evidence = _io_slot_evidence(
            "\n".join(
                (
                    "Placement details for the N side",
                    "Actual pin count:  4",
                    "Total pin count:  4",
                    "Tracks count:  6",
                )
            ),
            fixed_headroom=2,
            headroom_percent=5,
            expected_side_pin_bits={"N": 4, "E": 0, "W": 0, "S": 0},
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["sides"]["E"]["status"], "not_applicable")

    def test_io_slot_evidence_rejects_side_assignment_drift(self):
        self.assertIsNone(
            _io_slot_evidence(
                _io_log(slots=6),
                fixed_headroom=2,
                headroom_percent=5,
                expected_side_pin_bits={"N": 3, "E": 4, "W": 4, "S": 4},
            )
        )

    def test_failed_io_step_preserves_exact_capacity_deficit(self):
        evidence = _io_slot_evidence(
            "\n".join(
                (
                    "[ERROR] The W side of the floorplan doesn't have enough slots "
                    "for all the pins: 256 pins/199 slots.",
                    "Placement details for the E side",
                    "Actual pin count:  1",
                    "Total pin count:  1",
                    "Tracks count:  199",
                )
            ),
            fixed_headroom=2,
            headroom_percent=5,
            expected_side_pin_bits={"N": 0, "S": 8, "E": 1, "W": 256},
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["status"], "capacity_insufficient")
        self.assertEqual(evidence["sides"]["W"]["required_slots"], 269)
        self.assertEqual(evidence["sides"]["W"]["slot_deficit"], 70)
        self.assertEqual(evidence["sides"]["S"]["status"], "not_evaluated")

    def test_adaptive_successor_expands_only_the_deficient_axis(self):
        policy = load_policy()
        candidate = {
            "candidate_id": "candidate-001",
            "core_width_um": 130.0,
            "core_height_um": 250.0,
            "io_preflight_evidence": {
                "slot_evidence": {
                    "status": "capacity_insufficient",
                    "sides": {
                        "W": {
                            "status": "capacity_insufficient",
                            "required_slots": 269,
                            "legal_slots": 199,
                        }
                    },
                }
            },
        }
        successor = _adaptive_io_successor(
            candidate,
            policy=policy,
            existing_candidates=[candidate],
            expansion_index=1,
        )
        self.assertIsNotNone(successor)
        assert successor is not None
        self.assertEqual(successor["core_width_um"], 130.0)
        self.assertGreater(successor["core_height_um"], 250.0)
        self.assertEqual(successor["expansion_parent_id"], "candidate-001")
        self.assertEqual(successor["expansion_reason"]["sides"], ["W"])

    def test_failed_probe_result_keeps_capacity_evidence_for_adaptation(self):
        log = (
            "[ERROR] The W side of the floorplan doesn't have enough slots for "
            "all the pins: 256 pins/199 slots.\n"
            "Placement details for the E side\n"
            "Actual pin count:  1\n"
            "Total pin count:  1\n"
            "Tracks count:  199\n"
        )
        candidate = {
            "candidate_id": "candidate-001",
            "expected_side_pin_bits": {"N": 0, "S": 8, "E": 1, "W": 256},
        }
        lane = {
            "run_id": "probe-run",
            "status": "failed",
            "last_completed_step": "OpenROAD.IOPlacement",
            "failed_step": "Odb.CustomIOPlacement",
            "errors": ["W side lacks slots"],
            "librelane_run_dir": "/tmp/probe-run",
        }
        with (
            patch(
                "pi_lane.physical_planner._probe_evidence",
                return_value={
                    "status": "available",
                    "tool": {"script_sha256": "a" * 64},
                    "logs": [{"path": "/tmp/probe-run/io.log"}],
                },
            ),
            patch("pi_lane.physical_planner._safe_file_text", return_value=log),
        ):
            result = _probe_result(
                candidate,
                lane,
                gate="io_preflight_gate",
                io_geometry={
                    "io_headroom_fixed_slots": 2,
                    "io_headroom_percent": 5,
                },
            )
        self.assertEqual(result["io_preflight_gate"], "fail")
        slots = result["io_preflight_evidence"]["slot_evidence"]
        self.assertEqual(slots["status"], "capacity_insufficient")
        self.assertEqual(slots["sides"]["W"]["slot_deficit"], 70)

    def test_robust_probe_requires_calibrated_midpnr_timing_margin(self):
        candidate = {"candidate_id": "candidate-001"}
        lane = {
            "run_id": "probe-run",
            "status": "completed",
            "last_completed_step": "OpenROAD.STAMidPNR-3",
            "failed_step": None,
            "errors": [],
            "librelane_run_dir": "/tmp/probe-run",
            "metrics_path": "/tmp/probe-run/final/metrics.json",
            "qor": {
                "design__die__bbox": "0 0 60 60",
                "design__core__bbox": "10 10 50 50",
                "utilization_pct": 40.0,
                "status": "final",
                "setup_ws_ns": 3.1,
                "setup_tns_ns": 0.0,
                "max_slew_violation_count": 0.0,
                "max_cap_violation_count": 0.0,
            },
        }
        with patch(
            "pi_lane.physical_planner._probe_evidence",
            return_value={"status": "available", "logs": []},
        ):
            result = _probe_result(
                candidate,
                lane,
                gate="robustness_gate",
                maximum_utilization_pct=72.0,
                minimum_setup_wns_ns=4.0,
            )
        self.assertEqual(result["robustness_gate"], "pass")
        self.assertEqual(result["timing_qualification_gate"], "fail")
        self.assertEqual(result["midpnr_setup_wns_ns"], 3.1)


if __name__ == "__main__":
    unittest.main()
