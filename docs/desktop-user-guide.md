# 简历工作台 0.2.0 预览版

## 打开使用

Windows：解压完整压缩包，双击 ResumeDesk/ResumeDesk.exe。不能只复制 exe；同目录 _internal 包含运行时与 OCR 模型。无需安装 Python、uv 或填写 API。

Mac：使用对应芯片构建的 ResumeDesk.app，复制到个人 Applications 后打开。当前 Windows 构建不包含 Mac 成品；Mac 构建与签名验证完成前，不宣称已支持用户设备。

预览包未作发布签名。不要通过关闭系统防护来安装；受管理设备可交由 IT 审核，正式分发需完成签名。

## 无 API 使用

在“材料与审阅”选择岗位并导入文件。混合岗位可按文件名前缀分流；未知岗位请手工选择固定岗位。解析完成后选中材料，查看提取文本或打开原文件副本。

右侧选择检查项，填写判断与原文证据/备注。输入停止约 0.7 秒后，当前材料会自动保存为本机草稿；切换材料或退出时也会先保存草稿。填写审阅者和整体意见，点击保存后才形成正式修订记录；未审阅项会保留待审阅状态，信息不足可以保存。导出生成新的目录，含 CSV、提取文本和人工意见，无 AI 分数。

目录监听默认只接收有明确岗位前缀的文件，经过两个稳定周期再处理；点击“停止接收”后等待当前任务完成。重新导入可重试解析失败材料。数据目录 last-import-report.txt 记录最近一轮跳过与失败信息。

## 可选模型

在设置页选择 openai-compatible 或 minimax，填写 HTTPS Base URL（通常以 /v1 结尾）、模型名和自己的 Key。密钥存入 Windows 凭据库或 macOS Keychain，普通配置文件不保存密钥。

测试只发合成短文本，可能产生少量费用。选中一份或多份已整理材料，点击 AI 分析并确认后发出请求。批量分析会逐份显示进度；若当前端点和模型已有历史运行，会在确认框中提示可能重复计费。认证失败或限流会暂停当前批次，不会继续向后发送请求；修正配置或等待服务恢复后需显式重新发起。每次分析独立保存，连接测试不证明模型判断质量。没有配置、额度不足或网络失败时仍可使用人工模式。

在 AI 历史打开每次运行目录。成功结果在 outputs/<材料ID> 下；queue.sqlite3 保存状态。格式错误、超时或远端是否完成不明时不自动重试。需自行核查供应商请求记录后再决定是否重新分析。

## 飞书可选集成

需由使用者/管理员安装并登录 lark-cli。选择 CLI 路径、简历目录，填写自己的 Base、表、视图、文件夹标识和字段名称，先预检查，再点击执行本轮同步。默认只同步文档，未接入桌面 AI 回写或人工意见回写。

当前无登录向导和自动安装器；预检查检测访问权限及表结构。授权或字段错误时查看数据目录 feishu 中的 report.json。未配置飞书不影响本地功能。

## 本机数据与升级

Windows：%LOCALAPPDATA%/ResumeDesk。Mac：~/Library/Application Support/ResumeDesk。

材料原始副本、脱敏文本、人工修订和 AI 运行保存在此目录。不要将整个目录公开分享。诊断按钮只导出版本、平台、数量及资源状态。

设置页可将材料、人工修订、自动保存草稿、AI 运行和最近一次导入报告打包为带版本清单的 ZIP。备份不包含 API Key、模型配置或飞书凭据。恢复前会自动生成当前数据的回滚备份；替换中途失败会自动恢复原数据，若回滚本身也遇到文件系统错误，会提示使用回滚备份。恢复完成后当前模型配置和凭据仍保留，需要时可用回滚 ZIP 撤销恢复。新包继续使用同一目录，不会自动导入开发项目的 .env、数据库或简历。便携版移除只需删除解压的程序目录；不会删除数据或系统凭据。如需清除凭据，通过系统凭据管理器删除 ResumeDesk 项。

## 开发与各平台构建

使用原生架构 Python 3.12，确保 Tk 可导入：

    uv sync --locked --group build
    uv run --locked python -m resume_screening.desktop
    uv run --locked --group build python scripts/build_desktop.py

Mac 构建机的 Python 必须带 Tcl/Tk；GUI smoke test 需要可用图形会话。构建阶段可联网准备 OCR 模型，发行包运行时不下载模型。

Windows 与 M 系列使用 ONNX Runtime 1.29.0；Intel Mac 使用 1.23.2，因为 1.29.0 不再提供对应轮子。当前锁定依赖组合在两类 Mac 上均以 macOS 14 为下限：M 系列受 ONNX Runtime 约束，Intel 受 OpenCV 5.0.0.93 的轮子约束。已检查两平台的锁定原生依赖可解析，但这不替代实际启动及 OCR 验证。无法升级到 macOS 14 的旧 Air 不在当前预览版目标内。来源：[ONNX Runtime 发行说明](https://github.com/microsoft/onnxruntime/releases)、[Intel ONNX 发行文件](https://pypi.org/project/onnxruntime/1.23.2/)、[OpenCV 发行文件](https://pypi.org/project/opencv-python/5.0.0.93/)。

PyInstaller 必须分别在目标系统构建。正式 Mac 发布还需 Developer ID 签名、公证及真实设备验收。来源：[PyInstaller 构建说明](https://pyinstaller.org/en/stable/usage.html)。
