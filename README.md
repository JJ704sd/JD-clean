# 简历清洗与证据化初筛

这是一个以 Python 为主的可恢复简历初筛工具。每份简历先在本地转换为可追溯 Markdown，再将脱敏后的单份简历连同对应岗位 skill 一次性发送给 `MiniMax-M3`。模型只负责逐项取证，分数和 A–E 档由 Python 按固定规则计算；watch 模式负责下载目录分流、稳定性保护、人工复核队列和后台健康状态。

支持的岗位：

- AI 产品经理：`ai-product-manager`
- 资深全栈工程师：`senior-fullstack-engineer`
- 全栈开发实习生：`fullstack-development-intern`

## 适用边界与当前校准状态

本项目输出的是“针对指定岗位的简历证据匹配结果”，不是候选人的通用能力分，也不是对人工招聘状态的预测。使用前必须先确认候选人的实际投递岗位；AI 产品经理、资深全栈和全栈实习生不得放入同一岗位批次后横向比较。人工“未通过”如果发生在沟通、薪资、地点、到岗时间或求职意愿确认之后，也不能直接作为简历评分的反例。

当前版本仍处于人工校准阶段，不能用于自动筛除、强制排名或直接写入 ATS 最终状态：

- `E0`–`E3` 和加权总分只表示简历中可提取证据的覆盖与强度；`not_evidenced` 不等于候选人不具备该能力。
- A–E 档只表示证据匹配分区间；人工复核状态单独保存在 `review_status` 和 `model_recommendation` 中。进入二审不再覆盖证据档位。
- 原文件无法可靠解析时应先执行 OCR/来源核验并记录 `U01_PARSE_QUALITY`，不得把空白文本或重复平台令牌解释为真实的 0 分能力记录。
- 模型输出无效、解析失败和岗位错配属于流程质量问题，应与候选人的能力判断分开统计。
- 决策相关疑点只有在澄清后可能改变建议时才进入二审；不会改变当前方向的缺失项会由 Python 过滤。

资深全栈 v8 已用同岗位 87 份简历及现有人工原因做定向校准：Go 项目证据仍是绝对硬门槛，Node.js 不能替代；岗位同时接受完整全栈和 Go 后端偏重画像。独立前端不再单项阻断，直接推进改为要求应用研发方向、Go、架构、数据和可定位个人责任均达到项目级证据。薪资、地点、到岗、沟通取消和岗位意愿与能力评分分离；只有终态而没有具体原因的记录不用于调权。

## 安装

项目固定使用 Python 3.12 和 `uv`：

```powershell
uv python install 3.12
uv sync
$env:MINIMAX_API_KEY = "你的 Token Plan Key 或 API Key"
```

密钥不得提交到仓库或写入简历、日志和命令参数。未设置 `MINIMAX_API_KEY` 时，worker 会在领取任务前退出，不领取任务、不增加模型尝试次数；`health` 会明确显示 `NOT_CONFIGURED`。

默认调用国内开放平台端点 `https://api.minimaxi.com/v1/text/chatcompletion_v2`。国际开放平台部署可显式覆盖：

```powershell
$env:MINIMAX_API_ENDPOINT = "https://api.minimax.io/v1/text/chatcompletion_v2"
```

## 使用

登记一个目录中的简历，必须显式选择固定岗位或自动分流：

```powershell
uv run --locked python -m resume_screening enqueue C:\Users\Administrator\Downloads `
  --role senior-fullstack-engineer
```

固定岗位的批量 `enqueue` 会检查文件名中的明确岗位前缀；若文件名明确标注为另一受支持岗位，任务会被拒绝而不是按 `--role` 强制混入。无岗位前缀的单份简历可以由招聘责任人显式指定岗位。

下载目录混有多个岗位或其他 PDF 时，使用文件名自动分流。只有明确包含受支持岗位前缀的文件会入队，发票、规范等未知文件会跳过：

```powershell
uv run --locked python -m resume_screening `
  --database "var\screening-today.sqlite3" `
  enqueue "C:\Users\Administrator\Downloads" --auto-route --today
```

`--today` 按本机时区筛选“最后修改日期为今天”的文件，避免把下载目录中的历史文件全部登记。建议每个批次始终显式指定数据库；不加 `--database` 会写入新的默认库 `var\screening-v8.sqlite3`，不会触碰旧的 `var\screening.sqlite3`。

Boss格式文件名中的候选人姓名或昵称只写入本地审核记录和导出文件；发送给模型前仍会脱敏。无法从可信文件名格式识别姓名时保持为空，不从简历邮箱等信息猜测。

单份登记时可以指定稳定 ID 和仅用于本地展示的姓名：

```powershell
uv run --locked python -m resume_screening enqueue C:\Users\Administrator\Downloads\candidate.pdf `
  --role senior-fullstack-engineer `
  --candidate-id candidate-001 `
  --candidate-name 张三
```

处理当前队列后退出：

```powershell
uv run --locked python -m resume_screening worker --once
```

首次接入新提示词或新模型时，可先限制为一份任务做真实小样验证：

```powershell
uv run --locked python -m resume_screening worker --once --max-tasks 1
```

长期监听（无目录输入时只消费当前数据库队列）：

```powershell
uv run --locked python -m resume_screening worker --watch --poll-seconds 5
```

下载目录混有三个支持岗位时，使用明确文件名前缀自动分流。支持 `.pdf`、`.docx`、`.txt`、`.md`；隐藏文件、`.crdownload`、`.part` 和其他临时文件会忽略。文件大小和最后修改时间连续两个轮询周期不变后才入队，同一文件内容和合同只入队一次：

```powershell
uv run --locked python -m resume_screening worker --watch `
  --input C:\Users\Administrator\Downloads `
  --auto-route `
  --poll-seconds 5
```

自动分流识别 `AI 产品经理`、`全栈开发实习生`、`资深全栈/全栈工程师` 等文件名前缀。未知文件计入 `UNKNOWN_FILE_SKIPPED`；固定岗位错配计入 `ROLE_MISMATCH_SKIPPED`，都不会终止监听。

如需只监听一个岗位：

```powershell
uv run --locked python -m resume_screening worker --watch `
  --input C:\Users\Administrator\Downloads `
  --role senior-fullstack-engineer
```

固定岗位 watch 默认跳过没有岗位前缀的文件；只有明确确认目录内未标注文件也属于该岗位时才加：

```powershell
uv run --locked python -m resume_screening worker --watch `
  --input C:\Users\Administrator\Downloads `
  --role senior-fullstack-engineer `
  --accept-unlabeled
```

查看状态、后台健康、显式重置未获得模型完成响应的任务、导出批次结果与人工复核队列：

```powershell
uv run --locked python -m resume_screening status
uv run --locked python -m resume_screening health
uv run --locked python -m resume_screening retry-failed
uv run --locked python -m resume_screening export --directory exports
```

`health` 输出 worker 心跳是否仍持有、最后心跳/成功时间、当前 parser/prompt/scoring/JD/rubric、五类队列计数、24 小时成功/错误数、错误码分布、watch 跳过计数和超阈值 processing 任务。worker 使用 SQLite 租约限制同一数据库只能有一个活跃消费者；进程异常退出后租约过期，health 会报告 `stale`。

默认数据库为 `var/screening-v8.sqlite3`，默认输出目录为 `outputs`。可在子命令前使用 `--database` 和 `--output` 修改：

```powershell
uv run --locked python -m resume_screening --database D:\screening\state.sqlite3 `
  --output D:\screening\outputs worker --once
```

## 飞书简历闭环监控

`scripts/feishu_resume_monitor.py` 将下载目录中的 `.pdf` 简历做成可恢复的闭环：每轮先读取 Base 表结构、视图和全量记录，默认只接收当前岗位前缀 `【全栈工程师_深圳 15-25K】` 并按固定 `PDF_KEY_RULE` 做唯一匹配，再执行本地提取/OCR、脱敏和结构化 Markdown；同一飞书文件夹使用独立的跨进程周期锁串行化状态核验、导入决策和实际导入，异步任务会续查，文档 URL 只取 CLI 实际返回值，随后回读并检查正文。监控器本身不调用模型；加上 `--screening` 后，仅在文档回读成功后把任务交给现有 SQLite 筛选队列。

首次接入或表结构发生变化时，必须先用 dry-run 为当前字段结构建立本地门禁：

```powershell
$env:FEISHU_BASE_TOKEN = "通过受控环境注入，不要写入脚本或日志"
$env:FEISHU_PDF_DIR = "$env:USERPROFILE\Downloads"
$env:DRY_RUN = "true"
uv run --locked python scripts\feishu_resume_monitor.py --once `
  --seed-report "D:\JD clean\outputs\feishu-batch-2026-09-02\batch-report.json"
```

这里的 `--seed-report` 用于吸收本次已成功导入的 9 份文档；以后新增监控轮次不再重复创建它们。若换了批次或环境，应明确替换为对应的成功批次报告。

接入现有岗位匹配、证据提取和确定性评分脚手架时，先运行一次 dry-run（此时只建立门禁和本地状态，不入 AI 队列）：

```powershell
uv run --locked python scripts\feishu_resume_monitor.py --once --dry-run --screening `
  --seed-report "D:\JD clean\outputs\feishu-batch-2026-09-02\batch-report.json"
```

确认报告后，用 apply 将“文档已成功回读”的简历交给现有筛选队列：

```powershell
uv run --locked python scripts\feishu_resume_monitor.py --once --apply --screening
```

再启动已有的模型 worker 消费队列；模型密钥只由 worker 从 `MINIMAX_API_KEY` 读取，监控器不接触该密钥：

```powershell
uv run --locked python -m resume_screening `
  --database "var\screening-v8.sqlite3" `
  --output "outputs" worker --watch --poll-seconds 5
```

持续运行时，将第二条命令改为 `--watch --apply --screening --interval-seconds 300`，并保持上述 worker 单独运行。当前岗位前缀默认映射到 `senior-fullstack-engineer`；更换岗位时必须显式指定 `--screening-role` 并重新 dry-run。worker 会复用现有岗位 Skill/Rubric、模型证据提取、Python 确定性评分、`screening.json`、`conclusion.md` 和人工复核队列；模型结果仍是 `non_final`，不会自动淘汰或写入最终招聘状态。

确认报告后再启动实际监控：

```powershell
$env:DRY_RUN = "false"
uv run --locked python scripts\feishu_resume_monitor.py --watch --apply --screening --interval-seconds 300
```

本次已完成的批次可以用 `--seed-report` 写入本地幂等状态，避免当前 Base 尚未补字段时重复导入已有文档。状态保存在 `var/feishu-resume-monitor/state.json`，当前轮报告为 `outputs/feishu-resume-monitor/batch-report.json`，历史摘要为 `batch-history.ndjson`。已有文档链接或相同源哈希默认跳过；文档回读和 Base 操作的临时网络错误/限流单次最多重试 3 次，当前 `drive +import` 没有 CLI 幂等键，临时错误且没有 URL/ticket 时会保留为 `import_pending`，不会在下一轮或 `--retry-failed` 中盲目重放，权限、认证和格式错误不自动重试。

只有配置的“简历文档链接、处理状态、错误信息、处理时间、源 PDF 哈希”字段全部存在且类型可写时，成功回读的文档才会触发 `base +record-batch-update`，随后使用 `base +record-get` 核验。字段缺失时仍可完成 Markdown、文档导入和本地 AI 筛选队列交接，但 Base 写回与 NEXT_ACTION 保持关闭；新增字段后需重新运行一次 dry-run。

## 在线简历发布器与 MiniMax-M3 筛选

如果不需要读取或写回 Base，只需把本地 PDF 发布为飞书文档并生成本地链接列表，可使用 `scripts/feishu_online_resume_publisher.py`。默认只做本地提取/OCR、清洗脱敏和 dry-run；加 `--screening` 后，`--apply` 只有在飞书文档回读非空时才会把任务交给筛选队列，发布器自身不会调用模型或发送群消息：

```powershell
uv run --locked python scripts\feishu_online_resume_publisher.py --once --dry-run --screening
uv run --locked python scripts\feishu_online_resume_publisher.py --once --apply --screening
```

如需处理历史下载文件，两个命令都加上 `--all-dates`；岗位前缀变化时，两个命令都必须使用相同的 `--job-prefix`，并重新执行 dry-run。链接列表写入 `resume-index.md`，格式为纯文本：

```text
候选人文档 · 在线简历

• 张三  简历：https://example.feishu.cn/docx/abc
```

然后启动现有 worker 消费队列。默认模型为 `MiniMax-M3`，默认端点为中国服务商 MiniMax 开放平台 `https://api.minimaxi.com/v1/text/chatcompletion_v2`；模型只负责提取岗位证据，Python 继续按固定 rubric 确定性评分并生成 `screening.json`、`conclusion.md` 和人工复核状态：

```powershell
uv run --locked python -m resume_screening `
  --database "var\screening-v8.sqlite3" `
  --output "outputs" worker --watch --poll-seconds 5
```

## 输出

每位候选人的当前结果位于：

```text
outputs/<candidate_id>/
├── resume.cleaned.md
├── screening.json
└── conclusion.md
```

`screening.json` 使用双层结构：`screening_record` 是已通过岗位 validator 的证据与流程记录，`scorecard` 是应用层生成的确定性分数、证据档位、独立 `review_status`、各维度得分和评分版本。

当前 prompt v4 中，AI 产品经理继续返回逐项证据、决策相关疑点和面试追问；资深全栈改为返回逐项事实清单，不再自报证据等级。Python 负责规范证据等级、建议、摘要、人工审核状态、确定性分数和档位。旧版已完成结果保持只读；尚未处理的旧解析/提示/评分/rubric 任务不会调用模型，并汇总为 `STALE_CONTRACT_VERSION`，需从原始文件重新登记为新版本任务。重筛始终创建新的版本化任务键。

## 调用与失败语义

- 相同“文件哈希 + 岗位 + JD + rubric + 模型 + 解析/提示/评分版本”只登记一次。
- 解析或 OCR 质量不合格时记录 `U01_PARSE_QUALITY`，不会调用模型。
- 每份简历只接受一个完成的模型响应。完整响应不是合法 JSON 或不符合岗位契约时进入人工处理，不再次请求模型。
- 仅供应商明确拒绝生成、连接失败或限流等未完成请求可重试，自动上限为 3 次。
- 请求超时或 worker 在请求期间中断属于远端状态不确定，为避免重复分析直接进入人工处理，不自动重试。
- 所有模型结果均为 `non_final`，必须人工一审；二审触发条件由各岗位 skill 决定。

## 清洗与隐私

- 支持 `.pdf`、`.docx`、`.txt`、`.md`。
- PDF 优先读取文本层；页面缺少有效文字时才使用 RapidOCR。
- `resume.cleaned.md` 保留页码、源文件 SHA-256、解析器版本和 OCR 标记，但正文保存为脱敏版本；原始文件不复制进数据库，也不会被改写。
- 发给模型的内容会移除姓名、电话、邮箱、身份证号和明确标注的详细地址；常见的空格/连字符联系方式也会拦截。
- SQLite 不保存简历正文或无效模型原文，只保存任务状态、调用元数据和通过校验的结构化结果；诊断信息会脱敏，不在日志中打印简历正文、电话、邮箱或 API Key。

## 评分

证据强度固定换算为 `E0=0%`、`E1=40%`、`E2=75%`、`E3=100%`。资深全栈 v8 中，模型只返回事实清单，Python 生成证据等级；E3 必须同时具备项目背景、个人动作、方法或取舍、结果口径和可核验影响，缺一项最多 E2。

- A：证据匹配分 85–100。
- B：证据匹配分 70–84。
- C：证据匹配分 55–69。
- D：证据匹配分 40–54。
- E：证据匹配分 0–39。

证据档位不覆盖岗位门禁：没有至少 `E2` 的 Go 项目级个人交付证据时，即使相邻能力分较高，资深全栈建议仍为暂不推进。Node.js 仅作补充能力，不能替代 Go；物流/供应链维度权重为 15 分。旧版 v2–v7 记录继续只读兼容，新任务使用 v8 重筛并保留版本信息。

## 人工原因与校准

导出的 `review_queue.csv` 可由人工补充后导入。CSV 至少包含 `task_id` 或 `candidate_id`、`human_conclusion` 和 `reason_category`；`reason_category` 只能是 `capability`、`hard_eligibility`、`process_or_commercial`、`intent`、`unknown`，可选 `criterion_id` 和 `model_recommendation`：

```text
task_id,human_conclusion,reason_category,criterion_id,model_recommendation
123,do_not_advance,capability,SEN-BE-01,advance
124,do_not_advance,process_or_commercial,,advance
```

```powershell
uv run --locked python -m resume_screening calibrate import .\human-results.csv
uv run --locked python -m resume_screening calibrate report --output .\exports\calibration-report.json
```

只有 `capability` 和 `hard_eligibility` 进入能力校准统计；薪资、地点、到岗、岗位意向和未知原因只计入非能力原因。报告包含分状态分数分布、Go 门槛命中率、建议与人工能力结论混淆矩阵和主要分歧 criterion。样本不足时会明确警告，不产生伪精确调权建议，也不会自动修改权重。

## 验证

```powershell
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m unittest discover -s skills/screen-ai-product-manager-resumes/scripts -p "test_*.py" -v
uv run --locked python -m resume_screening --help
```

Windows 后台运行可以参考 [计划任务命令模板](docs/windows-task-template.ps1)。模板只打印待确认的参数和命令，不会注册计划任务、不设置系统级凭据，也不会在本次变更中启动长期进程。实际配置时将“起始于”设置为项目目录，并使用独立的 `var\screening-v8.sqlite3`；同一数据库只运行一个 worker。SQLite 会保留已发现任务；请求期间中断的任务进入人工处理，避免重复调用。
