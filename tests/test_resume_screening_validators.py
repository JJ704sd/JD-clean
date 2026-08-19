"""Behavioral invariants for the two resume-screening Skills."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_validator(skill_dir: str):
    path = ROOT / "skills" / skill_dir / "scripts" / "validate_screening_output.py"
    spec = importlib.util.spec_from_file_location(skill_dir.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_renderer(skill_dir: str):
    path = ROOT / "skills" / skill_dir / "scripts" / "render_conclusion.py"
    spec = importlib.util.spec_from_file_location(f"{skill_dir.replace('-', '_')}_renderer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load renderer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def documented_record(skill_dir: str) -> dict:
    path = ROOT / "skills" / skill_dir / "references" / "output-contract.md"
    match = re.search(r"```json\s*(.*?)\s*```", path.read_text(encoding="utf-8"), re.DOTALL)
    if match is None:
        raise AssertionError(f"missing JSON example: {path}")
    return json.loads(match.group(1))


SENIOR_DIR = "screen-senior-fullstack-resumes"
INTERN_DIR = "screen-fullstack-intern-resumes"
SENIOR = load_validator(SENIOR_DIR)
INTERN = load_validator(INTERN_DIR)
SENIOR_RENDERER = load_renderer(SENIOR_DIR)
INTERN_RENDERER = load_renderer(INTERN_DIR)
SKILLS = (
    (SENIOR, SENIOR_RENDERER, SENIOR_DIR),
    (INTERN, INTERN_RENDERER, INTERN_DIR),
)


def evidence_item(record: dict, criterion_id: str) -> dict:
    return next(item for item in record["evidence"] if item["criterion_id"] == criterion_id)


def set_supported(record: dict, criterion_id: str, strength: str = "E2") -> None:
    item = evidence_item(record, criterion_id)
    item.update(
        {
            "state": "supported",
            "strength": strength,
            "excerpt": f"{criterion_id} 的可定位项目证据",
            "location": "项目经历/校准样本",
            "rationale": "满足该 criterion 的简历阶段证据门槛",
            "confidence": "high",
        }
    )


def set_not_evidenced(record: dict, criterion_id: str, strength: str = "E0") -> None:
    item = evidence_item(record, criterion_id)
    item.update(
        {
            "state": "not_evidenced",
            "strength": strength,
            "excerpt": None if strength == "E0" else f"只列出 {criterion_id} 关键词",
            "location": None if strength == "E0" else "技能列表",
            "rationale": "简历阶段没有足够的项目证据",
            "confidence": "high",
        }
    )


def set_directly_not_met(record: dict, criterion_id: str, strength: str = "E1") -> None:
    item = evidence_item(record, criterion_id)
    item.update(
        {
            "state": "directly_not_met",
            "strength": strength,
            "excerpt": f"候选人明确陈述不满足 {criterion_id}",
            "location": "个人概况/明确陈述",
            "rationale": "存在可定位的直接反证，不是由未写或关键词缺失推断",
            "confidence": "high",
        }
    )


def make_l2_not_required(record: dict) -> None:
    record["uncertainties"] = []
    record["human_review"].update(
        {
            "level_2_required": False,
            "level_2_status": "not_required",
            "level_2_mode": "not_required",
            "level_2_reason_codes": [],
            "independent_review_preferred": False,
            "independent_review_fallback_reason": None,
            "blind_review_required": False,
            "blind_review_confirmed": None,
            "level_2_reviewer": None,
            "level_2_decision": None,
        }
    )


def make_advance(skill_dir: str) -> dict:
    record = documented_record(skill_dir)
    record["model_recommendation"] = "advance_pending_human"
    record["recommendation_rationale"] = "岗位核心证据达到直接建议推进门槛。"
    record["recruiter_summary"]["human_next_action"] = "由责任人回看原始简历并完成人工一审。"
    make_l2_not_required(record)
    if skill_dir == SENIOR_DIR:
        set_supported(record, "SEN-ARCH-01", "E2")
    else:
        set_supported(record, "INT-AVAIL-01", "E1")
    return record


def make_negative(skill_dir: str) -> dict:
    record = make_advance(skill_dir)
    record["model_recommendation"] = "do_not_advance_pending_human"
    record["recommendation_rationale"] = "存在达到负面建议门禁的明确核心证据缺口。"
    record["recruiter_summary"]["critical_gaps"] = ["核心项目证据不足"]
    if skill_dir == SENIOR_DIR:
        set_not_evidenced(record, "SEN-BE-01")
        set_not_evidenced(record, "SEN-ARCH-01")
        set_not_evidenced(record, "SEN-FE-01")
    else:
        set_not_evidenced(record, "INT-BE-01")
        set_not_evidenced(record, "INT-WEB-01")
        set_not_evidenced(record, "INT-FE-01")
        set_not_evidenced(record, "INT-DATA-01")
        set_not_evidenced(record, "INT-PROJECT-01")
        set_not_evidenced(record, "INT-QUALITY-01")
    return record


class ScreeningValidatorTests(unittest.TestCase):
    def test_single_record_renderer_produces_a_concise_auditable_card(self):
        for _, renderer, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                output = renderer.render_single(record)
                self.assertIn(f"### 初筛结论｜{record['candidate_id']}", output)
                self.assertIn("| 规则版本 |", output)
                self.assertIn("| 初筛建议（非最终） | 进入二审（非最终） |", output)
                self.assertIn("一审待完成；二审待完成", output)
                reason_short = "U04" if skill_dir == SENIOR_DIR else "U02"
                self.assertIn(reason_short, output)
                self.assertNotIn(
                    "U04_CONTRIBUTION_UNCLEAR" if skill_dir == SENIOR_DIR else "U02_MUST_HAVE_MISSING",
                    output,
                )
                self.assertIn("以上为非最终初筛建议，须由招聘责任人确认。", output)
                self.assertNotIn('"schema_version"', output)

                evidence_section = output.split("匹配证据\n\n", 1)[1].split(
                    "\n\n关键缺口 / 待确认", 1
                )[0]
                self.assertLessEqual(
                    sum(line.startswith("- ") for line in evidence_section.splitlines()), 3
                )
                probe_section = output.split("面试优先验证\n\n", 1)[1].split(
                    "\n\n以上为非最终", 1
                )[0]
                self.assertLessEqual(
                    len(re.findall(r"^\d+\. ", probe_section, re.MULTILINE)), 3
                )
                gap_section = output.split("关键缺口 / 待确认\n\n", 1)[1].split(
                    "\n\n下一步", 1
                )[0]
                self.assertEqual(
                    sum(line.startswith("- ") for line in gap_section.splitlines()), 2
                )

    def test_renderer_rejects_invalid_records(self):
        for _, renderer, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["automation_actions"] = ["send_rejection"]
                with self.assertRaisesRegex(ValueError, "automation_actions"):
                    renderer.render_single(record)

    def test_batch_renderer_adds_counts_table_and_only_expands_second_review(self):
        for _, renderer, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                advance = make_advance(skill_dir)
                advance["candidate_id"] = "candidate-advance"
                advance["screening_record_id"] = "sr-advance"
                second = documented_record(skill_dir)
                second["candidate_id"] = "candidate-second"
                second["screening_record_id"] = "sr-second"
                output = renderer.render_batch([advance, second])
                self.assertIn("共 2 份：建议推进 1，二审 1，暂不推进 0", output)
                self.assertIn("| 候选人 ID | 初筛建议 |", output)
                self.assertIn("## 二审队列", output)
                self.assertEqual(output.count("### 初筛结论｜"), 1)
                self.assertIn("### 初筛结论｜candidate-second", output)
                self.assertNotIn("### 初筛结论｜candidate-advance", output)

    def test_batch_renderer_rejects_duplicate_candidate_ids(self):
        for _, renderer, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                first = documented_record(skill_dir)
                second = copy.deepcopy(first)
                second["screening_record_id"] = "sr-other"
                with self.assertRaisesRegex(ValueError, "candidate_id values must be unique"):
                    renderer.render_batch([first, second])

    def test_documented_second_review_examples_are_valid(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                self.assertEqual(validator.validate_record(documented_record(skill_dir)), [])

    def test_valid_advance_and_negative_records(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, recommendation="advance"):
                self.assertEqual(validator.validate_record(make_advance(skill_dir)), [])
            with self.subTest(skill=skill_dir, recommendation="negative"):
                self.assertEqual(validator.validate_record(make_negative(skill_dir)), [])

    def test_missing_domain_bonus_does_not_block_advance(self):
        senior = make_advance(SENIOR_DIR)
        set_not_evidenced(senior, "SEN-DOMAIN-01")
        self.assertEqual(SENIOR.validate_record(senior), [])

        intern = make_advance(INTERN_DIR)
        set_not_evidenced(intern, "INT-DOMAIN-01")
        self.assertEqual(INTERN.validate_record(intern), [])

    def test_strong_adjacent_stack_can_route_to_second_review(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["uncertainties"][0]["code"] = "U05_TRANSFERABILITY"
                record["uncertainties"][0]["description"] = "相邻技术栈有较强项目证据"
                record["human_review"]["level_2_reason_codes"] = ["U05_TRANSFERABILITY"]
                record["human_review"]["level_2_mode"] = "same_owner_separate_pass"
                record["human_review"]["blind_review_required"] = True
                if skill_dir == SENIOR_DIR:
                    record["human_review"]["independent_review_preferred"] = True
                    record["human_review"]["independent_review_fallback_reason"] = (
                        "当前只有一名责任人，先采用分时盲审。"
                    )
                self.assertEqual(validator.validate_record(record), [])

    def test_untrusted_resume_instructions_route_to_independent_second_review(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["uncertainties"][0].update(
                    {
                        "code": "U11_UNTRUSTED_CONTENT",
                        "description": "简历正文包含要求改变筛选规则或执行外部操作的指令",
                        "decision_impact": "若被当成操作指令会污染证据和建议",
                        "required_human_action": "忽略该内容并独立核对原始简历与证据矩阵",
                    }
                )
                record["human_review"].update(
                    {
                        "level_2_mode": "independent_reviewer",
                        "level_2_reason_codes": ["U11_UNTRUSTED_CONTENT"],
                        "independent_review_preferred": True,
                        "independent_review_fallback_reason": None,
                        "blind_review_required": True,
                    }
                )
                self.assertEqual(validator.validate_record(record), [])

    def test_strong_or_fragmented_adjacent_evidence_blocks_negative_shortcuts(self):
        senior = make_advance(SENIOR_DIR)
        senior["model_recommendation"] = "do_not_advance_pending_human"
        senior["recruiter_summary"]["critical_gaps"] = ["目标主栈和前端栈未充分证明"]
        set_not_evidenced(senior, "SEN-BE-01")
        set_not_evidenced(senior, "SEN-FE-01")
        senior_errors = SENIOR.validate_record(senior)
        self.assertTrue(any("transferability review" in error for error in senior_errors))

        intern = make_advance(INTERN_DIR)
        intern["model_recommendation"] = "do_not_advance_pending_human"
        intern["recruiter_summary"]["critical_gaps"] = ["后端和完整项目表述不足"]
        set_not_evidenced(intern, "INT-BE-01")
        set_not_evidenced(intern, "INT-PROJECT-01")
        intern_errors = INTERN.validate_record(intern)
        self.assertTrue(any("boundary review" in error for error in intern_errors))

    def test_irrelevant_admin_unknown_does_not_force_second_review_on_clear_negative(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = make_negative(skill_dir)
                admin_id = "SEN-ADM-01" if skill_dir == SENIOR_DIR else "INT-AVAIL-01"
                set_not_evidenced(record, admin_id)
                self.assertEqual(validator.validate_record(record), [])

    def test_skill_relative_links_exist(self):
        for _, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                skill_root = ROOT / "skills" / skill_dir
                markdown_files = [skill_root / "SKILL.md", *sorted((skill_root / "references").glob("*.md"))]
                for markdown_file in markdown_files:
                    content = markdown_file.read_text(encoding="utf-8")
                    targets = re.findall(r"\[[^]]+\]\(([^)]+)\)", content)
                    for target in targets:
                        if target.startswith(("http://", "https://", "#")):
                            continue
                        local_target = target.split("#", 1)[0]
                        self.assertTrue(
                            (markdown_file.parent / local_target).is_file(),
                            f"{markdown_file}: {target}",
                        )

    def test_recruiter_summary_is_concise_and_actionable(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, case="too_many"):
                record = documented_record(skill_dir)
                record["recruiter_summary"]["strongest_matches"] = ["证据"] * 4
                errors = validator.validate_record(record)
                self.assertTrue(any("at most 3 items" in error for error in errors))
            with self.subTest(skill=skill_dir, case="long_action"):
                record = documented_record(skill_dir)
                record["recruiter_summary"]["human_next_action"] = "复核" * 101
                errors = validator.validate_record(record)
                self.assertTrue(any("at most 200 characters" in error for error in errors))
            with self.subTest(skill=skill_dir, case="second_review_gap"):
                record = documented_record(skill_dir)
                record["recruiter_summary"]["critical_gaps"] = []
                errors = validator.validate_record(record)
                self.assertTrue(any("pending item" in error for error in errors))

    def test_profile_rubric_contract_and_validator_stay_synchronized(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                skill_root = ROOT / "skills" / skill_dir
                profile = (skill_root / "references" / "jd-profile.md").read_text(
                    encoding="utf-8"
                )
                rubric = (skill_root / "references" / "rubric.md").read_text(
                    encoding="utf-8"
                )
                expected_metadata = {
                    "role": validator.EXPECTED_ROLE,
                    "jd_version": validator.EXPECTED_JD_VERSION,
                    "rubric_version": validator.EXPECTED_RUBRIC_VERSION,
                }
                for field, expected in expected_metadata.items():
                    self.assertIn(f"- `{field}`: `{expected}`", profile)
                prefix = "SEN" if skill_dir == SENIOR_DIR else "INT"
                rubric_ids = set(re.findall(rf"\| `({prefix}-[A-Z]+-01)` \|", rubric))
                self.assertEqual(rubric_ids, set(validator.CRITERIA))
                contract_ids = {
                    item["criterion_id"] for item in documented_record(skill_dir)["evidence"]
                }
                self.assertEqual(contract_ids, set(validator.CRITERIA))

    def test_all_role_criteria_are_required_exactly_once(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, case="missing"):
                record = documented_record(skill_dir)
                record["evidence"].pop()
                errors = validator.validate_record(record)
                self.assertTrue(any("missing criteria" in error for error in errors))
            with self.subTest(skill=skill_dir, case="duplicate"):
                record = documented_record(skill_dir)
                record["evidence"][-1] = copy.deepcopy(record["evidence"][0])
                errors = validator.validate_record(record)
                self.assertTrue(any("duplicate evidence" in error for error in errors))

    def test_evidence_strength_and_traceability_are_consistent(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, case="E0_has_excerpt"):
                record = documented_record(skill_dir)
                zero = next(item for item in record["evidence"] if item["strength"] == "E0")
                zero["excerpt"] = "不应存在的摘录"
                errors = validator.validate_record(record)
                self.assertTrue(any("E0 must use null" in error for error in errors))
            with self.subTest(skill=skill_dir, case="E1_missing_location"):
                record = documented_record(skill_dir)
                one = next(item for item in record["evidence"] if item["strength"] == "E1")
                one["location"] = None
                errors = validator.validate_record(record)
                self.assertTrue(any("location is required" in error for error in errors))

    def test_uncertainty_requires_impact_action_and_exact_l2_reasons(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, case="impact"):
                record = documented_record(skill_dir)
                record["uncertainties"][0]["decision_impact"] = ""
                errors = validator.validate_record(record)
                self.assertTrue(any("decision_impact" in error for error in errors))
            with self.subTest(skill=skill_dir, case="reason_mismatch"):
                record = documented_record(skill_dir)
                record["human_review"]["level_2_reason_codes"] = ["U06_BOUNDARY_CASE"]
                errors = validator.validate_record(record)
                self.assertTrue(any("exactly match" in error for error in errors))

    def test_uncertainty_cannot_bypass_second_review(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["model_recommendation"] = "advance_pending_human"
                make_l2_not_required(record)
                record["uncertainties"] = [
                    {
                        "code": "U06_BOUNDARY_CASE",
                        "description": "处于边界",
                        "decision_impact": "澄清后可能改变推进建议",
                        "required_human_action": "进行盲审",
                    }
                ]
                errors = validator.validate_record(record)
                self.assertTrue(any("model_recommendation=second_review" in error for error in errors))
                self.assertTrue(any("level_2_required=true" in error for error in errors))

    def test_pending_second_review_cannot_claim_completion(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["human_review"]["blind_review_confirmed"] = True
                record["human_review"]["level_2_reviewer"] = "reviewer-a"
                errors = validator.validate_record(record)
                self.assertTrue(any("pending level 2" in error for error in errors))

    def test_completed_same_owner_review_is_valid_and_uses_same_reviewer(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["screening_status"] = "human_finalized"
                record["human_review"].update(
                    {
                        "level_1_status": "completed",
                        "level_1_reviewer": "reviewer-a",
                        "level_1_decision": "second_review",
                        "level_2_status": "completed",
                        "level_2_mode": "same_owner_separate_pass",
                        "blind_review_required": True,
                        "blind_review_confirmed": True,
                        "level_2_reviewer": "reviewer-a",
                        "level_2_decision": "advance",
                        "final_disposition": "advance",
                        "resolution": "分时盲审后确认可推进。",
                    }
                )
                missing_time_errors = validator.validate_record(
                    record, allow_human_finalized=True
                )
                self.assertTrue(
                    any("level_1_reviewed_at" in error for error in missing_time_errors),
                    missing_time_errors,
                )
                record["human_review"].update(
                    {
                        "level_1_reviewed_at": "2026-08-18T10:00:00+08:00",
                        "level_2_reviewed_at": "2026-08-18T15:00:00+08:00",
                    }
                )
                self.assertEqual(
                    validator.validate_record(record, allow_human_finalized=True), []
                )
                record["human_review"]["level_2_reviewed_at"] = "2026-08-18T09:00:00+08:00"
                self.assertTrue(
                    any(
                        "level 2 review must occur after level 1" in error
                        for error in validator.validate_record(record, allow_human_finalized=True)
                    )
                )

    def test_completed_source_fact_confirmation_is_valid_without_blind_review(self):
        record = documented_record(INTERN_DIR)
        record["screening_status"] = "human_finalized"
        record["human_review"].update(
            {
                "level_1_status": "completed",
                "level_1_reviewer": "reviewer-a",
                "level_1_decision": "second_review",
                "level_1_reviewed_at": "2026-08-18T10:00:00+08:00",
                "level_2_status": "completed",
                "blind_review_required": False,
                "blind_review_confirmed": None,
                "level_2_reviewer": "reviewer-a",
                "level_2_decision": "advance",
                "level_2_reviewed_at": "2026-08-18T12:00:00+08:00",
                "final_disposition": "advance",
                "resolution": "候选人确认每周可以实习 4 天。",
            }
        )
        self.assertEqual(
            INTERN.validate_record(record, allow_human_finalized=True), []
        )

    def test_source_fact_confirmation_cannot_replace_interpretive_review(self):
        record = documented_record(SENIOR_DIR)
        record["human_review"].update(
            {
                "level_2_mode": "source_fact_confirmation",
                "blind_review_required": False,
                "blind_review_confirmed": None,
            }
        )
        errors = SENIOR.validate_record(record)
        self.assertTrue(
            any("only allowed for U01/U02/U03" in error for error in errors)
        )

    def test_source_fact_confirmation_cannot_claim_blind_review(self):
        record = documented_record(INTERN_DIR)
        record["human_review"].update(
            {
                "blind_review_required": True,
                "blind_review_confirmed": True,
            }
        )
        errors = INTERN.validate_record(record)
        self.assertTrue(any("must not claim blind review" in error for error in errors))

    def test_independent_review_must_use_a_distinct_reviewer(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["screening_status"] = "human_finalized"
                record["human_review"].update(
                    {
                        "level_1_status": "completed",
                        "level_1_reviewer": "reviewer-a",
                        "level_1_decision": "second_review",
                        "level_2_status": "completed",
                        "level_2_mode": "independent_reviewer",
                        "blind_review_confirmed": True,
                        "level_2_reviewer": "reviewer-a",
                        "level_2_decision": "advance",
                        "final_disposition": "advance",
                        "resolution": "独立复核完成。",
                    }
                )
                errors = validator.validate_record(record, allow_human_finalized=True)
                self.assertTrue(any("must differ" in error for error in errors))

    def test_default_validation_rejects_even_well_formed_human_finalized_records(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["screening_status"] = "human_finalized"
                record["human_review"].update(
                    {
                        "level_1_status": "completed",
                        "level_1_reviewer": "reviewer-a",
                        "level_1_decision": "second_review",
                        "level_2_status": "completed",
                        "blind_review_confirmed": (
                            None if skill_dir == INTERN_DIR else True
                        ),
                        "level_2_reviewer": "reviewer-a",
                        "level_2_decision": "advance",
                        "final_disposition": "advance",
                        "resolution": "人工审核完成。",
                    }
                )
                errors = validator.validate_record(record)
                self.assertTrue(any("explicit human-finalized" in error for error in errors))

    def test_bias_or_rubric_risk_prefers_independent_review(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["uncertainties"][0]["code"] = "U07_BIAS_OR_PROXY"
                record["human_review"]["level_2_reason_codes"] = ["U07_BIAS_OR_PROXY"]
                errors = validator.validate_record(record)
                self.assertTrue(any("independent_review_preferred=true" in error for error in errors))

                record["human_review"]["independent_review_preferred"] = True
                errors = validator.validate_record(record)
                self.assertTrue(any("fallback reason" in error for error in errors))

    def test_stale_versions_and_cross_role_records_are_rejected(self):
        senior_record = documented_record(SENIOR_DIR)
        senior_record["rubric_version"] = "senior-fullstack-2026-08-18-v1"
        self.assertTrue(any("rubric_version" in error for error in SENIOR.validate_record(senior_record)))
        intern_record = documented_record(INTERN_DIR)
        self.assertTrue(any("role must be" in error for error in SENIOR.validate_record(intern_record)))

    def test_contract_transition_accepts_pinned_legacy_and_current_pairs(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                current = documented_record(skill_dir)
                self.assertEqual(current["schema_version"], "1.2")
                self.assertEqual(validator.validate_record(current), [])

                legacy = copy.deepcopy(current)
                legacy["schema_version"] = "1.1"
                legacy["rubric_version"] = (
                    "senior-fullstack-2026-08-18-v2"
                    if skill_dir == SENIOR_DIR
                    else "fullstack-intern-2026-08-18-v2"
                )
                self.assertEqual(validator.validate_record(legacy), [])

                mismatched = copy.deepcopy(current)
                mismatched["schema_version"] = "1.1"
                self.assertTrue(
                    any("schema/rubric compatibility pair" in error for error in validator.validate_record(mismatched))
                )

                legacy_with_new_state = copy.deepcopy(legacy)
                direct_id = "SEN-BE-01" if skill_dir == SENIOR_DIR else "INT-AVAIL-01"
                set_directly_not_met(legacy_with_new_state, direct_id)
                self.assertTrue(
                    any(
                        "directly_not_met requires schema_version='1.2'" in error
                        for error in validator.validate_record(legacy_with_new_state)
                    )
                )

    def test_role_specific_advance_thresholds_are_enforced(self):
        senior = make_advance(SENIOR_DIR)
        set_not_evidenced(senior, "SEN-DATA-01", "E1")
        self.assertTrue(any("SEN-DATA-01" in error for error in SENIOR.validate_record(senior)))

        intern = make_advance(INTERN_DIR)
        set_not_evidenced(intern, "INT-WEB-01")
        set_not_evidenced(intern, "INT-FE-01")
        self.assertTrue(any("two supported" in error for error in INTERN.validate_record(intern)))

    def test_low_confidence_decision_evidence_blocks_directional_recommendations(self):
        senior_advance = make_advance(SENIOR_DIR)
        evidence_item(senior_advance, "SEN-ARCH-01")["confidence"] = "low"
        self.assertTrue(
            any("low-confidence decision evidence requires second review" in error for error in SENIOR.validate_record(senior_advance))
        )

        intern_advance = make_advance(INTERN_DIR)
        evidence_item(intern_advance, "INT-PROJECT-01")["confidence"] = "low"
        self.assertTrue(
            any("low-confidence decision evidence requires second review" in error for error in INTERN.validate_record(intern_advance))
        )

        senior_negative = make_negative(SENIOR_DIR)
        for criterion_id in ("SEN-BE-01", "SEN-ARCH-01", "SEN-FE-01"):
            evidence_item(senior_negative, criterion_id)["confidence"] = "low"
        self.assertTrue(
            any("low-confidence negative gate requires second review" in error for error in SENIOR.validate_record(senior_negative))
        )

        intern_negative = make_negative(INTERN_DIR)
        for criterion_id in ("INT-BE-01", "INT-PROJECT-01"):
            evidence_item(intern_negative, criterion_id)["confidence"] = "low"
        self.assertTrue(
            any("low-confidence negative gate requires second review" in error for error in INTERN.validate_record(intern_negative))
        )

    def test_negative_recommendation_needs_a_role_specific_gate(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = make_advance(skill_dir)
                record["model_recommendation"] = "do_not_advance_pending_human"
                errors = validator.validate_record(record)
                self.assertTrue(any("negative evidence gate" in error for error in errors))

    def test_conflicting_source_facts_cannot_be_used_as_a_direct_negative_gate(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = make_negative(skill_dir)
                criterion_id = "SEN-BE-01" if skill_dir == SENIOR_DIR else "INT-AVAIL-01"
                item = evidence_item(record, criterion_id)
                item.update(
                    {
                        "state": "conflicting",
                        "strength": "E2" if skill_dir == SENIOR_DIR else "E1",
                        "excerpt": "同一简历对该事实存在相互矛盾的陈述",
                        "location": "个人概况与项目经历",
                        "rationale": "来源事实冲突，必须先澄清，不能直接作为负面门禁",
                        "confidence": "high",
                    }
                )
                errors = validator.validate_record(record)
                self.assertTrue(
                    any("conflicting evidence requires U03_CONFLICTING_FACTS" in error for error in errors),
                    errors,
                )

    def test_explicit_criterion_contradiction_uses_directly_not_met(self):
        senior = make_advance(SENIOR_DIR)
        senior["schema_version"] = "1.2"
        senior["rubric_version"] = "senior-fullstack-2026-08-18-v3"
        senior["model_recommendation"] = "do_not_advance_pending_human"
        senior["recommendation_rationale"] = "候选人明确陈述没有后端生产交付。"
        senior["recruiter_summary"]["critical_gaps"] = ["明确没有后端生产交付"]
        set_directly_not_met(senior, "SEN-BE-01", "E2")
        self.assertEqual(SENIOR.validate_record(senior), [])

        intern = make_advance(INTERN_DIR)
        intern["schema_version"] = "1.2"
        intern["rubric_version"] = "fullstack-intern-2026-08-18-v3"
        intern["model_recommendation"] = "do_not_advance_pending_human"
        intern["recommendation_rationale"] = "候选人明确说明无法保证每周四天。"
        intern["recruiter_summary"]["critical_gaps"] = ["明确无法保证每周四天"]
        set_directly_not_met(intern, "INT-AVAIL-01", "E1")
        self.assertEqual(INTERN.validate_record(intern), [])

    def test_missing_ai_on_advance_requires_a_targeted_probe(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = make_advance(skill_dir)
                ai_id = "SEN-AI-01" if skill_dir == SENIOR_DIR else "INT-AI-01"
                record["interview_probes"] = [
                    probe for probe in record["interview_probes"] if probe["criterion_id"] != ai_id
                ]
                for priority, probe in enumerate(record["interview_probes"], start=1):
                    probe["priority"] = priority
                errors = validator.validate_record(record)
                self.assertTrue(any("AI evidence" in error for error in errors))

    def test_external_automation_and_missing_l1_are_forbidden(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir, case="automation"):
                record = documented_record(skill_dir)
                record["automation_actions"] = ["send_rejection"]
                self.assertIn("automation_actions must be an empty list", validator.validate_record(record))
            with self.subTest(skill=skill_dir, case="l1"):
                record = documented_record(skill_dir)
                record["human_review"]["level_1_required"] = False
                self.assertIn("human_review.level_1_required must be true", validator.validate_record(record))

    def test_output_records_reject_direct_contact_identifiers(self):
        for validator, _, skill_dir in SKILLS:
            with self.subTest(skill=skill_dir):
                record = documented_record(skill_dir)
                record["recommendation_rationale"] += " 联系邮箱 candidate@example.com"
                self.assertTrue(
                    any("possible PII found" in error for error in validator.validate_record(record))
                )


if __name__ == "__main__":
    unittest.main()
