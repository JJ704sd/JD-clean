from __future__ import annotations

import unittest
from pathlib import Path

from resume_screening.prompts import build_system_prompt
from resume_screening.queue import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[1]


class PromptContractTests(unittest.TestCase):
    def test_senior_prompt_v4_requests_facts_without_model_strength(self):
        prompt = build_system_prompt(
            ROOT,
            role="senior-fullstack-engineer",
            candidate_id="candidate-test",
            jd_version="senior-fullstack-2026-08-14-v1",
            rubric_version="senior-fullstack-2026-09-03-v9",
            prompt_version=PROMPT_VERSION,
        )

        self.assertEqual(PROMPT_VERSION, "resume-screening-prompt-2026-09-01-v4")
        self.assertIn("<evidence-extraction-contract>", prompt)
        self.assertIn(
            "顶层字段只能是 evidence、uncertainties、interview_probes", prompt
        )
        self.assertIn("Python 将负责建议、评分、摘要和人工审核字段", prompt)
        self.assertIn("不得输出 strength", prompt)
        self.assertIn("evidence_factors", prompt)
        self.assertIn("五项事实全部具备才可为 E3", prompt)
        self.assertIn("logistics_flexible_backend", prompt)
        self.assertIn("非 Go 后端", prompt)

    def test_ai_product_manager_prompt_v3_requests_only_evidence_payload_fields(self):
        prompt = build_system_prompt(
            ROOT,
            role="ai-product-manager",
            candidate_id="candidate-test",
            jd_version="ai-pm-2026-08-v2",
            rubric_version="ai-pm-rubric-2026-08-18-v3",
        )

        self.assertIn("<evidence-extraction-contract>", prompt)
        self.assertIn(
            "顶层字段只能是 evidence、uncertainties、interview_probes", prompt
        )
        self.assertIn("Python 将负责建议、评分、摘要和人工审核字段", prompt)


if __name__ == "__main__":
    unittest.main()
