# Codex Telegram Bridge

通过 Telegram 私聊选择本机项目与 Codex 会话、发送任务，并接收最终答复。
通知、菜单、轮询和路由由普通 Python 处理，不额外调用分发模型。

这是从个人运行环境整理出的源码版本，面向 macOS，界面以中文为主。
仓库不包含任何真实 Bot Token、绑定账号、项目列表、会话记录、日志或个人配置。

## 功能

- `/projects`：项目按钮、搜索、翻页和最近使用排序。
- `/sessions`：当前项目的会话列表，每页五条；明确选择已有或新建会话。
- `/current`：当前目标、模型与推理设置；`/new`：下一条消息新建会话。
- `/status`：运行中的本机任务及任务详情；`/last`：补看最终答复。
- `/health`：接收、扫描、发送和工作线程心跳，以及消息队列状态。
- `/cancel`：取消尚未提交的任务或停止 Bot 自己启动的 CLI 任务。
- 回复完成通知可续接对应的原会话，不受当前项目选择影响。
- 长答复完整拆分；完成回调与本地事件观察器按会话和回合去重。
- 观察器按会话保存读取进度，归档或恢复记录不会从头补推；仅转发监控启用后的完成事件，
  并显示事件的实际完成时间。旧路径进度自动迁移，缺少有效时间的扫描事件不会作为新通知发送。
- 桌面会话通过官方 `codex queue --thread` 交回 Desktop；Bot 创建的 CLI 会话使用 `codex exec resume`。

只有配对成功的私人聊天和用户可以发起任务。完成通知表示模型回合结束，
不保证业务验收成功；Telegram 发送回执也不表示收件人已读。

## 环境要求

- macOS，Python 3.9 或更新版本，仅使用标准库。
- 已安装、登录 Codex；续接桌面会话需要 Desktop 运行，且其 CLI 支持 `queue --thread`。
- Telegram BotFather 创建的专用 Bot，不应被其他轮询程序或 webhook 共用。
- 本机 Codex 的 `state_5.sqlite` 与 rollout 记录。其内部结构可能随版本变化。

默认 CLI 路径为 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`。
CLI 安装位置不同时，通过环境变量指定。注意：当前 Desktop 运行检测仍只识别
`/Applications/ChatGPT.app/Contents/MacOS/ChatGPT`；单独修改 CLI 路径不能改变这一限制。

```sh
export CODEX_CLI="/absolute/path/to/codex"
export CODEX_THREAD_DB="$HOME/.codex/state_5.sqlite"
```

`CODEX_HOME` 可调整默认 Codex 数据目录。使用 LaunchAgent 时，需在其环境中
明确设置这些值。时间显示目前固定为 `Asia/Shanghai`。

## 配置与启动

1. 将项目保存在仅本人可访问的本机目录中。源码与运行数据共用同一个目录，
   私有运行文件由 `.gitignore` 排除。不要让两个服务同时轮询同一个 Bot。
2. 运行配对程序；Token 在终端隐藏输入，不要写入命令行或聊天：

   ```sh
   python3 bridge.py setup
   ```

3. 按终端提示向自己的 Bot 发送带随机验证码的 `/start` 消息。验证码十分钟失效。
   程序生成权限为 `0600` 的 `token` 与 `settings.json`，远程执行默认关闭。
4. 编辑生成的 `settings.json`，保留配对字段，将 `projects` 配成项目名称到真实
   绝对目录的映射。理解执行权限后，可将 `execution_enabled` 设为 `true`。
   `settings.example.json` 仅作结构参考，不要用它覆盖已配对设置。
   `new_thread_defaults` 留空时沿用个人 Codex 配置；也可添加 `model` 和
   `model_reasoning_effort` 来指定新建会话的默认设置。
5. 注册菜单并启动：

   ```sh
   python3 configure_menu.py
   python3 bridge.py run
   ```

在 Telegram 使用 `/projects` → 选择项目 → 选择会话或新建会话 → 发送文字。
源码、配置与数据库均位于本项目目录。长期运行可自行配置用户级
LaunchAgent，使用 Python 和脚本的绝对路径，并将日志放在私有运行目录。
本仓库不安装或改动任何现有后台服务。

## 一个目录，选择性纳入 Git

源码和本机运行数据放在同一个目录，不需要维护第二份代码或同步目录。
Git 只跟踪源码、测试、示例配置和文档。Token、真实配置、数据库、日志与备份
始终留在本机，并由 `.gitignore` 排除。不要使用 `git add -f` 强行添加这些文件。

更新流程：修改代码 → 跑离线测试 → 检查暂存文件和差异 → 本地提交 → 在没有 Bot
CLI 任务运行时重启服务 → 用 `/health` 检查。服务不会自动热重载；notify 回调是
短进程，下次调用会读取新代码。不要用旧数据库或旧个人配置覆盖现有运行数据。

## 可选的完成回调

服务自带本地结束事件观察器。还可以在个人 Codex `config.toml` 配置 `notify`，
指向唯一源码仓库中的 `notify_hook.py`，与观察器去重。修改前保存已有回调；若存在，
将其命令数组保存在运行目录的 `original-notify.json` 中，由新回调继续调用。
不要直接覆盖已有回调或提交原始个人配置。

```toml
notify = ["/absolute/path/to/python3", "/absolute/path/to/notify_hook.py"]
```

全面停用时，同时停止服务并恢复原 notify 回调。只停止后台服务，回调仍可能入队。

## 执行与隐私边界

- 任务在本机执行，沿用 Codex 权限和项目指令。已有会话设置无法保留时拒绝续接。
- 桌面提交被接受不等于完成；仅跟踪关联的请求与回合。超时或重启后不自动重复提交。
- Bot 无法远程取消已交给 Desktop 的任务，应在原桌面会话中停止。
- 一次处理一个 Telegram 执行任务；暂不支持附件、语音、群聊或多用户。
- 超过十五分钟的待提交指令不会静默执行。Mac 睡眠、断网或关机时不能接收任务。
- CLI 工作进程使用绑定其 PID 的保活，结束后释放；本仓库不包含个人电脑常驻保活服务。
- 通知包含会话标题、项目名称与最终答复，并经 Telegram 传输。不要让最终答复包含秘密。
  内置处理会隐藏本 Bot 的精确 Token，但不是通用敏感信息检测器。
- 本地状态库含任务文字、通知内容、配对与回执信息，应保留在受保护的运行目录。
- Telegram 发送结果不确定时不会自动重发；限流会遵守 `retry_after`。
- 依赖 Codex 当前的数据库、事件格式和 CLI 输出，升级后应重新验证兼容性。

## 开发与提交

```sh
python3 -m unittest discover -p 'test_*.py'
```

测试使用临时数据库及虚构数据，不需要真实 Token。不要以实际配对账号运行测试发送。
`.gitignore` 排除凭据、运行设置、数据库、日志、备份和本机资源。提交前仍应检查
`git diff --cached` 与 `git ls-files`；忽略规则无法清除已经进入历史的秘密。

源码尚未指定开源许可证。对外发布前需由维护者选择并加入许可证。

## 设计参考

以下项目用于交互和可靠性设计参考；不作为安装或运行依赖：

- [Takopi](https://github.com/banteg/takopi)
- [codex-telegram-bot](https://github.com/woosungchoi/codex-telegram-bot)
- [tele-codex](https://github.com/emanuel-prado/tele-codex)
- [Telegram Bot API](https://core.telegram.org/bots/api)
