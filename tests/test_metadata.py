from __future__ import annotations

import unittest
from pathlib import Path

from resume_screening.metadata import infer_candidate_name, infer_role


class ResumeMetadataTests(unittest.TestCase):
    def test_boss_filename_yields_local_display_name_and_role(self):
        fullstack = Path("【全栈工程师_深圳 15-25K】唐先生 4年.pdf")
        product = Path("【ai产品经理_深圳 15-25K】陈熙纯 4年.pdf")

        self.assertEqual(infer_candidate_name(fullstack), "唐先生")
        self.assertEqual(infer_role(fullstack), "senior-fullstack-engineer")
        self.assertEqual(infer_candidate_name(product), "陈熙纯")
        self.assertEqual(infer_role(product), "ai-product-manager")

    def test_unrelated_document_is_not_treated_as_a_resume(self):
        invoice = Path("电子发票.pdf")

        self.assertIsNone(infer_candidate_name(invoice))
        self.assertIsNone(infer_role(invoice))

    def test_generic_resume_filename_can_supply_name_without_guessing_role(self):
        resume = Path("翟建钧的简历-v2.1(1).pdf")

        self.assertEqual(infer_candidate_name(resume), "翟建钧")
        self.assertIsNone(infer_role(resume))


if __name__ == "__main__":
    unittest.main()
