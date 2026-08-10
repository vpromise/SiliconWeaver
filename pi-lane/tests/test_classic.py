from __future__ import annotations

import unittest

from pi_lane.classic import (
    CLASSIC_REGISTRY_VERSION,
    CLASSIC_STEP_IDS,
    CLASSIC_STEP_PHASE,
    CLASSIC_TERMINAL_STEP,
    canonical_classic_step,
    next_classic_step,
    validate_classic_range,
)


class ClassicRegistryTests(unittest.TestCase):
    def test_registry_matches_the_pinned_classic_shape(self):
        self.assertEqual(CLASSIC_REGISTRY_VERSION, "librelane-3.0.5-classic-v1")
        self.assertEqual(len(CLASSIC_STEP_IDS), 80)
        self.assertEqual(len(set(CLASSIC_STEP_IDS)), 80)
        self.assertEqual(CLASSIC_TERMINAL_STEP, "Misc.ReportManufacturability")
        self.assertEqual(set(CLASSIC_STEP_PHASE), set(CLASSIC_STEP_IDS))

    def test_ids_are_case_canonicalized_but_not_fuzzy_matched(self):
        self.assertEqual(
            canonical_classic_step("openroad.floorplan"), "OpenROAD.Floorplan"
        )
        with self.assertRaisesRegex(ValueError, "unknown LibreLane Classic Step"):
            canonical_classic_step("floorplan")

    def test_range_and_next_step_follow_execution_order(self):
        self.assertEqual(
            validate_classic_range("Yosys.Synthesis", "OpenROAD.CTS"),
            ("Yosys.Synthesis", "OpenROAD.CTS"),
        )
        with self.assertRaisesRegex(ValueError, "must not occur after"):
            validate_classic_range("OpenROAD.CTS", "Yosys.Synthesis")
        self.assertEqual(
            next_classic_step("OpenROAD.Floorplan"), "OpenROAD.DumpRCValues"
        )
        self.assertIsNone(next_classic_step(CLASSIC_TERMINAL_STEP))


if __name__ == "__main__":
    unittest.main()
