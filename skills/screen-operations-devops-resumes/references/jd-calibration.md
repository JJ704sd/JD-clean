# JD 校准门禁

原始 JD 和 Skill 默认画像不自动等于已批准的简历淘汰规则。使用岗位前，招聘负责人和用人经理应在 jd-profile-template.json 中逐项确认要求与处理方式，并使用 validate_jd_profile.py 校验。

每项标准必须定义：

| 字段 | 需要明确的内容 |
|---|---|
| criterion_id | 固定且唯一的维度编号 |
| requirement_text | 直接对应的岗位要求 |
| job_relevance | 为什么与实际工作有关 |
| category | requires_calibration、must_have、exclude_if_evidenced、preferred、interview_only、administrative 或 prohibited |
| resume_observable | 是否能从简历可靠判断 |
| accepted_evidence | 什么样的项目、动作、约束、结果可作为证据 |
| insufficient_evidence | 哪些头衔、技能名或自评不足以证明 |
| missing_information_action | 缺失时二审、面试确认或不影响建议 |
| conflict_action | 冲突时的处理方式 |
| proxy_risk | 是否存在敏感属性或代理变量风险 |
| owner / approved_by | 负责解释和批准的角色 |

只有与岗位核心工作直接相关、可从简历可靠观察、缺失与冲突处理明确、无未解决代理风险，并由招聘负责人及用人经理共同批准的要求，才可作为简历阶段 hard gate。

当前岗位画像仍是草稿。没有通过校验的 approved profile 时：

- screening_basis 使用 provisional_baseline，jd_hard_gates_approved 为 false；
- 只整理证据、缺口、追问和待校准事项；model recommendation 必须为 second_review；
- 不得使用 do_not_advance_pending_human；
- 最新 JD 的本科及以上、计算机相关专业和 5 年及以上相关运维/DevOps/SRE/平台经验，以及办公电脑/IT、办公网络/VPN职责，须逐项校准；在双签批准前只能记录证据，不得据此形成不推进结论。年限不足、缺失或日期不完整不得单独触发不推进；存在日期或职责冲突时才按不确定性二审。计算机相关专业单独记录，不自动视为已批准硬门槛。
- 独立承担/核心责任、AI 深度使用或 AI 产品经验、运维项目复杂度/业务量/用户规模、物流/供应链/跨境行业经验属于偏好或加分项；Kubernetes、服务网格、Ansible/Terraform 不是默认硬门槛。它们不得配置成简历阶段 must_have 或 exclude_if_evidenced。

批准后的 profile 必须具有新的 jd_version、两名不同角色批准人及逐项规则。校验通过只说明字段完整和门禁一致，不证明招聘标准本身无偏或合理。
