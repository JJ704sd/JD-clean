# JD 校准门禁

原始 JD 不能直接变成淘汰规则。筛选前，将每项要求整理为以下字段：

| 字段 | 必须回答的问题 |
|---|---|
| `criterion_id` | 是否有稳定唯一编号？ |
| `requirement_text` | JD 原文是什么？ |
| `job_relevance` | 为什么与实际工作直接相关？ |
| `category` | 必须项、优先项、面试验证项、行政确认项或禁止使用项？ |
| `resume_observable` | 简历能否可靠证明或否定？ |
| `accepted_evidence` | 哪些项目、动作、约束或结果算有效证据？ |
| `insufficient_evidence` | 哪些头衔、关键词或背景不能单独证明？ |
| `missing_information_action` | 没写时进入二审、面试验证还是不影响推进？ |
| `conflict_action` | 信息冲突时如何处理？ |
| `proxy_risk` | 是否可能借年龄、性别、学校或公司品牌等无关变量做判断？ |
| `owner` / `approved_by` | 谁负责解释，招聘负责人和用人经理是否确认？ |

只有与岗位核心工作直接相关、简历可观察、缺失处理已定义、无不当代理风险且经过招聘负责人和用人经理确认的条目，才能成为简历阶段硬门槛。

可从 [jd-profile-template.json](jd-profile-template.json) 复制结构。模板故意保留不可通过的替换标记和未批准状态，必须填写真实版本、审批人并完成批准后再运行：

```bash
python <skill-dir>/scripts/validate_jd_profile.py <jd-profile.json>
```

校验通过只代表结构和审批门禁完整，不证明标准本身合理；招聘负责人和用人经理仍需对岗位相关性负责。

## 未批准或缺失 JD

- 将 `screening_basis` 设为 `provisional_baseline`，`jd_hard_gates_approved=false`。
- 只做证据抽取与面试问题生成；使用 `second_review` 和 `U10_RUBRIC_AMBIGUITY`。
- 不输出 `do_not_advance_pending_human`，不把默认画像冒充企业招聘标准。

## 必须项未写

简历没写某项但没有反证时，记录 `U02_MUST_HAVE_MISSING`，进入二审或结构化面试；不得改写为“候选人不具备”。只有简历直接陈述的事实与已批准硬门槛冲突，才可记录明确不匹配。
