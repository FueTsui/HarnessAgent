"""Safe recovery of parameter-bearing tool names seen in real Harness runs."""
import unittest
from unittest.mock import patch

from backend.runtime.control import canonical_tool_name, repair_tool_call


class ToolCallRecoveryTests(unittest.TestCase):
    offered = {"read_skill_resource", "web_search", "sentiment_analysis"}

    def recover(self, name, arguments="", offered=None):
        return repair_tool_call(
            {"name": name, "arguments": arguments},
            self.offered if offered is None else offered,
        )

    def assert_rejected(self, name, arguments="", offered=None):
        result = self.recover(name, arguments, offered)
        self.assertTrue(result.error)
        self.assertEqual(result.name, "")
        self.assertEqual(result.arguments, {})
        self.assertFalse(result.repaired)
        self.assertLess(len(result.error), 180)
        return result

    def test_reproduces_real_truncated_parameter_ending(self):
        result = self.recover('read_skill_resource(file="SKILL", skill="pptx")\n</parameter')
        self.assertIsNone(result.error)
        self.assertTrue(result.repaired)
        self.assertEqual(result.name, "read_skill_resource")
        self.assertEqual(result.arguments, {"file": "SKILL", "skill": "pptx"})

    def test_recovers_only_known_parameter_delimiter(self):
        result = self.recover('read_skill_resource(file="SKILL.md", skill="pptx")\n</parameter>')
        self.assertEqual(result.arguments["file"], "SKILL.md")
        self.assertIsNone(result.error)
        self.assert_rejected('web_search(query="test")</untrusted>')
        self.assert_rejected('web_search(query="test")</parameter></parameter>')

    def test_regular_call_preserves_schema_pipeline_arguments_and_input(self):
        arguments = '{"query":"深圳天气"}'
        original = {"name": "web_search", "arguments": arguments}
        result = repair_tool_call(original, self.offered)
        self.assertEqual(result.name, "web_search")
        self.assertEqual(result.arguments, arguments)
        self.assertFalse(result.repaired)
        self.assertIsNone(result.error)
        self.assertEqual(original, {"name": "web_search", "arguments": arguments})

    def test_regular_arguments_are_not_rewritten_even_when_invalid(self):
        result = self.recover("web_search", "invalid JSON")
        self.assertIsNone(result.error)
        self.assertEqual(result.arguments, "invalid JSON")

    def test_harmony_markers_and_real_analysis_name_remain_compatible(self):
        for name in ("web_searchcommentary", "web_search<|channel|>analysis", "web_search<|channel|>final"):
            with self.subTest(name=name):
                result = self.recover(name, {"query": "test"})
                self.assertIsNone(result.error)
                self.assertEqual(result.name, "web_search")
        result = self.recover("sentiment_analysis", {})
        self.assertEqual(result.name, "sentiment_analysis")
        self.assertFalse(result.repaired)

    def test_accepts_json_compatible_nested_literals_and_unicode(self):
        result = self.recover(
            'web_search(query="中文", options={"nested": [1, -2, 3.5, True, False, None]})'
        )
        self.assertIsNone(result.error)
        self.assertEqual(result.arguments, {
            "query": "中文", "options": {"nested": [1, -2, 3.5, True, False, None]},
        })

    def test_identical_embedded_and_supplied_arguments_are_unambiguous(self):
        for arguments in ('{"query":"test"}', {"query": "test"}):
            with self.subTest(arguments=arguments):
                result = self.recover('web_search(query="test")', arguments)
                self.assertIsNone(result.error)
                self.assertEqual(result.arguments, {"query": "test"})

    def test_empty_name_arguments_can_use_supplied_object(self):
        result = self.recover("web_search()", '{"query":"test"}')
        self.assertIsNone(result.error)
        self.assertEqual(result.arguments, {"query": "test"})

    def test_conflicting_or_partial_sources_are_never_merged(self):
        for arguments in ({"query": "other"}, {"count": 1}, {"query": "test", "count": 1}):
            with self.subTest(arguments=arguments):
                result = self.assert_rejected('web_search(query="test")', arguments)
                self.assertIn("不一致", result.error)

    def test_type_conflicts_are_not_hidden_by_python_equality(self):
        self.assert_rejected("web_search(count=True)", {"count": 1})
        self.assert_rejected("web_search(count=1)", {"count": 1.0})

    def test_unknown_or_unauthorized_tools_never_resolve(self):
        for name in ('delete_file(path="secret")', 'web_search(query="test")'):
            with self.subTest(name=name):
                self.assert_rejected(name, offered={"read_skill_resource"})

    def test_plain_unavailable_name_is_preserved_for_catalog_rejection(self):
        result = self.recover("unknowncommentary", {"query": "test"})
        self.assertEqual(result.name, "unknowncommentary")
        self.assertIsNone(result.error)
        self.assertFalse(result.repaired)

    def test_case_ambiguous_harmony_mapping_is_not_repaired(self):
        offered = {"web_search", "WEB_SEARCH"}
        self.assertEqual(canonical_tool_name("web_searchcommentary", offered), "web_searchcommentary")
        self.assert_rejected("web_searchcommentary(query='x')", offered=offered)
        self.assertEqual(self.recover("web_search", offered=offered).name, "web_search")

    def test_rejects_multiple_calls_and_non_name_functions(self):
        for name in (
            'web_search(query="x"); web_search(query="y")',
            '(web_search(query="x"), web_search(query="y"))',
            'obj.web_search(query="x")',
            'web_search(query="x") or web_search(query="y")',
            'web_search(query="x")\n__import__("os")',
        ):
            with self.subTest(name=name):
                self.assert_rejected(name)

    def test_expression_arguments_are_not_executed(self):
        attacks = (
            'web_search(query=__import__("os").system("DO_NOT_EXECUTE"))',
            'web_search(query=open("DO_NOT_OPEN").read())',
            'web_search(query=(lambda: "x")())',
            'web_search(query=[x for x in [1, 2]])',
            'web_search(query="a" * 100000000)',
            'web_search(query=2 ** 100000000)',
            'web_search(query=unknown_value)',
            'web_search(query=f"{1}")',
        )
        with patch("os.system") as system, patch("builtins.open") as open_file:
            for name in attacks:
                with self.subTest(name=name):
                    self.assert_rejected(name)
            system.assert_not_called()
            open_file.assert_not_called()

    def test_no_positional_star_or_duplicate_keywords(self):
        for name in (
            'web_search("x")', 'web_search(*["x"])',
            'web_search(**{"query": "x"})', 'web_search(query="x", query="y")',
        ):
            with self.subTest(name=name):
                self.assert_rejected(name)

    def test_duplicate_nested_object_keys_are_rejected_before_literal_eval(self):
        self.assert_rejected('web_search(options={"key": "first", "key": "second"})')
        self.assert_rejected('web_search(options={**{"key": "first"}})')
        self.assert_rejected("web_search()", '{"query":"first","query":"second"}')

    def test_non_json_literals_and_nonfinite_numbers_are_rejected(self):
        for literal in ("(1, 2)", "{1, 2}", "b'bytes'", "...", "1j", "float('nan')", "1e999", "{1: 'x'}"):
            with self.subTest(literal=literal):
                self.assert_rejected(f"web_search(query={literal})")
        for arguments in ('{"count":NaN}', '{"count":Infinity}', [], "[]", "not JSON"):
            with self.subTest(arguments=arguments):
                self.assert_rejected("web_search()", arguments)

    def test_parser_input_size_node_count_and_depth_are_bounded(self):
        cases = (
            'web_search(query="' + "x" * 16_384 + '")',
            "web_search(query=" + "[" * 40 + "1" + "]" * 40 + ")",
            "web_search(query=[" + ",".join("1" for _ in range(2200)) + "])",
        )
        for index, name in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected(name)
        self.assert_rejected("web_search()", '{"query":"' + "x" * 16_384 + '"}')
        cycle = {}
        cycle["self"] = cycle
        self.assert_rejected("web_search()", cycle)

    def test_invalid_diagnostics_do_not_echo_model_secrets(self):
        secret = "SENSITIVE_USER_PASSWORD"
        result = self.assert_rejected(f"web_search(query=unsafe('{secret}'))")
        self.assertNotIn(secret, result.error)
        self.assertNotIn("unsafe", result.error)

    def test_malformed_function_objects_fail_closed(self):
        for function in (None, [], {}, {"name": None}, {"name": 123}, {"name": " "}):
            with self.subTest(function=function):
                result = repair_tool_call(function, self.offered)
                self.assertTrue(result.error)
                self.assertEqual(result.name, "")


if __name__ == "__main__":
    unittest.main()
