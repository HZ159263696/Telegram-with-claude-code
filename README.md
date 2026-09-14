# Windows 使用说明

通过 Telegram 远程操控运行在 WSL2（Ubuntu）里的 Claude Code。

---

## 环境信息（已安装完成）

| 项目 | 内容 |
|------|------|
| WSL 发行版 | Ubuntu 22.04 LTS，安装在 `D:\WSL\Ubuntu` |
| WSL 用户名 | `<user>` |
| Claude Code | 已安装 |
| Stop 钩子 | `/home/<user>/.claude/hooks/send-to-telegram.sh` |
| Bot Token | 已保存到 `/etc/claude_env.sh` 和 `~/.profile` |

---

## 每次启动（日常使用）

打开 PowerShell，运行：

```powershell
wsl -d Ubuntu -- bash /mnt/d/AI/claudecode-telegram-main/windows/start.sh
```

启动后会自动完成：
1. 创建名为 `bridge` 的 tmux 会话
2. 在 `bridge` 会话内新开 `claude` 窗口并运行 Claude Code
3. 启动 localtunnel 公网 HTTPS 隧道
4. 自动向 Telegram 注册 Webhook
5. 启动桥接服务器（监听 8080 端口，自动重启）

看到 `Bridge on :8080` 后即可在 Telegram 里发消息给 Bot。

---

## Telegram Bot 命令

| 命令 | 功能 |
|------|------|
| 直接发文字 | 将消息发送给 Claude Code 执行 |
| 直接发图片 | 图片下载到本地后将路径发给 Claude |
| `/status` | 查看 tmux 会话状态 |
| `/stop` | 中断当前 Claude 任务（发送 Escape） |
| `/clear` | 清空当前对话 |
| `/continue_` | 继续最近一次会话 |
| `/resume` | 显示历史会话列表，选择恢复 |
| `/loop <提示词>` | 启动 Ralph Loop（最多 5 次迭代） |
| `/model` | 切换 Claude 模型（Opus / Sonnet / Haiku） |
| `/restart` | 重启桥接服务器 bridge.py |

---

## 常用维护命令

**进入 WSL Ubuntu 终端：**
```powershell
wsl -d Ubuntu
tmux attach -t bridge 进入关闭bridge
```

**查看/接管桥接服务器的终端窗口：**
```bash
tmux attach -t bridge
# 退出但不关闭：按 Ctrl+B，然后按 D
```

**在 bridge 会话内切换到 Claude Code 窗口：**
```bash
# 接管后按 Ctrl+B，再按数字 1（或用方向键选窗口）
# 窗口 0 = bridge 服务，窗口 1 = claude
```

**手动启动 Claude Code 窗口（如果丢失）：**
```bash
tmux new-window -t bridge -n claude "claude --dangerously-skip-permissions"
```

**更新 Bot Token：**
```bash
# 在 WSL 里编辑
sudo nano /etc/claude_env.sh
# 找到 TELEGRAM_BOT_TOKEN 那行，修改后保存
source /etc/claude_env.sh
```

**查看 Stop 钩子状态（调试用）：**
```bash
ls ~/.claude/telegram_pending  # 存在时说明正在等待响应
cat ~/.claude/telegram_chat_id # 当前绑定的 Telegram Chat ID
```

---

## 工作原理

```
你在 Telegram 发消息（文字或图片）
    ↓
localtunnel 转发到本地 8080 端口
    ↓
bridge.py 收到 webhook
    ↓
tmux send-keys 注入文字到 Claude Code（claude 窗口）
    ↓
Claude 执行完毕，触发 Stop 钩子
    ↓
send-to-telegram.sh 读取回复，发回 Telegram
```

---

## 路径速查

| Windows 路径 | WSL 路径 |
|---|---|
| `D:\AI\claudecode-telegram-main` | `/mnt/d/AI/claudecode-telegram-main` |
| `D:\WSL\Ubuntu` | Ubuntu 系统文件 |
| `C:\Users\<user>\` | `/mnt/c/Users/<user>/` |
| WSL 用户目录 | `/home/<user>/` |
| Claude 配置 | `/home/<user>/.claude/` |
