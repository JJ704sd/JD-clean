# 桌面构建执行记录

2026-09-07：按 one-click-app-spec.md 与 no-api-fallback-spec.md 构建。

## 当前计划

1. 已完成：现有 148 项 unittest 基线通过；采用 Tk + 后台工作线程 + 独立 SQLite 审阅库。
2. 已完成：本地整理、人工审阅、模型适配和桌面界面；桌面人工库独立且保留修订。
3. 已完成：Windows 免安装 ZIP、合成中英文材料断网验收、系统凭据测试、Mac 原生构建脚本和使用说明。
4. 待外部条件：Mac 两类真机原生构建及验收、发布签名、真实模型与飞书集成验证。当前交付为预览版，完整规格未全部验收，详见 desktop-validation.md。

## 兼容边界

- 原 CLI、AI 数据库及现有合同不迁移。桌面使用独立版本库和每次 AI 运行的独立队列，旧输出不覆盖。
- UI 不参与业务计算；后台服务复用 cleaning、pipeline 和 TaskStore。Tk 操作仅在主线程执行。
- 新增 keyring 25.6.0 用于系统凭据存储；新增构建组 PyInstaller 6.22.0。Windows/Mac arm64 保持 ONNX Runtime 1.29.0；Intel Mac 条件锁定 1.23.2。其他已有直接依赖不升级，新增 Intel 分支的传递依赖由 uv 解析。
- 替代方案为本地 Web 服务，但增加端口、浏览器生命周期和请求授权边界；当前三页本地工具优先 Tk。
- 回退只需使用原 CLI；不会修改开发目录 .env、现有数据库和私人材料。
- 官方打包依据：https://pyinstaller.org/en/stable/usage.html 。每个平台分别打包，不从 Windows 宣称产出已验证的 Mac 应用。

## 发布限制

本机 Windows 可运行源码和打包测试；无 Mac 主机、发布签名身份及授权的真实 API/飞书测试环境。对应项必须标记未验证。

Mac 的完整锁定原生依赖以 macOS 14 为下限，两架构均通过跨平台解析检查。13.0 的尝试发现 arm64 ONNX 与 Intel OpenCV 轮子要求更高系统，因此最终目标统一为 14；没有为了声称兼容而强行忽略依赖要求。
