# 全栈工程师简历筛选 Rubric

- 当前版本：`senior-fullstack-2026-09-11-v13`

## 证据强度

| 级别 | 定义 |
|---|---|
| `E0` | 没有相关信息 |
| `E1` | 技能列表、自评、头衔、系统名或孤立关键词 |
| `E2` | 同一项目中有项目背景、个人动作、对象和责任边界 |
| `E3` | `E2` 基础上同时具备方法/取舍、结果口径和可核验影响 |

## 证据矩阵

| ID | 维度 | v13 判断 | 用途 |
|---|---|---|---|
| `SEN-EXP-01` | 应用研发年限 | 日期与研发职责支持 3–7 年为高匹配；明确范围外为 `directly_not_met` | 20% 高权重评分 |
| `SEN-BE-01` | 后端与语言选择 | 记录真实后端交付，并保留对换语言/转栈的明确接受、犹豫或抵触原文 | 排除识别/追问 |
| `SEN-ARCH-01` | 微服务/BFF | 服务边界、接口、拆分或架构责任至少 `E2` | 技术深度 |
| `SEN-FE-01` | 前端交付 | 页面、组件、状态或工程化个人交付至少 `E2` | 全栈深度 |
| `SEN-DATA-01` | 数据与中间件 | 数据模型、查询、缓存、异步或一致性动作至少 `E2` | 技术深度 |
| `SEN-AI-01` | AI 深度使用/产品经验 | AI 工作流、产品或工程项目有个人动作至少 `E2` | 加分 |
| `SEN-DOMAIN-01` | 物流/供应链 | 物流业务项目有可归属个人的交付至少 `E2` | 高影响加分 |
| `SEN-LEVEL-01` | 独立/核心项目责任 | 独立承担、主导关键链路或核心开发责任至少 `E2` | 重点优先 |
| `SEN-ADM-01` | 学历 | 明确本科及以上，`E1` 即可 | 硬门槛 |

## 决策规则

`qualification_dimensions` 只包含 `education`，状态为 `met`、`not_met` 或 `unclear`。学历 `not_met` 生成 `do_not_advance_pending_human`；学历 `unclear` 进入 `second_review`。

`experience_fit_signal` 记录 `preferred_3_to_7_years`、`outside_preferred_range`、`not_evidenced` 或 `unclear`。经验维度权重为 20%；它显著影响总分和优先级，但任何经验状态都不能单独触发暂不推进或二审。

`language_acceptance=resistant` 或 `employment_model=outsourcing_evidenced` 也生成 `do_not_advance_pending_human`。这两项只允许由明确、可定位原文触发；缺少相关信息不得推断为负面。

`project_ownership_signal`、`ai_bonus_signal` 和 `logistics_experience` 只影响优先排序、摘要和追问，不单独改变推进状态。物流缺失不得作为硬缺口。

确定性评分仍只表示 9 个证据维度的覆盖程度，不能替代硬门槛、排除信号或人工复核。
