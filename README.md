# Codex Telegram Bridge

[![Tests](https://github.com/wyfblues/codex-telegram-bridge/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/wyfblues/codex-telegram-bridge/actions/workflows/tests.yml)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB)
![macOS](https://img.shields.io/badge/macOS-local--first-161B22)
[![MIT License](https://img.shields.io/badge/License-MIT-7AC9B0)](LICENSE)

**Get Codex results in Telegram. Reply to keep working in the same session.**

English · [简体中文](README.zh-CN.md)

Codex Telegram Bridge is an open-source bot that connects a private Telegram
chat to Codex on your Mac. Choose a project and conversation, send a task, and
receive its progress and full answer in Telegram. Reply to the result to
continue the original conversation with its context and execution settings.

If Telegram is where you already check messages, you can follow your Codex work
there too. Read a result, ask for another change, or send a screenshot without
switching to a separate app to find the conversation.

[Official Codex on mobile](https://learn.chatgpt.com/docs/mobile) also connects to
your computer and continues existing conversations. This project gives that
workflow a Telegram entry point, with a few deliberate choices:

- **Reply to the result you mean.** Replies target that message's original
  conversation, even after you select a different project.
- **Keep progress in one message.** An optional card updates in place. The full
  final answer arrives separately, split without truncation when it is long.
- **Send screenshots and get original images back.** Send one photo with a
  caption; receive previews of local images attached to a final answer, with a
  button to request the original file. Optional cleanup retains incoming copies
  for 7 days and outgoing original copies for 30 days, protects active tasks and
  pending deliveries, and leaves project originals alone.
- **Run and change the bridge yourself.** It uses the Python standard library
  and MIT-licensed source. Routing and notifications need no extra dispatcher
  model; Codex tasks still use your existing account and model.

![Telegram to local Codex workflow](docs/images/overview.png)

<details>
<summary>Watch the 10-second illustrated walkthrough</summary>

![Project selection, a single progress card, and a final reply in Telegram](docs/images/demo.gif)

</details>

*The visuals use fictional projects, conversations, messages, and images. They
are an illustrative demo, not a recording of a private conversation.*

## How does it compare with official Codex on mobile?

The official app already supports local conversations, progress, notifications,
and attachments. It also offers command approvals, code diffs, and guidance for
work that is already running. Those are useful reasons to choose it.
Sources: [mobile guide](https://learn.chatgpt.com/docs/mobile),
[remote connections](https://learn.chatgpt.com/docs/remote-connections), and
[mobile workflow guide](https://learn.chatgpt.com/blog/mastering-codex-remote-for-engineering).

| Your workflow | Official Codex on mobile | This bridge |
| --- | --- | --- |
| Where you work from | Codex in the ChatGPT mobile app | A paired private Telegram chat |
| Where tasks run | Cloud or a connected Mac or Windows computer | Configured projects on your Mac |
| Continue an existing conversation | Supported | Supported; replying to a delivered result selects its original conversation |
| Follow progress and results | Task views and notifications | One updating progress card and full answers in the chat |
| Approve commands and review code | Approval controls, changed files, and diffs | Text and image results; no approval or dedicated diff interface |
| Guide an active task | Queue and steering controls | Follow up after the task ends; no live steering or user-managed backlog |
| Set it up and maintain it | Pair supported devices in the official apps | Run your own Python service and maintain compatibility with Codex |

Choose the official app when you want to review code, handle approvals, or manage
active work from your phone. Choose this bridge when you mainly want to receive
results and send follow-ups in Telegram, and are comfortable maintaining a local
service. Both ways of working on your own computer need that host awake and
online. Messages and attached images sent through this bridge pass through
Telegram.

## How does it compare with other Telegram bridges?

Existing projects also support conversation continuation, progress, and images.
Their workflows are worth comparing before you choose:

| Project | Main focus in its published documentation |
| --- | --- |
| [Codex Telegram Frontend](https://github.com/anton88vlc/codex-telegram-frontend/blob/fb3b296ab5675cd05449673f972bcc6c5852d751/README.md) | A Desktop frontend organized around project groups and conversation topics, with approvals, plans, and file-change progress |
| [jvogan/telegram-codex-bridge](https://github.com/jvogan/telegram-codex-bridge/blob/e5ab6e2e94fac5bff80fe7bee1727ccd142de007/README.md) | A bridge to existing Desktop conversations, with files, images, voice, and optional live calls |
| [Codex Telegram Bot](https://github.com/woosungchoi/codex-telegram-bot/blob/9a801ca39ad901bf623d8a3357308b4d7cc2e818/README.md) | A CLI/SDK bot with project and session menus, a task queue, settings, and maintenance tools |

This bridge focuses on one person's private chat. For example, you receive a
result from project A, then switch to project B. Replying to A's old result still
continues A's original conversation. The bridge stores the delivered message's
conversation mapping, so the current project selection does not redirect that
reply. Together with one updating progress card, explicit image-retention rules,
and Python standard-library deployment, this is the workflow the project is
designed to support. Choose it if you want to manage personal projects in one
private chat and control how messages return to conversations and how long local
image copies are kept.

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

You need macOS, Python 3.9+, and a signed-in local Codex installation. No `pip`
packages, Telegram `api_id`/`api_hash`, public server, or inbound port are needed.
This guide runs the bridge in a terminal; it does not install a background service.

1. **Download the source and enter its directory.** With Git installed:

   ```sh
   git clone https://github.com/wyfblues/codex-telegram-bridge.git
   cd codex-telegram-bridge
   chmod 700 .
   ```

   Alternatively, download GitHub's ZIP, extract it, and open a terminal in the
   extracted directory, then run `chmod 700 .`. Run all following commands there,
   using the same Python.

2. **Check Python and Codex before pairing.** Open Desktop, sign in, and create
   a conversation in the project directory you want to use. Send a simple text
   request and wait for its answer; an empty new chat may not have a resumable
   record yet. Check the default CLI:

   ```sh
   python3 --version
   export CODEX_CLI="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
   "$CODEX_CLI" --version
   "$CODEX_CLI" queue --help
   ```

   Python must be 3.9 or newer. The CLI must run, and its `queue` help must include
   `--thread`. If not, resolve the installation or CLI compatibility first.

   **Desktop compatibility:** the running-app check currently recognizes only
   `/Applications/ChatGPT.app/Contents/MacOS/ChatGPT`. Changing `CODEX_CLI` does
   not change that check; an installation named `Codex.app` is not automatically
   compatible. Do not rename the app to work around it.

   Conversation discovery also needs the existing Codex database, normally
   `$HOME/.codex/state_5.sqlite`. For a different location, set
   `CODEX_THREAD_DB` to its absolute path before running the bridge. `CODEX_HOME`
   changes the default Codex data directory. Do not create an empty database or
   copy someone else's. A custom CLI path, if needed, replaces the value above.
   Environment variables must also be set in any future LaunchAgent.

3. **Create your own bot and pair your account.** In Telegram, open the official
   [BotFather](https://t.me/BotFather), send `/newbot`, and follow its prompts for
   a display name and a username ending in `bot`. It returns your Bot Token.
   See [Telegram's bot creation guide](https://core.telegram.org/bots/features#creating-a-new-bot).
   Use a dedicated bot with no existing webhook or other polling program.

   ```sh
   python3 bridge.py setup
   ```

   Paste the token at `Paste BotFather token (hidden):`; characters will not be
   shown. Do not put it in a command, README, issue, or Telegram message.
   The next prompt asks for an HTTPS proxy URL:

   - If the Mac can reach Telegram directly, press Enter to leave it blank.
   - Otherwise, enter your HTTP proxy endpoint, such as
     `http://127.0.0.1:7890`, with your actual port. SOCKS URLs are not supported.

   A blank value uses a direct connection; this bridge does not inherit system
   proxy settings or `HTTPS_PROXY`. The `setup` command asks interactively, so do
   not use `setup --proxy` to supply this value.

   Keep the terminal open. Open the bot link it prints and send the **complete**
   `/start <challenge>` line from your own private Telegram account within the
   pairing window of about 10 minutes. Pressing Start alone is insufficient.
   Successful pairing prints `Paired.` and creates local `token`,
   `settings.json` (both `0600`), and `state.sqlite3`. Execution starts disabled.
   You do not need to find or enter chat, user, or bot IDs yourself.

4. **Configure a project.** Open the generated `settings.json` in a plain-text
   editor. Modify these fields inside it, preserving the other fields:

   ```json
   {
     "projects": {
       "demo": "/absolute/path/to/your/project"
     },
     "execution_enabled": true
   }
   ```

   Replace the example path with an existing absolute project directory, exactly
   matching the directory used by its Codex conversation. Do not use `~`, a
   different worktree, or a symlink alias. `demo` is your menu label and can change.
   `execution_enabled: true` permits your paired account to submit Codex tasks.
   This is a **fragment**, not a replacement configuration: keep `proxy`,
   `chat_id`, `user_id`, `bot_id`, and `bot_username` generated by setup.
   Never copy [settings.example.json](settings.example.json) over the paired file.

   Progress, image sending, image receiving, and cleanup are optional and default
   to off. Add `progress_cards_enabled`, `image_delivery_enabled`,
   `image_receive_enabled`, and `image_cleanup_enabled` with value `true` to
   enable them. Check JSON syntax without printing its private contents:

   ```sh
   python3 -m json.tool settings.json >/dev/null
   ```

5. **Register the menu, start, and verify.** Register once; successful output
   includes `"verified": true`. Then start the foreground service:

   ```sh
   python3 configure_menu.py
   python3 bridge.py run
   ```

   Keep this terminal open and the Mac awake and online. The service may be quiet
   while idle. In your paired private chat:

   - Send `/health`. Check that execution is enabled and the polling, sending,
     worker, and observer checks have recent heartbeats without ongoing errors.
   - Send `/projects`, select your project and an idle existing conversation,
     then use `/current` to confirm the target. If you have no conversation yet,
     create one in Codex in that same directory, complete one simple text
     exchange, and refresh `/sessions`.
   - Send a small test: `Reply only: connection successful. Do not modify files
     or run commands.` Confirm that it runs in the chosen conversation and that
     its complete answer returns to Telegram. `/health` alone does not prove this.

   To change settings later, stop **your own** foreground service with Ctrl+C
   while tasks and delivery are idle, edit, and start it again. Do not run two
   pollers for one bot. The bundled `.command` scripts only perform pairing;
   they do not register the menu or start the service. For unattended use,
   configure your own user LaunchAgent with absolute paths and private logs.

The built-in event observer works without changing Codex configuration. An
optional completion callback is documented in the
[Chinese operating guide](README.zh-CN.md#可选的完成回调); preserve any existing
callback before adding it.

## Installation troubleshooting

The CLI deliberately hides exception details because a network error can contain
the token URL. Use the error type, Telegram code, and the stage that failed:

| Symptom | What to check |
| --- | --- |
| `python3` missing, below 3.9, or import failure | Install a compatible Python and use that same interpreter for every command. No third-party Python packages are required. |
| `FileNotFoundError` before the first run | Enter the source directory and complete `setup`; `token` and the generated `settings.json` must exist there. |
| `Rejected` with Telegram code 401 or 404 during setup | Check the Bot Token from BotFather. If no `settings.json` was created, run `setup` again with the correct token. |
| `RuntimeError` before a pairing link appears | An existing `settings.json` prevents re-pairing; edit that configuration. Otherwise, copy the complete BotFather token again without whitespace. A webhook may also own this bot: use a new dedicated bot. The bridge does not remove webhooks. |
| Pairing times out after a link was printed | Send the exact new challenge to the correct bot in a private chat. If no `settings.json` exists, rerun `setup` to get a fresh challenge. |
| `URLError`, timeout, or repeated polling failures | Check Telegram connectivity and the explicit HTTP proxy. For HTTPS certificate errors, repair the Python installation's certificate trust; do not disable TLS verification. |
| Telegram code 409 | Another poller may be using the same bot. Stop your duplicate instance or use a separate bot; do not stop unrelated services. |
| `JSONDecodeError` or startup failure after editing settings | Run the JSON syntax check above. Fix the existing file; preserve pairing fields and keep `projects` an object of names to absolute directory paths. |
| `BlockingIOError` when starting | This checkout already has a running bridge. Do not start a second instance or remove its lock. |
| Empty session list or persistent observer errors | Open Codex and verify its database location, compatibility, and read access. For an empty list, also check that the configured directory exactly matches the conversation's working directory. |
| `DESKTOP_OFFLINE`, `START_FAILED`, or no `queue --thread` support | Repeat the Desktop and CLI checks in step 2. Pairing success does not establish Codex compatibility. Do not blindly resubmit a task with an uncertain submission result. |

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
