from __future__ import annotations

import unittest

from rtl_closure.extraction import extract_python, extract_verilog


class ExtractionTests(unittest.TestCase):
    def test_python_prefers_last_valid_answer_block(self):
        response = """
<answer>```python
def broken(:
```</answer>
<answer>```python
class TopModule:
    def eval(self, inputs):
        return {"out": inputs["value"]}
```</answer>
"""
        extracted = extract_python(response)
        self.assertIn("class TopModule", extracted)
        self.assertNotIn("def broken", extracted)

    def test_python_accepts_tilde_fence_and_crlf(self):
        response = "~~~py\r\nx = 1\r\n~~~~\r\n"
        self.assertEqual(extract_python(response), "x = 1")

    def test_python_rejects_invalid_or_empty_content(self):
        self.assertEqual(extract_python("```python\ndef broken(:\n```"), "")
        self.assertEqual(extract_python(""), "")

    def test_verilog_trims_unfenced_explanation(self):
        response = """Here is the design:
module top_module(input logic a, output logic y);
  assign y = a;
endmodule
This implementation is synthesizable.
"""
        self.assertEqual(
            extract_verilog(response),
            "module top_module(input logic a, output logic y);\n"
            "  assign y = a;\n"
            "endmodule",
        )

    def test_verilog_keeps_complete_multi_module_design(self):
        response = """```systemverilog
module helper(input logic a, output logic y);
  assign y = a;
endmodule
module top_module(input logic a, output logic y);
  helper child(.a(a), .y(y));
endmodule
````
"""
        extracted = extract_verilog(response)
        self.assertIn("module helper", extracted)
        self.assertIn("module top_module", extracted)
        self.assertEqual(extracted.count("endmodule"), 2)

    def test_verilog_rejects_incomplete_module(self):
        self.assertEqual(extract_verilog("```sv\nmodule top_module;\n```"), "")

    def test_unfenced_verilog_ignores_structure_words_in_comments_and_strings(self):
        response = '''The module is below.
// This module implements a passthrough; do not add another endmodule.
/* module decoy; endmodule */
module top_module(input logic a, output logic y);
  string description = "module and endmodule are prose";
  assign y = a;
endmodule
Done.
'''
        extracted = extract_verilog(response)
        self.assertTrue(extracted.startswith("module top_module"))
        self.assertTrue(extracted.endswith("endmodule"))
        self.assertNotIn("// This module", extracted)

    def test_fenced_verilog_preserves_macros_packages_and_trailing_directives(self):
        response = '''```systemverilog
`default_nettype none
package widths;
  parameter int WIDTH = 1;
endpackage
module top_module(input logic a, output logic y);
  assign y = a;
endmodule
`default_nettype wire
```
'''
        extracted = extract_verilog(response)
        self.assertTrue(extracted.startswith("`default_nettype none"))
        self.assertIn("package widths;", extracted)
        self.assertTrue(extracted.endswith("`default_nettype wire"))


if __name__ == "__main__":
    unittest.main()
