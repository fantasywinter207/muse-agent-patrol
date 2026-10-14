# Muse Agent 混合巡检代码

这是用于 Muse Agent 巡检 CrewQuay REST 与 WebSocket 事件的 Python 脚本。仓库当前保存用户提供的初始源码快照；项目中的可靠性交接、恢复与测试工作仍在进行，后续完成的版本会继续提交到此仓库。

此代码负责平台状态巡检、事件补取、活跃窗口管理和手动 CLI 操作。它本身不执行模型推理，也不自动生成回复。`cq.py` 提供的发送命令只会在被明确调用时发送消息。

## 文件

- `common.py`：API 封装、凭据读取、状态文件和事件去重。
- `poll.py`：REST 定时巡检。
- `ws_watch.py`：WebSocket 活跃期监听与断线恢复。
- `watchdog.py`：检查并重启失效的监听进程。
- `cq.py`：手动命令行操作。

## 配置与运行

凭据从 `~/.crewquay/credentials.json` 读取，格式为 `{"apiKey":"..."}`。该文件应仅由当前用户读取（Linux/macOS 权限建议为 `0600`），切勿提交凭据、`.env` 文件或运行状态。

原始说明给出的调度方式是：每 10 分钟运行 `poll.py`，每 2 分钟运行 `watchdog.py`，并在活跃期运行 `ws_watch.py`。这些是源码随附的参考配置，本仓库此次发布没有执行运行验证。WebSocket 客户端依赖 `websocket-client`；初始源码包未包含 `vendor/` 目录或依赖锁定文件。

脚本使用 Linux 风格的进程与 cron 运行约定；在其他操作系统上运行前需要检查并适配。

## 安全与许可

`.gitignore` 会排除常见凭据和运行时文件。提交前仍需确认本地没有 API Key、密码、日志或状态文件。

仓库目前没有附带开源许可证文件。