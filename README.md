# Codex Telegram Bridge

[![Tests](https://github.com/wyfblues/codex-telegram-bridge/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/wyfblues/codex-telegram-bridge/actions/workflows/tests.yml)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB)
![macOS](https://img.shields.io/badge/macOS-local--first-161B22)
[![MIT License](https://img.shields.io/badge/License-MIT-7AC9B0)](LICENSE)

**Leave your desk. Keep your Codex conversation going.**

English · [简体中文](README.zh-CN.md)

Codex is still checking code or running tests, but you need to head out. Later,
you want to read the result, ask a follow-up, or send a screenshot of something
you noticed. Codex Telegram Bridge connects your phone's Telegram chat to Codex
on your Mac, so you can keep working without returning to your desk.

Choose a project and an existing conversation, then send your task. It continues
in the original Codex session with its context and execution settings. When the
answer arrives, reply to that message to continue the same conversation, even
if you have since selected a different project. There is no need to explain the
background again or keep track of which chat a result belongs to.

You can follow the work without filling your chat with updates. An optional
progress card edits the same message with status, elapsed time, and the latest
public progress. The full final answer arrives separately; long answers are
split without truncation. Send one photo with a caption to an existing session,
receive previews of local images attached to Codex's final answer, and request
the original files when you need the detail. Optional cleanup manages those
image copies without touching project originals.

The bridge runs on your own Mac using the Python standard library. Your projects
and task execution stay there, and only the paired private Telegram account can
submit work. Ordinary code handles polling, routing, notifications, progress,
and image management, without an extra model dispatching requests. Keep your Mac
awake, online, and Codex available, and your phone becomes another way to pick up
the work you already started.

![Telegram to local Codex workflow](docs/images/overview.png)

<details>
<summary>Watch the 10-second illustrated walkthrough</summary>

![Project selection, a single progress card, and a final reply in Telegram](docs/images/demo.gif)

</details>

*The visuals use fictional projects, conversations, messages, and images. They
are an illustrative demo, not a recording of a private conversation.*

## How it works

Telegram sends commands to a local Python service. The service checks both the
paired private chat and user, fixes the target session, and submits the task:

- Existing Desktop sessions use `codex queue --thread` to continue in Desktop.
- Bot-created CLI sessions use `codex exec resume`.
- Local completion events and an optional Codex `notify` callback feed one
  deduplicated outbox. Replies to delivered notifications target the original
  session, independently of the currently selected project.

SQLite stores routing, queue state, receipts, and event checkpoints beside the
source. Image copies stay in private, Git-ignored directories. No database
server, Python package installation, or dispatcher model is required.

## Features

| Capability | What is supported |
| --- | --- |
| Project and session selection | Search, pagination, recent sessions, and explicit selection of an existing or new conversation |
| Original-session continuation | Preserve the original execution settings; refuse continuation when they cannot be retained |
| Progress cards | Optional state, elapsed time, and latest public commentary in one message; edits at most every 10 seconds |
| Full final answers | Long replies split without truncation; callback and observer notices deduplicate by thread and turn |
| Image delivery | Explicit local images in final answers become previews or original files, with a button to request the original |
| Image input | One photo with its caption, sent to an explicitly selected or notification-linked existing session |
| Image retention | Optional 7-day incoming and 30-day original-copy cleanup, protecting active tasks and pending deliveries |
| Status and recovery | Task panels, health checks, recent results, and conservative handling of uncertain submission or delivery |

Final answers remain separate from the progress card. A turn ending does not
prove that its requested business outcome succeeded, and a Telegram send receipt
does not prove that a person read the message.

## Quick start

Allow about five minutes once the prerequisites are ready. Setup is manual and
does not install or replace a background service.

You need macOS, Python 3.9+, a signed-in local Codex installation, and a dedicated
bot created with [BotFather](https://t.me/BotFather). Do not share that bot with
another poller or webhook. Desktop continuation also requires a running Desktop
app and a CLI that supports `queue --thread`.

**Check the installation paths first.** The current CLI default is
`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`. Desktop process
detection is hard-coded to `/Applications/ChatGPT.app/Contents/MacOS/ChatGPT`.
Setting `CODEX_CLI` changes the CLI path but does not change that detection; an
installation named `Codex.app` is not automatically compatible with this check.

For a different CLI or local database path, set these **before** setup and run:

```sh
export CODEX_CLI="/absolute/path/to/codex"
export CODEX_THREAD_DB="$HOME/.codex/state_5.sqlite"
```

`CODEX_HOME` can change the default Codex data directory. Set these variables in
the LaunchAgent environment too if you use one. The bridge currently displays
times in `Asia/Shanghai`.

1. Clone into a private local directory and pair the bot:

   ```sh
   git clone https://github.com/wyfblues/codex-telegram-bridge.git
   cd codex-telegram-bridge
   chmod 700 .
   python3 bridge.py setup
   ```

   Enter the token in the terminal's hidden prompt. Send the displayed
   `/start <challenge>` to your bot from your private Telegram account within
   10 minutes. Setup creates local `token` and `settings.json` files with `0600`
   permissions; remote execution starts disabled.

2. Edit the generated `settings.json`, preserving its pairing fields. Set
   `projects` to your own absolute directories and enable execution when ready:

   ```json
   {
     "projects": {
       "demo": "/absolute/path/to/your/project"
     },
     "execution_enabled": true
   }
   ```

   This is a fragment, not a replacement configuration. Use
   [settings.example.json](settings.example.json) for the full structure, and
   never overwrite the paired file with the example. Optional features are
   disabled by default: `progress_cards_enabled`, `image_delivery_enabled`,
   `image_receive_enabled`, and `image_cleanup_enabled`.

3. Register the menu and run the service:

   ```sh
   python3 configure_menu.py
   python3 bridge.py run
   ```

   In Telegram, send `/projects`, choose a project and conversation, then send a
   task. Use `/health` to check the connection. Keep this terminal running, or
   configure your own user LaunchAgent with absolute paths and private logs.

The built-in event observer works without changing Codex configuration. An
optional completion callback is documented in the
[Chinese operating guide](README.zh-CN.md#可选的完成回调); preserve any existing
callback before adding it.

## Commands

| Command | Purpose |
| --- | --- |
| `/projects [keyword]` | Browse or search configured projects |
| `/sessions [keyword]` | Browse or search the selected project's conversations |
| `/current` | Show the current target, model, and reasoning setting |
| `/new` | Create a new conversation for the next text task |
| `/status` | Inspect local running tasks and task panels |
| `/last` | Retrieve the selected conversation's latest final answer |
| `/cancel` | Cancel an unsubmitted task or stop a bot-owned CLI task |
| `/health` | Inspect polling, observation, delivery, and optional features |

Replying to a delivered result, image, or progress card takes precedence over
the project selection. It continues that message's original session without
changing your selection.

## Images and progress

Enable optional features in the local settings, run the offline tests, and
restart only while bot tasks and message delivery are idle. The service does
not hot-reload its configuration or source.

- **Progress:** one card per tracked turn, showing only explicitly public
  commentary. Reasoning, prompts, and tool logs are excluded. Terminal cards
  freeze, and editing failures do not create replacement messages.
- **Incoming:** send one PNG/JPEG photo with a caption to an existing session.
  Telegram may compress photos; downloads are limited to 20 MB. Desktop receives
  a validated local path and instructions to open the image; CLI continuation
  uses `--image`.
- **Outgoing:** attach an absolute local path to the final answer with Markdown
  image syntax. PNG/JPEG previews up to 10 MB are preferred; originals and other
  supported image formats use file delivery. The maximum is 50 MB per image and
  10 images per turn. Remote image URLs are not downloaded.
- **Cleanup:** incoming copies retain 7 days, outgoing original copies 30 days.
  Active and queued task references, pending or uncertain deliveries, and shared
  protected copies defer deletion. Project originals are never cleanup targets.
  Expired original buttons explain that the local copy is no longer available.

Example final-answer attachment:

```md
![Screenshot](/absolute/path/to/screenshot.png)
![Original](/absolute/path/to/export.png)
```

An alt label beginning with `original` or `原图` requests direct original-file
delivery. See the [Chinese guide](README.zh-CN.md) for format details, retention
rules, and operational recovery.

## Privacy and execution boundaries

- Only the paired private chat and user can submit tasks. Tasks still run with
  Codex's permissions and project instructions; pairing is not a new sandbox.
- Project names, conversation titles, public progress, final answers, and
  explicitly attached images pass through Telegram. Keep secrets out of those
  outputs. The bridge redacts its exact bot token, not arbitrary sensitive data.
- The local state database can contain task text, notification content, pairing,
  and delivery receipts. Protect the entire checkout, its backups, and logs.
- Source and runtime data share one directory. `.gitignore` excludes credentials,
  personal settings, databases, logs, backups, and media copies. Check staged
  files before publishing; ignore rules cannot remove secrets from Git history.
- Unknown send or submission outcomes are not automatically repeated. Rate
  limits respect `retry_after`. This favors avoiding duplicate work over blindly
  retrying a request that might already have succeeded.

## Known limitations

- macOS only, with the `ChatGPT.app` process check described above. Codex database
  schemas, rollout events, and CLI output are internal compatibility boundaries;
  recheck them after upgrades. Compatibility with every Desktop release is not
  claimed.
- One Telegram execution task at a time. No user-managed backlog, live steering,
  remote Desktop interruption, approval UI, voice input, general document input,
  albums, group chat, or multiple users.
- Image input requires an existing conversation and a caption. If the original
  model or tools cannot inspect images, the task must say so.
- A sleeping, offline, or powered-off Mac cannot process commands. Instructions
  older than 15 minutes are not silently executed.
- Bot-owned CLI workers hold a power assertion for their own process lifetime.
  This repository does not install a permanent keep-awake service.
- UI text is mostly Chinese; displayed times use `Asia/Shanghai`.

## Testing

```sh
python3 -m unittest discover -p 'test_*.py'
```

The suite contains **215 offline tests**, verified locally and run in GitHub
Actions on Ubuntu and macOS with Python 3.9 and 3.12. The badge above links to
the latest hosted result. Tests use temporary databases, mocked transports,
and fictional data; no real bot token, Desktop process, or Telegram recipient
is required. Live acceptance is separate and needs a deliberately chosen test
account and content.

## License and credits

[MIT License](LICENSE). Codex and Telegram belong to their respective owners;
this project is independently maintained and is not endorsed by either.

Design references, not runtime dependencies:

- [Codex Telegram Frontend](https://github.com/anton88vlc/codex-telegram-frontend)
- [Takopi](https://github.com/banteg/takopi)
- [Codex Telegram Bot](https://github.com/woosungchoi/codex-telegram-bot)
- [tele-codex](https://github.com/emanuel-prado/tele-codex)
- [Telegram Bot API](https://core.telegram.org/bots/api)
