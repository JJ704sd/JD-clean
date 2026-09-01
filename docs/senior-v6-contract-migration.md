# 资深全栈 v6 契约迁移

> 历史迁移记录：v6 已由 [v7 契约迁移](senior-v7-contract-migration.md)取代为新写入版本；Go 硬门槛继续保留。

## 目标

将新写入规则从 `senior-fullstack-2026-08-25-v5` 迁移到 `senior-fullstack-2026-09-01-v6`：Go 从优先栈提升为硬门槛，Node.js 不再具备替代资格，物流/供应链的确定性评分权重提高至 15。

## 兼容矩阵

| Schema | Rubric | 读取 | 新写入 | 语义 |
|---|---|---:|---:|---|
| 1.1 | v2 | 是 | 否 | 旧审计记录 |
| 1.2 | v3 | 是 | 否 | 旧审计记录 |
| 1.2 | v4 | 是 | 否 | 旧审计记录 |
| 1.2 | v5 | 是 | 否 | Go/Node.js 旧门槛 |
| 1.2 | v6 | 是 | 是 | Go 硬门槛、物流 15 分 |

## 依赖图

```text
PDF/Markdown
  → 本地解析与脱敏
  → v6 prompt pack
  → MiniMax-M3 单份输出
  → v6 validator
  → evidence-score-2026-09-01-v1
  → SQLite / JSON / Markdown
```

validator 是所有新结果的门禁。评分器只接受 validator 所要求的完整 criterion 集合，不改变模型建议和人工复核要求。

## 迁移阶段

1. Expand：validator 同时读取 v2–v6，renderer 保留旧 `nodejs_only` 标签。
2. Migrate writer：CLI 只登记 v6 资深全栈任务，任务键包含 rubric 与评分版本。
3. Observe：SQLite 按版本保留结果；导出显示 `rubric_version` 和 `scoring_version`。
4. Contract：只有在确认不再需要旧审计读取后，才可另行移除 v2–v5；本次不执行收缩。

## 重筛与恢复

- 所有 v2–v5 资深全栈结果必须从原始简历重新登记为 v6，不能原地改字段或复用旧模型结论。
- 相同原文件的 v5 和 v6 任务键不同，可以并存并核对。
- 重筛失败不会覆盖旧记录；修复运行环境后使用 `retry-failed` 仅重置供应商明确未生成响应的任务。请求期间进程中断属于状态不确定，不自动重试。
- 已获得完整模型响应但契约无效的任务必须人工处理，不能自动再次调用模型。
