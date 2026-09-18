# 结构化输出契约

模型只输出三项：`evidence`、`uncertainties`、`interview_probes`。Python 负责补齐候选人标识、岗位版本、`recommendation`、摘要、人工复核和评分字段。

每项 `evidence` 必须包含 `criterion_id`、`state`、`excerpt`、`location`、`rationale`、`confidence` 和五字段 `evidence_factors`。五个事实字段为 `project_context`、`personal_action`、`method_or_tradeoff`、`result_scope`、`verifiable_impact`；没有原文支持时使用 `null`。不得输出模型自拟总分、等级或最终处置。

完整的角色记录示例见 [example-record.json](example-record.json)。运行后应使用 `scripts/validate_screening_output.py` 校验，使用 `scripts/score_screening_record.py` 确定性生成内部证据覆盖分。
