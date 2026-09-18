# 结构化筛选记录契约

默认使用结论卡；用户要求机器留档、批量导出或审计时才输出 JSON。当前筛选记录使用 Schema 1.4，包含本岗位的校准字段。评分卡是脚本根据筛选记录派生的独立审计结果，不写入模型生成的 screening_record。当前完整示例见 example-record-v6.json；example-record.json 和 example-scorecard.json 保留为 Rubric v5 历史示例。

当前记录使用 Rubric `operations-devops-rubric-2026-09-17-v1`。历史 Rubric v1/v2/v3/v4/v5 记录仍可按原规则校验和阅读，但不得按新规则追溯计算分数。评分脚本只接受当前 rubric_version；权重或维度改变时，必须发布新的 rubric_version 与 scoring_version，并为新增字段/维度升级对应 schema。

## 关键字段

- role 固定为 operations-devops-engineer。
- screening_basis 只能是 approved_jd 或 provisional_baseline。
- jd_hard_gates_approved 必须与岗位画像一致。草稿 profile 必须设为 false。
- screening_status 固定为 non_final。
- recommendation 只能是 advance_pending_human、second_review、do_not_advance_pending_human。
- rubric_version 必须是当前岗位 Rubric 版本；评分脚本只为当前版本记录计算独立 scorecard。
- 当前 Schema 1.4 / Rubric 2026-09-17-v1 的 evidence 必须恰好覆盖 20 个 criterion，每项一次。历史 Rubric v1/v2 使用 Schema 1.2 和 13 项；Rubric v3/v4 使用 Schema 1.2 和 16 项；Rubric v5 使用 Schema 1.3 和 17 项。
- evidence state 只能为 supported、not_evidenced、conflicting、directly_not_met。岗位能力的 supported 需 E2/E3；学历与专业 OPS-EDU-01 的明确文本事实可用 E1 标记 supported 或 directly_not_met。E0 只能用于 not_evidenced；E1 通常表示未提供足够能力证据。E1-E3 必须有最短原文和位置，E0 使用 null。
- hard_gate_conflicts 只记录已批准 profile 中的 must_have 或 exclude_if_evidenced 条目，并引用简历明确直接反证。不得把缺失或模糊信息写入其中。
- uncertainties 只能使用 U01_PARSE_QUALITY、U02_MUST_HAVE_MISSING、U03_CONFLICTING_FACTS、U04_CONTRIBUTION_UNCLEAR、U05_TRANSFERABILITY、U06_BOUNDARY_CASE、U07_BIAS_OR_PROXY、U08_DIMENSION_CONFLICT、U09_ROLE_AMBIGUITY、U10_RUBRIC_AMBIGUITY、U11_UNTRUSTED_CONTENT；每条必须包含 description、decision_impact、required_human_action，并要求二审。
- interview_probes 为 3–6 个对象，每项包含 criterion_id、question、expected_signal。
- sensitive_attributes_used 必须为 false，automation_actions 必须为空数组。

## 建议与审核一致性

- provisional_baseline 必须设置 recommendation 为 second_review、jd_hard_gates_approved=false，并包含 U10_RUBRIC_AMBIGUITY；不得输出暂不推进。
- 任何 uncertainty、低置信度方向性证据或负面建议必须要求二审，并将原因码写入 human_review。
- do_not_advance_pending_human 必须有 approved_jd 且至少一个与批准 must_have 或 exclude_if_evidenced 直接对应的 hard_gate_conflict；同时需要独立二审。不得由评分、E0/E1 或关键词缺失触发。
- 初筛 human_review 的一审字段、审核人、决定、时间、最终处理及冲突裁决字段必须为空或 pending；不得伪造 human_finalized。
- summary.top_strengths 和 summary.key_gaps 各不超过 3 条；summary.human_next_action 只写一个动作。
- top_strengths 必须引用有 E2/E3 支持的岗位能力 criterion，不得把学历、年限、行业背景或外包行政信号列为优势。每条 key_gaps 必须包含 gap_type：evidence_gap 对应 not_evidenced，conflicting_facts 对应 conflicting，direct_contradiction 对应 directly_not_met。已支持维度中的待确认细节放入面试追问，不放入 key_gaps；OPS-SCALE-01 和 OPS-DOMAIN-01 属于加分项，缺少证据不得列为 key gap。

保存 JSON 后，先运行 validate_screening_output.py。仅当校验通过后，才能运行 render_conclusion.py。校验只检查结构和门禁一致性，不证明证据解释正确，也不代表已完成人审。

用 `score_screening_record.py <record.json>` 生成独立 scorecard。其 `screening_record_id`、`candidate_id` 和 `rubric_version` 用于追溯来源；`score` 为 0–100 的证据覆盖分，`grade` 为内部档位，`components` 为各维度加权得分，`weights` 为百分比权重的整数值（如 10 表示 10%），`review_status` 仅复制非最终建议。分数由 Python 根据 evidence 重算，不能从模型返回值读取；它不改变筛选记录、推荐状态、hard gate 或人工复核要求。

当前 evidence 包含 20 个 criterion ID，其中 19 个维度计分，`OPS-OUTSOURCE-01` 权重为 0，仅记录明确外包/派遣证据。5 年及以上经验、独立责任、办公 IT、办公网络/VPN、项目规模、AI 与行业背景按当前 Rubric 作为校准或偏好维度，不作为自动门槛。第一学历本科及以上与明确外包经历只有双签批准后才能作为门槛或排除依据；画像仍是 draft 时，建议状态不得基于这些待批准规则变成不推进。历史记录按各自 rubric 和 schema 版本读取，不使用当前评分脚本追溯改分。
