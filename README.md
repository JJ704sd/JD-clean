# 简历清洗与证据化初筛

这是一个以 Python 为主的可恢复简历初筛工具。每份简历先在本地转换为可追溯 Markdown，再将脱敏后的单份简历连同对应岗位 skill 一次性发送给 `MiniMax-M3`。模型只负责逐项取证，分数和 A–E 档由 Python 按固定规则计算。

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

密钥不得提交到仓库或写入简历、日志和命令参数。未设置 `MINIMAX_API_KEY` 时，worker 会在领取任务前退出，不增加模型调用次数。

默认调用国内开放平台端点 `https://api.minimaxi.com/v1/text/chatcompletion_v2`。国际开放平台部署可显式覆盖：

```powershell
$env:MINIMAX_API_ENDPOINT = "https://api.minimax.io/v1/text/chatcompletion_v2"
```

## 使用

登记一个目录中的简历，必须显式指定岗位：

```powershell
uv run python -m resume_screening enqueue C:\Users\Administrator\Downloads `
  --role senior-fullstack-engineer
```

固定岗位批次仍会检查文件名中的明确岗位前缀；若文件名明确标注为另一受支持岗位，任务会被拒绝而不是按 `--role` 强制混入。无岗位前缀的单份简历可以由招聘责任人显式指定岗位。

下载目录混有多个岗位或其他 PDF 时，使用文件名自动分流。只有明确包含受支持岗位前缀的文件会入队，发票、规范等未知文件会跳过：

```powershell
uv run --locked python -m resume_screening `
  --database "var\screening-today.sqlite3" `
  enqueue "C:\Users\Administrator\Downloads" --auto-route --today
```

`--today` 按本机时区筛选“最后修改日期为今天”的文件，避免把下载目录中的历史文件全部登记。建议每个批次始终显式指定数据库；不加 `--database` 会写入默认的 `var\screening.sqlite3`。

Boss格式文件名中的候选人姓名或昵称只写入本地审核记录和导出文件；发送给模型前仍会脱敏。无法从可信文件名格式识别姓名时保持为空，不从简历邮箱等信息猜测。

单份登记时可以指定稳定 ID 和仅用于本地展示的姓名：

```powershell
uv run python -m resume_screening enqueue C:\Users\Administrator\Downloads\candidate.pdf `
  --role senior-fullstack-engineer `
  --candidate-id candidate-001 `
  --candidate-name 张三
```

处理当前队列后退出：

```powershell
uv run python -m resume_screening worker --once
```

首次接入新提示词或新模型时，可先限制为一份任务做真实小样验证：

```powershell
uv run python -m resume_screening worker --once --max-tasks 1
```

长期监听：

```powershell
uv run python -m resume_screening worker --watch --poll-seconds 5
```

如需自动发现下载目录中的新 PDF，可为监听任务绑定一个固定岗位：

```powershell
uv run python -m resume_screening worker --watch `
  --input C:\Users\Administrator\Downloads `
  --role senior-fullstack-engineer
```

查看状态、显式重置未获得模型完成响应的任务、导出批次结果与人工复核队列：

```powershell
uv run python -m resume_screening status
uv run python -m resume_screening retry-failed
uv run python -m resume_screening export --directory exports
```

默认数据库为 `var/screening.sqlite3`，默认输出目录为 `outputs`。可在子命令前使用 `--database` 和 `--output` 修改：

```powershell
uv run python -m resume_screening --database D:\screening\state.sqlite3 `
  --output D:\screening\outputs worker --once
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

当前 prompt v4 中，AI 产品经理继续返回逐项证据、决策相关疑点和面试追问；资深全栈改为返回逐项事实清单，不再自报证据等级。Python 负责规范证据等级、建议、摘要、人工审核状态、确定性分数和档位。旧版已完成结果保持只读；尚未处理的旧解析/提示/评分任务进入 `STALE_CONTRACT_VERSION`，需从原始文件重新登记为新版本任务。

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
- Markdown 保留页码、源文件 SHA-256、解析器版本和 OCR 标记。
- 发给模型的内容会移除姓名、电话、邮箱、身份证号和明确标注的详细地址；原始文件不复制进数据库。
- SQLite 不保存简历正文或无效模型原文，只保存任务状态、调用元数据和通过校验的结构化结果。

## 评分

证据强度固定换算为 `E0=0%`、`E1=40%`、`E2=75%`、`E3=100%`。资深全栈 v8 中，模型只返回事实清单，Python 生成证据等级；E3 必须同时具备项目背景、个人动作、方法或取舍、结果口径和可核验影响，缺一项最多 E2。

- A：证据匹配分 85–100。
- B：证据匹配分 70–84。
- C：证据匹配分 55–69。
- D：证据匹配分 40–54。
- E：证据匹配分 0–39。

证据档位不覆盖岗位门禁：没有至少 `E2` 的 Go 项目级个人交付证据时，即使相邻能力分较高，资深全栈建议仍为暂不推进。Node.js 仅作补充能力，不能替代 Go；物流/供应链维度权重为 15 分。旧版 v2–v6 记录继续只读兼容，新任务使用 v7 重筛并保留版本信息。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s skills/screen-ai-product-manager-resumes/scripts -p "test_*.py" -v
uv run python -m resume_screening --help
```

Windows 长期运行可让任务计划程序在登录或开机时启动 `uv run python -m resume_screening worker --watch`，并将“起始于”设置为本项目目录。SQLite 会恢复明确尚未发送的队列任务；请求期间中断的任务会进入人工处理，避免重复调用。同一数据库只建议运行一个 worker。
