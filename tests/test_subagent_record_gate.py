from __future__ import annotations

import unittest

from quality_pilot.subagents import validate_mcp_subagent_results


class SubagentRecordGateTest(unittest.TestCase):
    def _provider(self, name: str, verified: bool = True) -> dict[str, object]:
        return {
            "provider": name,
            "chat_url": f"https://{name}.example/chat/1",
            "chat_record_verified": verified,
            "chat_record": {
                "chat_url": f"https://{name}.example/chat/1",
                "reloaded": verified,
                "prompt_match": verified,
                "answer_match": verified,
                "prompt_sha256": "a" * 64 if verified else "",
                "answer_sha256": "b" * 64 if verified else "",
            },
        }

    def test_excluded_provider_is_explicit_and_mimo_is_in_active_scope(self) -> None:
        active = ("deepseek", "kimi", "qwen", "mimo")
        payload = {"provider_results": {name: self._provider(name) for name in active}}
        result = validate_mcp_subagent_results(
            payload,
            [*active, "zai"],
            excluded_providers=["zai"],
        )
        self.assertEqual(result["record_gate_status"], "VERIFIED")
        self.assertEqual(result["active_providers"], list(active))
        self.assertEqual(result["excluded_providers"], ["zai"])
        self.assertEqual(result["missing_providers"], [])
        self.assertEqual(result["providers"]["mimo"]["status"], "VERIFIED")
        self.assertEqual(len(result["providers"]["mimo"]["receipt"]["receipt_hash"]), 64)

    def test_unverified_answer_is_candidate_only_and_blocks_record_gate(self) -> None:
        payload = {"provider_results": {"deepseek": self._provider("deepseek", verified=False)}}
        result = validate_mcp_subagent_results(payload, ["deepseek"])
        self.assertEqual(result["record_gate_status"], "BLOCK")
        self.assertEqual(result["reason"], "candidate_unverified")
        self.assertEqual(result["providers"]["deepseek"]["status"], "candidate_unverified")
        self.assertNotIn("receipt", result["providers"]["deepseek"])

    def test_missing_provider_is_not_treated_as_success(self) -> None:
        result = validate_mcp_subagent_results({"provider_results": {}}, ["deepseek", "qwen"])
        self.assertEqual(result["record_gate_status"], "BLOCK")
        self.assertEqual(result["missing_providers"], ["deepseek", "qwen"])


if __name__ == "__main__":
    unittest.main()
