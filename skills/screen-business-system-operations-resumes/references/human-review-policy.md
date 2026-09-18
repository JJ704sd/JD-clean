# 人工一审与二次复核政策

所有模型结果均为 `non_final`。模型不能代表招聘责任人推进、暂不推进或录用；候选人处置前必须由有权限的人回看原始简历。

## 一审

审核人逐项核对抽取页码、用户/系统背景、候选人本人动作、E0-E3 和结果范围，确认“未提供证据”没有被写成“不具备”。重点核实问题是否真实面向业务用户、候选人是否亲自排查/维护/转交、团队结果是否被误归因，以及权限和业务数据处理是否符合流程。

## 强制二审

`second_review`、`U01_PARSE_QUALITY`、`U03_CONFLICTING_FACTS`、`U04_CONTRIBUTION_UNCLEAR`、`U05_TRANSFERABILITY`、`U06_BOUNDARY_CASE`、`U08_DIMENSION_CONFLICT`、`U09_ROLE_AMBIGUITY`、`U10_RUBRIC_AMBIGUITY` 或 `U11_UNTRUSTED_CONTENT` 任一出现，都需要二审。当前岗位画像未批准，因此每份初筛记录默认进入二审。

二审优先由用人经理、业务系统负责人或另一位招聘责任人独立完成；不能由模型建议替代。复核仍无法确认时保留待验证状态，用结构化面试核实，不强行给出淘汰结论。
