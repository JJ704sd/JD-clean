# 来源与设计范围

## 材料与岗位依据

2026-10-08 只读读取用户指定的全栈开发实习简历视图。NDJSON 查询返回 5 条记录、5 份 PDF 附件，has_more=false；三份 1 页、两份 2 页，共 7 页，全部完成本地文本层提取与项目段落检查。

用于方法提炼的观察：材料同时存在后端与前端为主的经历，技术列表与个人实现需要分开；团队成果、性能指标、AI 功能和上线可靠性主张需要明确归属和验证范围。该小样本只用于设计表达规则，不用于统计招聘通过率、评估个人真实性或建立候选人排名。未读取面试评价，不将招聘状态用作能力标签，未对候选人源码或生产指标做独立验证。

公开文件不保存真实姓名、联系方式、学校/公司履历、简历原文、附件 token、记录 ID 或内部飞书链接。所有改写示例为重新设计的虚构场景。

岗位选择同时参考本仓库实习岗位画像 `fullstack-intern-2026-08-14-v1` 及 Rubric `fullstack-intern-2026-08-24-v4`。本 Skill 是求职材料写作工具；不调用筛选队列，不修改岗位合同和评分逻辑。新投递版本以用户当前 JD 为准。

## ASu-skills 方法参考

读取版本：`cf9eacba193ea08de169e30dcedb698202ad6f6b`，查询日期 2026-10-08。

- [great-resume](https://github.com/Hisn00w/ASu-skills/blob/cf9eacba193ea08de169e30dcedb698202ad6f6b/skills/great-resume/SKILL.md)：真实经历重组、个人边界与可追问表达。
- [claim-evidence-ledger](https://github.com/Hisn00w/ASu-skills/blob/cf9eacba193ea08de169e30dcedb698202ad6f6b/skills/great-resume/references/claim-evidence-ledger.md)：原始事实与候选措辞分开，跨版本保留确认状态。
- [make-resume](https://github.com/Hisn00w/ASu-skills/blob/cf9eacba193ea08de169e30dcedb698202ad6f6b/skills/make-resume/SKILL.md)：内容写作与文件制作按请求区分，复制模板生成用户副本。
- [page-balance-qa](https://github.com/Hisn00w/ASu-skills/blob/cf9eacba193ea08de169e30dcedb698202ad6f6b/skills/make-resume/references/page-balance-qa.md)：可读字号、合理分页与打印检查。

本 Skill 面向全栈实习场景重新编写指南、案例和模板，不提供 ASu 的完整模板库、照片、Logo、插件入口或导出脚本，不要求安装 ASu。方法参考的上游项目采用 MIT；版权与许可原文保留于 [asu-license.txt](asu-license.txt)，适用上游材料，不替代 JD-clean 的授权约定。

## 验证边界

Skill 结构校验只能确认 frontmatter、命名和脚手架完整性；链接检查只确认本地引用存在。HTML 模板测试使用虚构内容，不能证明任意长文本都在一页内，也不能证明真实简历的事实。每次正式生成仍需检查实际内容与打印结果。

## 2026-10-08 本次检查记录

- skill-creator quick_validate：通过；全部本地 Markdown 引用与 UI 元数据检查通过。
- 使用本机 Chrome 与虚构内容检查 HTML：正文编辑、下载保存、重载保存内容通过；无 JavaScript 错误或外部网络请求，长链接未横向溢出。
- 导出并逐页查看单页与多页测试 PDF：分别 1 页和 2 页，A4、文本可提取、打印工具栏隐藏，未见裁切或空白页。
- 按仓库 README 执行现有检查：主测试集 246 项、AI 产品经理 Skill 校验器测试 12 项均通过；CLI help 正常。

以上是模板和既有仓库的检查，不是对未来真实简历的事实核验或自动投递验收。
