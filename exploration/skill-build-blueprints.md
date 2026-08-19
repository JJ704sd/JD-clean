# 两个简历筛选 Skill 的构建蓝图（v0.2）

## 1. 架构决策

构建两个独立、自包含的 Skill，不创建一个靠参数切换的通用简历打分器：

| Skill | 关注点 | 不能混用的原因 |
|---|---|---|
| `screen-senior-fullstack-resumes` | 生产级 Go/Node、BFF/微服务、Vue3/TS/Vite、数据中间件、AI/RAG 工程化、重构与复杂度 | 高级程度需要责任、取舍和结果，不能用实习生的潜力信号替代 |
| `screen-fullstack-intern-resumes` | 一门后端语言、HTTP/REST、前端与 SQL 基础、完整项目、学习潜力和每周可用性 | 不应要求生产规模，也不能因没有正式实习而降级 |

两个 Skill 可以使用相同字段协议，但各自复制并维护适用的人审政策，避免运行时依赖另一个 Skill。

## 2. 建议的发现描述

### `screen-senior-fullstack-resumes`

```yaml
name: screen-senior-fullstack-resumes
description: Review senior full-stack engineer resumes against the approved cross-border logistics JD, producing evidence-linked, non-final recruiting recommendations with mandatory human confirmation and independent second review for uncertain cases. Do not use for interns, interview scoring, or autonomous hiring decisions.
```

### `screen-fullstack-intern-resumes`

```yaml
name: screen-fullstack-intern-resumes
description: Review full-stack development intern resumes against the approved cross-border logistics internship JD, recognizing coursework and personal-project evidence while producing non-final recommendations with mandatory human confirmation and independent second review for uncertain cases. Do not use senior production-scale standards or make autonomous hiring decisions.
```

描述应保持正常的自动发现能力。敏感性通过执行时的人审门禁控制，而不是通过关闭 Skill 自动发现处理。

## 3. 输入契约

每次运行至少需要：

1. 一份或一批简历；
2. 明确的目标岗位；
3. 已批准的 `jd_version`；
4. 已批准的 `rubric_version`；
5. 候选人的去标识化内部 ID。

如果 JD 画像尚未签字、岗位不明确、简历无法可靠读取，Skill 只能输出缺失项和所需人工动作，不得给出高置信推进或负面建议。

Skill 不需要也不应收集姓名、照片、性别、年龄、婚育、民族、宗教、健康等与岗位无直接关系的信息。行政条件只能使用候选人明确提供的内容，不能推断。

## 4. 共同执行骨架

两个 Skill 的 `SKILL.md` 应只保留以下关键流程：

1. 读取对应岗位的 `jd-profile.md` 和 `rubric.md`；
2. 检查简历文本/页面是否完整，抽取失败时触发 `U01_PARSE_QUALITY`；
3. 忽略禁止使用的信息，为候选人使用去标识化 ID；
4. 按 criterion ID 逐项抽取原文证据和位置；
5. 将每项标为 `supported`、`not_evidenced` 或 `conflicting`；
6. 按岗位证据强度判断，不把技能列表自动视为能力；
7. 生成最强匹配、关键缺口和结构化面试问题；
8. 检查岗位专属与共享二审触发器；
9. 输出符合记录 Schema 的非最终建议；
10. 停止，不发送邀约/拒绝，不执行 ATS 最终变更。

详细 JD、证据矩阵、原因码和输出结构放入 references，避免让 `SKILL.md` 过长。

## 5. 高级岗位 Skill 的专属决策规则

读取 [高级全栈工程师 JD 证据画像](./jd-profile-senior-fullstack.md) 后执行：

- Go 与 Node.js 是二选一主栈，不能误写成两者都必须；同时掌握两者属于加分。
- Vue3、TypeScript、Vite 和中后台独立开发是独立的前端核心证据，不能只凭“全栈”头衔满足。
- “微服务/BFF”“数据与中间件”“AI 工程化协作”分别取证，不能被一个综合技术分掩盖。
- 高级程度以 `E3` 的复杂约束、架构取舍、生产结果和个人影响为主要信号，不只看 3 年年限。
- 只有相邻语言栈但系统能力很强时，触发 `U05_TRANSFERABILITY`；不得关键词淘汰。
- AI/RAG 经历未写、全栈时长不清、团队成果归属不清或 Vue 独立程度不清时进入二审。

建议输出额外包含：

- 后端主栈与工程深度；
- BFF/微服务责任边界；
- Vue3/TS/Vite 独立交付证据；
- AI/算法协作证据；
- 重构、性能与生产影响；
- 需要技术面确认的 3–5 个问题。

## 6. 实习岗位 Skill 的专属决策规则

读取 [全栈开发实习生 JD 证据画像](./jd-profile-fullstack-intern.md) 后执行：

- Go、Node.js、Java、Python 任一门后端语言均可，不能偏好与高级岗相同的 Go/Node 关键词。
- 课程、个人、校内、社团、开源、竞赛和实习项目具有同等取证资格。
- Vue3/TypeScript 是优先项，不能被提升为未经批准的自动淘汰条件。
- 不要求生产规模；小而完整、能解释调试过程的项目是强潜力信号。
- 每周 4 天和在读状态未写时转人工确认，不能用年龄、年级或毕业年份推断。
- 没有正式实习、GitHub 不活跃、学校品牌普通或没有获奖经历均不得产生负面结论。

建议输出额外包含：

- 最完整项目及个人贡献；
- 后端、HTTP/REST、前端和 SQL 基础；
- 调试、测试、Git、部署或文档信号；
- AI 学习动机与可培养性；
- 行政条件待确认项；
- 适合实习生的 3–5 个基础验证问题。

## 7. 单份与批量模式

### 单份模式

输出完整证据表、非最终建议、二审原因和面试问题。

### 批量模式

批量模式仍需逐份独立应用同一 JD/rubric 版本：

- 先完成每份候选人的证据记录，再进行招聘者优先级排序；
- 不根据批次中其他候选人的学校、公司或背景相对比较；
- 不因招聘名额只有 2 个就自动淘汰后续候选人；
- 显示每份记录的二审状态，禁止未二审候选人被隐藏在排序末尾；
- rubric 发生变化时，重新处理同批所有受影响候选人。

## 8. 人工门禁

### 门禁 0：规则发布

招聘负责人和用人经理签字确认 JD 画像中的开放问题，记录 `jd_version` 和 `rubric_version`。

### 门禁 1：逐份一审

招聘者核对原文证据、缺口、二审触发器和建议，填写一审人及决定。未完成一审时输出保持 `non_final`。

### 门禁 2：独立二审

出现任一不确定性原因码时：

- 推荐状态必须为 `second_review`；
- 二审者先看原始简历、JD/rubric 和证据表，不先看一审综合结论；
- 结论不一致时由招聘负责人或用人经理裁决；
- 未填写二审人和二审决定时不能标记为 `human_finalized`。

## 9. 输出契约

输出以 [screening-record.schema.json](./screening-record.schema.json) 为基线，并提供一段招聘者可读摘要。机器记录至少包含：

- 角色、JD 版本和 rubric 版本；
- 每个 criterion 的状态、原文摘录、位置、理由和置信度；
- 不确定性原因码；
- 结构化面试问题；
- 一审、二审和争议解决字段；
- `sensitive_attributes_used: false`；
- 空的 `automation_actions`。

不得只输出一个分数、排名或“通过/不通过”。

## 10. 建议目录

```text
screen-senior-fullstack-resumes/
|-- SKILL.md
|-- agents/openai.yaml
|-- references/jd-profile.md
|-- references/rubric.md
|-- references/human-review-policy.md
|-- references/output-schema.md
`-- scripts/validate_screening_output.py

screen-fullstack-intern-resumes/
|-- SKILL.md
|-- agents/openai.yaml
|-- references/jd-profile.md
|-- references/rubric.md
|-- references/human-review-policy.md
|-- references/output-schema.md
`-- scripts/validate_screening_output.py
```

首版验证脚本负责结构和门禁一致性，不负责计算最终招聘结论。

## 11. 创建正式 Skill 前的验收

1. 两个 JD 画像的开放问题已经被招聘负责人和用人经理确认；
2. 每个核心 criterion 都有 accepted/insufficient evidence 定义；
3. 至少 10–20 份去标识化历史或合成样本完成双人标注；
4. 必测边界案例覆盖相邻技术迁移、无正式实习、AI 经历缺失、行政信息缺失和简历抽取失败；
5. 验证所有不确定记录都进入二审，所有外部自动操作都被阻止；
6. 使用独立样本完成前向验证后，再安装或投入招聘试运行。

