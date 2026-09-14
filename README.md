# AI Bridge 3.0

在 Windows + WSL2 环境中，将 Telegram、飞书与 Claude Code / Codex CLI 连接起来的本地桥接系统。它支持多 Bot 隔离、模型切换、文件与图片处理、语音转写，以及浏览器控制面板。

> 本项目允许聊天消息触发本机 AI CLI 执行命令。请仅绑定可信账号，不要把管理入口或 Bot Token 分享给他人。

## 主要功能

- Telegram 与飞书双向消息转发
- Claude Code 常驻 tmux 会话
- Codex CLI 登录态调用与会话续接
- Claude、Codex、DeepSeek、智谱和百炼模型切换
- 文字、图片、文件及 Telegram 语音消息处理
- 多 Bot 独立会话、模型、思考档位和回复状态
- Dashboard 服务管理、实时日志、Token 统计和模型配置
- Webhook Secret 校验、回复去重、失败补发及超时保护
- 可选 Windows 桌面端与 Android WebView 客户端

## 架构

```text
Telegram ── HTTPS Webhook ─┐
                           ├─> bridge.py ─> Claude Code / Codex CLI
飞书 ── WebSocket ─────────┘        │
                                    ├─> hooks/send-to-telegram.py
浏览器 / Android ─> dashboard.py ──┘
```

核心桥接逻辑使用 Python 标准库。飞书、LiteLLM、语音转写及桌面/Android 客户端按需安装依赖。

## 环境要求

- Windows 10/11 与 WSL2 Ubuntu
- Python 3.10+
- tmux、curl、jq、ffmpeg
- Node.js 22 与 Claude Code
- Codex CLI（使用 Codex 模型时）
- ngrok（Telegram Webhook）
- 可选：LiteLLM、`lark-oapi`、Whisper、PyQt6、Android SDK

## 安装

项目启动脚本默认路径为：

```text
D:\AI\claudecode-telegram-main
/mnt/d/AI/claudecode-telegram-main
```

克隆到其他目录时，请同步修改启动脚本中的 `PROJECT` 路径。

在 WSL 中安装基础依赖：

```bash
cd /mnt/d/AI/claudecode-telegram-main
bash windows/setup_deps.sh
```

根据需要安装可选组件：

```bash
python3 -m pip install --user 'litellm[proxy]' lark-oapi
```

首次使用 Claude Code 或 Codex：

```bash
claude  # 首次运行时按提示完成登录
codex login
```

## 配置

凭据只能保存在环境变量或已被 `.gitignore` 排除的本地配置中，不要写进源码。

| 变量 | 用途 | 默认值 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram 主 Bot Token | 必填 |
| `STOCK_BOT_TOKEN` | 第二个 Telegram Bot | 可选 |
| `FEISHU_APP_ID` | 飞书应用 ID | 可选 |
| `FEISHU_APP_SECRET` | 飞书应用密钥 | 可选 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 可选 |
| `ZHIPU_API_KEY` | 智谱 API Key | 可选 |
| `BAILIAN_API_KEY` | 阿里百炼 API Key | 可选 |
| `CODEX_EXECUTABLE` | Codex CLI 路径 | 自动检测 |
| `CODEX_TIMEOUT` | Codex 单轮超时秒数 | `1800` |
| `PORT` | Bridge 端口 | `9999` |
| `DASHBOARD_PORT` | Dashboard 端口 | `8888` |

示例：

```bash
export TELEGRAM_BOT_TOKEN='replace-with-your-token'
export STOCK_BOT_TOKEN='replace-with-your-second-token'
```

Dashboard 保存的模型 API Key 位于 `~/.claude/telegram_api_keys.json`，启动脚本会将权限收紧为仅当前用户可读写。

## 启动

从 Windows PowerShell 执行：

```powershell
wsl -d Ubuntu -- bash /mnt/d/AI/claudecode-telegram-main/windows/start.sh
```

启动流程包括：

1. 部署当前 Python 回复 Hook。
2. 创建各 Bot 的 tmux 会话。
3. 启动模型兼容代理和可选飞书桥。
4. 启动 ngrok 并注册带 Secret 的 Telegram Webhook。
5. 启动 Dashboard 和 Bridge。

本地入口：

- Dashboard：`http://127.0.0.1:8888`
- Bridge 健康检查：`http://127.0.0.1:9999/health`

查看后台会话：

```powershell
wsl -d Ubuntu -- tmux attach -t bridge
```

按 `Ctrl+B`，再按 `D`，可退出 tmux 而不停止服务。

## 目录说明

| 路径 | 说明 |
|---|---|
| `bridge.py` | Telegram Webhook、消息路由和 Codex 调用 |
| `dashboard.py` | Web 控制面板与服务管理 |
| `feishu_bridge.py` | 飞书长连接桥接 |
| `anthropic_proxy.py` | Anthropic 与 OpenAI 格式转换 |
| `proactive.py` | 主动任务与 Bot 记忆调度 |
| `hooks/` | 回复发送、文件发送及记忆 Hook |
| `windows/start.sh` | WSL 一键启动入口 |
| `windows/deploy_hook.sh` | 部署当前 Hook |
| `android/` | Android 控制端源码 |

## 构建客户端

Windows 桌面端：

```powershell
build30.bat
```

构建输出位于本地 `dist/`，不会提交到 Git。Android 客户端说明见 [`android/ANDROID_README.md`](android/ANDROID_README.md)。

## 安全建议

- 首次部署前修改源码中的 Telegram Chat ID 白名单。
- 不要提交 `.env*`、证书、Token、API Key、聊天记录或本机配置。
- Dashboard 具备管理能力，建议仅通过可信网络访问。
- 一旦凭据进入 Git 历史，应立即轮换凭据；删除文件或强推不能撤销已经发生的泄露。
- 定期检查 Git 历史、Actions 日志和发布附件中的敏感信息。

## 许可证

当前仓库未附带开源许可证。公开可见不代表自动授权复制、修改或分发；如需开放使用，请添加合适的 `LICENSE`。
