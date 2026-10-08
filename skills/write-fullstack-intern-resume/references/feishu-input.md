# 从飞书简历表读取材料

仅在用户提供并要求读取飞书材料时使用。默认只读，不修改招聘评价、面试/录用状态，不创建候选记录或在线简历。现有登录可用时优先 lark-cli；身份或权限不足时保留已完成的本地工作并说明实际缺口，不索取聊天中的密钥。

## 按用户 URL 定位

从本次 URL 读取 base token、table ID 和 view ID，不沿用历史候选表。先用 `base +field-list --help` 和 `base +record-list --help` 核对本机版本参数。

```powershell
lark-cli base +field-list --base-token <BASE_TOKEN> --table-id <TABLE_ID> --as user --format json
lark-cli base +record-list --base-token <BASE_TOKEN> --table-id <TABLE_ID> --view-id <VIEW_ID> --field-id <ATTACHMENT_FIELD_ID> --as user --format ndjson --output tmp/resume-source/records.ndjson --limit 2000
```

这些参数是示例占位，执行时替换为当前 URL 和实际返回字段。只投影当前任务需要的字段；制作方法不需要 HR 评价或最终状态。NDJSON manifest 可能含真实示例值，保存到私有本地目录，不直接贴入报告或公开仓库。检查 records_count、has_more 和视图范围；有分页就补齐，不把首批当作全量。

## 附件与正文

从实际记录返回的 record_id 和附件 file_token 下载，不猜地址：

```powershell
lark-cli base +record-download-attachment --base-token <BASE_TOKEN> --table-id <TABLE_ID> --record-id <RECORD_ID> --file-token <FILE_TOKEN> --output tmp/resume-source/sample-01.pdf --as user --format json
```

单 file token 可用明确文件路径，避免真实姓名进入临时文件名。附件不是云文档 URL；在线文档只使用实际返回或用户给定的 URL，并用 `docs +fetch` 读取。

PDF 先在本地提取文本，核对页数、顺序、关键项目和重复平台字符；扫描页才采用已具备的本地 OCR。没有可靠文本时记录缺页或解析问题，不把解析缺失解释为经历缺失。发送到外部 OCR/模型需要本次材料对应的明确授权；安装或运行本 Skill 不提供这种授权。

## 私有材料与公开方法

单人制作时只使用该人的事实，其他简历用于观察结构与表达，不能转移经历。多人材料独立建事实基线，不混合姓名、学校、项目或数字。

原始附件、候选人文本、文件 token、记录 ID、招聘评价及私有链接保存在任务私有目录；公开 Skill 只保留抽象规则和虚构案例。在 JD-clean 中 `tmp/`、`outputs/`、`exports/` 已忽略；其他项目先核对真实忽略规则。

完成提炼后删除本次不再需要的临时下载与索引；保留用户明确要求的交付物和必要私有审计证据。不能因“同步 Skill 到仓库”而把源简历公开。
