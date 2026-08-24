from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path

import yaml

from rtl_closure.sandbox import (
    check_exact_interface,
    check_rtl_policy,
    normalize_top_module,
    parse_contract_ports,
)
from rtl_closure.spec import (
    SpecError,
    compute_intent_binding,
    load_spec,
    render_config,
    render_eqy_gate_tcl,
    render_eqy_script,
    render_interface,
    render_pin_order,
    render_question,
    render_sdc,
    validate_spec,
)


EXAMPLE = Path(__file__).parents[1] / "examples" / "counter.json"


class SpecTests(unittest.TestCase):
    @staticmethod
    def _explicit_contract(raw):
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "Count increments when enable is asserted and otherwise holds.",
                    "kind": "functional",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_COUNT",
                    "description": "Exercise one enabled counter transition.",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ],
        }
        return raw

    def test_required_intent_closure_is_hash_bound_and_tamper_evident(self):
        raw = self._explicit_contract(json.loads(EXAMPLE.read_text()))
        raw["design"]["architecture"] = {
            "required_modules": ["counter_datapath"],
            "min_sequential_bits": 8,
            "max_synth_cells": 500,
        }
        raw["intent_closure"] = {
            "mode": "required",
            "user_confirmed": True,
            "unresolved_questions": [],
            "assumptions": ["Inputs are sampled on each rising clock edge."],
            "confirmed_sha256": None,
        }
        digest = compute_intent_binding(raw)
        raw["intent_closure"]["confirmed_sha256"] = digest
        spec = validate_spec(raw)
        self.assertTrue(spec.intent_closure["closure_satisfied"])
        self.assertEqual(spec.intent_closure["binding_sha256"], digest)
        confirmed = json.loads(json.dumps(raw))

        raw["design"]["description"] += " The output saturates instead of wrapping."
        with self.assertRaisesRegex(SpecError, "does not bind"):
            validate_spec(raw)

        confirmed["design"]["architecture"]["max_synth_cells"] = 501
        with self.assertRaisesRegex(SpecError, "normalized architecture"):
            validate_spec(confirmed)

    def test_required_intent_closure_rejects_unresolved_or_model_planned_intent(self):
        raw = self._explicit_contract(json.loads(EXAMPLE.read_text()))
        raw["intent_closure"] = {
            "mode": "required",
            "user_confirmed": True,
            "unresolved_questions": ["What is the overflow behavior?"],
            "assumptions": [],
            "confirmed_sha256": compute_intent_binding(raw),
        }
        with self.assertRaisesRegex(SpecError, "unresolved_questions"):
            validate_spec(raw)

        raw = json.loads(EXAMPLE.read_text())
        raw["intent_closure"] = {
            "mode": "required",
            "user_confirmed": True,
            "unresolved_questions": [],
            "assumptions": [],
            "confirmed_sha256": "0" * 64,
        }
        with self.assertRaisesRegex(SpecError, "explicit verification.requirements"):
            validate_spec(raw)

    def test_generation_requires_two_independent_oracle_candidates(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["generation"]["n"] = 1
        with self.assertRaisesRegex(SpecError, "generation.n"):
            validate_spec(raw)

    def test_required_oracle_diversity_cannot_lower_minimum_below_two(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "oracle_diversity_mode": "required",
            "oracle_min_distinct_sources": 1,
        }
        with self.assertRaisesRegex(SpecError, "at least 2"):
            validate_spec(raw)

        raw["verification"]["oracle_diversity_mode"] = "advisory"
        spec = validate_spec(raw)
        self.assertEqual(spec.verification["oracle_min_distinct_sources"], 1)

    def test_example_renders_librelane_inputs(self):
        spec = load_spec(EXAMPLE)
        self.assertEqual(spec.pdk, "sky130A")
        self.assertIn("module rtl_closure_counter", render_interface(spec))
        self.assertIn("target period of 10 ns", render_question(spec))
        self.assertIn("create_clock", render_sdc(spec))
        config = yaml.safe_load(render_config(spec))
        self.assertEqual(config["meta"]["flow"], "Classic")
        self.assertEqual(config["PDK"], "sky130A")
        self.assertEqual(config["PL_TARGET_DENSITY_PCT"], 60.0)
        self.assertEqual(config["FP_SIZING"], "relative")
        self.assertEqual(config["IO_PIN_ORDER_CFG"], "dir::pin_order.cfg")
        self.assertEqual(spec.flow["mapped_equivalence_mode"], "off")
        self.assertEqual(spec.flow["physical_planning_mode"], "off")
        self.assertFalse(config["RUN_EQY"])
        self.assertNotIn("DIE_AREA", config)
        self.assertNotIn("CORE_AREA", config)

    def test_mapped_equivalence_mode_is_validated_and_rendered(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"]["mapped_equivalence_mode"] = "required"
        spec = validate_spec(raw)
        self.assertEqual(spec.flow["mapped_equivalence_mode"], "required")
        config = yaml.safe_load(render_config(spec))
        self.assertTrue(config["RUN_EQY"])
        self.assertEqual(config["EQY_SCRIPT"], "dir::eqy/rtl_closure_counter.eqy")
        script = render_eqy_script(spec)
        self.assertIn("name whole_design .*", script)
        self.assertIn("memory_map", script)
        self.assertIn("engine abc pdr -rfi", script)
        self.assertIn("tcl eqy/rtl_closure_counter-gate.tcl", script)
        self.assertIn("PI_LANE_RUN_DIR", render_eqy_gate_tcl())
        self.assertNotIn("eqy_script_sha256", config["meta"])

        raw["design"]["pdk"] = "gf180mcuD"
        with self.assertRaisesRegex(SpecError, "only for sky130A"):
            validate_spec(raw)

        raw["design"]["pdk"] = "sky130A"
        raw["flow"]["name"] = "OtherFlow"
        with self.assertRaisesRegex(SpecError, "Classic"):
            validate_spec(raw)

        raw["flow"]["name"] = "Classic"
        raw["flow"]["mapped_equivalence_mode"] = "advisory"
        with self.assertRaisesRegex(SpecError, "must be off or required"):
            validate_spec(raw)

    def test_only_classic_flow_is_accepted_even_without_mapped_equivalence(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"]["mapped_equivalence_mode"] = "off"
        raw["flow"]["name"] = "OtherFlow"
        with self.assertRaisesRegex(SpecError, "supports only LibreLane Classic"):
            validate_spec(raw)

    def test_floorplan_sizing_defaults_but_requires_current_explicit_mode(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"].pop("floorplan_sizing")
        relative = validate_spec(raw)
        self.assertEqual(relative.flow["floorplan_sizing"], "relative")
        self.assertNotIn("die_area", relative.flow)
        self.assertNotIn("core_area", relative.flow)

        raw["flow"]["floorplan_sizing"] = "auto"
        with self.assertRaisesRegex(SpecError, "must be relative or absolute"):
            validate_spec(raw)

        raw["flow"].pop("floorplan_sizing")
        raw["flow"]["die_area"] = [0, 0, 100, 120]
        raw["flow"]["core_area"] = [10, 10, 90, 110]
        with self.assertRaisesRegex(SpecError, "only valid with absolute"):
            validate_spec(raw)

        raw["flow"]["floorplan_sizing"] = "absolute"
        absolute = validate_spec(raw)
        config = yaml.safe_load(render_config(absolute))
        self.assertEqual(config["FP_SIZING"], "absolute")
        self.assertEqual(config["DIE_AREA"], [0.0, 0.0, 100.0, 120.0])
        self.assertEqual(config["CORE_AREA"], [10.0, 10.0, 90.0, 110.0])

    def test_floorplan_sizing_rejects_incomplete_or_conflicting_areas(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"] = {
            "floorplan_sizing": "absolute",
            "die_area": [0, 0, 100, 100],
        }
        with self.assertRaisesRegex(SpecError, "requires both"):
            validate_spec(raw)

    def test_physical_planning_modes_are_fail_closed(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"]["physical_planning_mode"] = "invalid"
        with self.assertRaisesRegex(SpecError, "physical_planning_mode"):
            validate_spec(raw)

        raw = json.loads(EXAMPLE.read_text())
        raw["flow"]["physical_planning_mode"] = "reference_locked"
        with self.assertRaisesRegex(SpecError, "reference_geometry_lock"):
            validate_spec(raw)

        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / "geometry-lock.json"
            plan.write_text("{}\n", encoding="utf-8")
            raw["flow"]["reference_geometry_lock"] = str(plan)
            spec = validate_spec(raw)
            self.assertEqual(
                spec.flow["reference_geometry_lock"], str(plan.resolve())
            )

        raw["flow"] = {
            "floorplan_sizing": "relative",
            "die_area": [0, 0, 100, 100],
            "core_area": [10, 10, 90, 90],
        }
        with self.assertRaisesRegex(SpecError, "only valid with absolute"):
            validate_spec(raw)

    def test_physical_planning_policy_is_content_bound(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["flow"]["physical_planning_mode"] = "auto_safe"
        with tempfile.TemporaryDirectory() as temp:
            policy = Path(temp) / "policy.json"
            policy.write_text("{}\n", encoding="utf-8")
            raw["flow"]["physical_planning_policy"] = str(policy)
            spec = validate_spec(raw)
            self.assertEqual(
                spec.flow["physical_planning_policy"], str(policy.resolve())
            )
            self.assertEqual(len(spec.flow["physical_planning_policy_sha256"]), 64)
            raw["flow"]["physical_planning_policy_sha256"] = "0" * 64
            with self.assertRaisesRegex(SpecError, "does not match"):
                validate_spec(raw)

    def test_pin_order_is_stable_and_splits_wide_buses_contiguously(self):
        spec = load_spec(EXAMPLE)
        self.assertEqual(
            render_pin_order(spec),
            "@bus_major\n\n"
            "#N\ncount\\[4\\]\ncount\\[5\\]\ncount\\[6\\]\ncount\\[7\\]\n\n"
            "#S\nclk\nrst_n\n\n"
            "#E\ncount\\[0\\]\ncount\\[1\\]\ncount\\[2\\]\ncount\\[3\\]\n\n"
            "#W\nenable\n",
        )

    def test_secret_and_commercial_pdk_are_rejected(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["generation"]["api_key"] = "secret"
        with self.assertRaises(SpecError):
            validate_spec(raw)
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["pdk"] = "commercial"
        with self.assertRaises(SpecError):
            validate_spec(raw)

    def test_non_api_profile_is_rejected(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["generation"]["profile"] = "rtl_closure-9b"
        with self.assertRaisesRegex(
            SpecError, "supports only the isolated api profile"
        ):
            validate_spec(raw)

    def test_closed_loop_defaults_keep_requested_reasoning_and_validate_formal_mode(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["generation"].pop("model", None)
        raw["generation"].pop("reasoning_effort")
        spec = validate_spec(raw)
        self.assertNotIn("model", spec.generation)
        self.assertEqual(spec.generation["reasoning_effort"], "xhigh")
        self.assertEqual(spec.verification["oracle_coverage_target_pct"], 85)
        self.assertEqual(spec.verification["formal_mode"], "advisory")
        self.assertEqual(spec.verification["formal_timeout_seconds"], 120)
        self.assertEqual(spec.verification["formal_total_timeout_seconds"], 3600)
        self.assertEqual(spec.intent_closure["mode"], "advisory")
        self.assertEqual(spec.verification["oracle_diversity_mode"], "advisory")
        self.assertEqual(
            spec.verification["oracle_line_coverage_mode"], "advisory"
        )

        raw["verification"] = {"formal_mode": "pretend-pass"}
        with self.assertRaisesRegex(SpecError, "formal_mode"):
            validate_spec(raw)

        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {"formal_total_timeout_seconds": 29}
        with self.assertRaisesRegex(SpecError, "formal_total_timeout_seconds"):
            validate_spec(raw)

    def test_explicit_null_verification_and_clock_reset_alias_are_rejected(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = None
        with self.assertRaisesRegex(SpecError, "verification must be a JSON object"):
            validate_spec(raw)

        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["reset"]["port"] = "clk"
        with self.assertRaisesRegex(SpecError, "must differ"):
            validate_spec(raw)

    def test_directed_tests_must_cover_declared_requirements(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {"id": "REQ_A", "text": "Cover requirement A."},
                {"id": "REQ_B", "text": "Cover requirement B."},
            ],
            "directed_tests": [
                {
                    "id": "TEST_A",
                    "description": "Only covers A.",
                    "requirement_ids": ["REQ_A"],
                    "cycles": [{"drive": {"enable": 0}}],
                }
            ],
        }
        with self.assertRaisesRegex(SpecError, "REQ_B"):
            validate_spec(raw)

        raw["verification"]["requirements"] = raw["verification"]["requirements"][:1]
        raw["verification"]["directed_tests"][0]["requirement_ids"] = []
        with self.assertRaisesRegex(SpecError, "at least one requirement"):
            validate_spec(raw)

        raw["verification"]["requirements"] = []
        with self.assertRaisesRegex(SpecError, "require at least one"):
            validate_spec(raw)

    def test_unknown_protocol_fields_are_rejected(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["orfs_config"] = "forbidden coupling"
        with self.assertRaises(SpecError):
            validate_spec(raw)

    def test_architecture_constraints_are_normalized_and_prompted(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["architecture"] = {
            "required_modules": ["counter_datapath", "counter_control"],
            "forbidden_operators": ["multiply", "divide"],
            "min_sequential_bits": 8,
            "max_sequential_bits": 16,
            "max_synth_cells": 500,
        }
        spec = validate_spec(raw)
        self.assertEqual(
            spec.architecture["required_modules"],
            ["counter_datapath", "counter_control"],
        )
        question = render_question(spec)
        self.assertIn("machine-checked", question)
        self.assertIn("counter_datapath", question)
        self.assertIn("Do not infer these operators: multiply, divide", question)

        raw["design"]["architecture"]["forbidden_operators"] = ["power"]
        with self.assertRaisesRegex(SpecError, "accepts only"):
            validate_spec(raw)

    def test_architecture_range_is_consistent(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["design"]["architecture"] = {
            "min_sequential_bits": 9,
            "max_sequential_bits": 8,
        }
        with self.assertRaisesRegex(SpecError, "must not exceed"):
            validate_spec(raw)

        raw["design"]["architecture"] = {
            "min_sequential_bits": 71,
            "max_sequential_bits": 71,
        }
        with self.assertRaisesRegex(SpecError, "must describe a range"):
            validate_spec(raw)

    def test_universal_properties_are_safe_and_requirement_bound(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_COUNT",
                    "text": "An enabled counter increments once per rising edge.",
                    "kind": "functional",
                    "signals": ["enable", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_COUNT",
                    "description": "Observe one enabled count transition.",
                    "requirement_ids": ["REQ_COUNT"],
                    "cycles": [{"drive": {"enable": 1}}],
                }
            ],
            "universal_mode": "required",
            "universal_properties": [
                {
                    "id": "PROP_COUNT_STEP",
                    "kind": "always_implication",
                    "requirement_ids": ["REQ_COUNT"],
                    "antecedent": {"port": "enable", "at": "previous"},
                    "consequent": {
                        "op": "eq",
                        "left": {"port": "count", "at": "current"},
                        "right": {
                            "op": "add",
                            "left": {"port": "count", "at": "previous"},
                            "right": {"const": 1, "width": 8},
                        },
                    },
                }
            ],
        }
        spec = validate_spec(raw)
        self.assertEqual(spec.verification["universal_mode"], "required")
        self.assertEqual(
            spec.verification["universal_properties"][0]["id"],
            "PROP_COUNT_STEP",
        )

        raw["verification"]["universal_properties"][0]["requirement_ids"] = [
            "REQ_UNKNOWN"
        ]
        with self.assertRaisesRegex(SpecError, "unknown requirement"):
            validate_spec(raw)

    def test_universal_properties_cannot_reference_harness_clock_or_reset(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["verification"] = {
            "requirements": [
                {
                    "id": "REQ_RESET",
                    "text": "Reset clears the counter output to zero.",
                    "kind": "reset",
                    "signals": ["rst_n", "count"],
                }
            ],
            "directed_tests": [
                {
                    "id": "TEST_RESET",
                    "description": "Observe reset and the cleared output.",
                    "requirement_ids": ["REQ_RESET"],
                    "cycles": [{"drive": {"rst_n": 0}}],
                }
            ],
            "universal_mode": "required",
            "universal_properties": [
                {
                    "id": "PROP_VACUOUS_RESET",
                    "kind": "always_implication",
                    "requirement_ids": ["REQ_RESET"],
                    "antecedent": {"port": "rst_n"},
                    "consequent": {
                        "op": "eq",
                        "left": {"port": "count"},
                        "right": {"const": 0, "width": 8},
                    },
                }
            ],
        }
        with self.assertRaisesRegex(SpecError, "harness-controlled"):
            validate_spec(raw)

    def test_rtl_policy_and_normalization(self):
        rtl = "module top_module(input wire a, output wire y); assign y = a; endmodule"
        normalized = normalize_top_module(rtl, "demo")
        self.assertIn("module demo", normalized)
        check_rtl_policy(normalized)
        with self.assertRaises(RuntimeError):
            check_rtl_policy("module x; initial begin end endmodule")
        with self.assertRaisesRegex(RuntimeError, "declaration initializer"):
            check_rtl_policy(
                "module x(input wire clk, output reg q = 1'b0); endmodule"
            )
        with self.assertRaisesRegex(RuntimeError, "declaration initializer"):
            check_rtl_policy(
                "module x(input wire a, output logic y); logic q = 1'b0; "
                "assign y = q & a; endmodule"
            )
        check_rtl_policy(
            "module x(input wire a, output wire y); wire keep = a; assign y = keep; endmodule"
        )
        check_rtl_policy(
            "module x(input wire a, output logic y); "
            "typedef enum logic [0:0] {IDLE = 1'b0, RUN = 1'b1} state_t; "
            "always_comb y = a; endmodule"
        )
        check_rtl_policy("module x; // no initial block here\nendmodule")

    def test_rtl_policy_rejects_all_variable_initializers_and_unsafe_tasks(self):
        initializers = (
            "integer count = 0;",
            "int count = 0;",
            "byte count = 0;",
            "logic [7:0] memory [0:3] = '{default:'0};",
            "output integer count = 0",
        )
        for declaration in initializers:
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(RuntimeError, "declaration initializer"):
                    check_rtl_policy(f"module x; {declaration}; endmodule")

        tasks = (
            "$readmemh",
            "$readmemb",
            "$writememh",
            "$writememb",
            "$urandom",
            "$fatal",
            "$past",
        )
        for task in tasks:
            with self.subTest(task=task):
                with self.assertRaisesRegex(RuntimeError, "unsafe system task"):
                    check_rtl_policy(
                        f"module x(input logic a); always_comb begin {task}(a); end endmodule"
                    )
        check_rtl_policy(
            "module x #(parameter integer W=8, parameter integer AW=$clog2(W)) "
            "(input wire [W-1:0] a, output wire [W-1:0] y); assign y=$unsigned(a); "
            "endmodule"
        )
        check_rtl_policy(
            "module x(input logic [3:0] a, output logic y); "
            "always_comb begin y = 1'b0; for (int i = 0; i < 4; i++) y |= a[i]; end "
            "endmodule"
        )

    def test_contract_parser_supports_signed_and_named_clock(self):
        ports = """module x(
          input wire clock_i,
          input wire signed [7:0] value_i,
          output wire signed [8:0] value_o
        ); endmodule
        """
        inputs, outputs = parse_contract_ports(ports)
        self.assertEqual(inputs, [("clock_i", 1), ("value_i", 8)])
        self.assertEqual(outputs, [("value_o", 9)])

    def test_exact_interface_accepts_parameters_and_ignores_helper_modules(self):
        spec = load_spec(EXAMPLE)
        rtl = """module rtl_closure_counter #(parameter W=8)(
          input wire clk, rst_n, enable,
          output reg [W-1:0] count
        ); endmodule
        module helper(input wire x, output wire y); assign y=x; endmodule
        """
        check_rtl_policy(rtl)
        check_exact_interface(rtl, spec.ports, spec.name)

    def test_exact_interface_accepts_parameterized_non_ansi_ports(self):
        spec = load_spec(EXAMPLE)
        rtl = """module rtl_closure_counter(clk, rst_n, enable, count);
          parameter W = 8;
          input clk, rst_n, enable;
          output reg [W-1:0] count;
          always @(posedge clk) begin
            if (!rst_n) count <= 0;
            else if (enable) count <= count + 1'b1;
          end
        endmodule
        """
        check_rtl_policy(rtl)
        check_exact_interface(rtl, spec.ports, spec.name)


if __name__ == "__main__":
    unittest.main()
