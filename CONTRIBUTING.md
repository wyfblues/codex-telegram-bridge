# Contributing

The bridge uses Python 3.9 or later and the standard library. Keep changes
focused and preserve unrelated local work.

## Pull request workflow

Publish changes to `main` through a pull request:

1. Develop on a typed working branch based on the latest `origin/main`.
2. Run the offline tests, review the full diff, and inspect every outgoing
   commit's English-only ASCII metadata before pushing.
3. Push the working branch and open a PR targeting `main`, describing the
   behavior change and validation.
4. Wait for CI and required reviews, merge without bypassing branch protections,
   and synchronize the local `main` branch.

Do not push changes directly to `main`. Remote operations require explicit owner
authorization. Publishing code does not restart or reconfigure a running bridge;
coordinate runtime activation separately.

## Validation

Run the offline test suite from the repository root:

```sh
python3 -m unittest discover -p 'test_*.py'
```

Tests must use temporary databases, synthetic messages, and mocked network and
process calls. They must not require credentials, poll a real Telegram bot, send
messages, or launch Codex tasks. Never start a second polling process for a bot
that already has a running bridge. Real integration checks need explicit owner
authorization and a separate test bot or a coordinated maintenance window.

## Behavior to preserve

- Route replies through persisted receipts to the exact original conversation.
- Preserve its model, reasoning effort, provider, permissions, working directory,
  and instructions. Reject continuation when these cannot be preserved.
- Keep execution acceptance, task completion, and Telegram delivery distinct.
  Do not automatically repeat execution or an uncertain initial send.
- Keep progress updates bounded and exclude reasoning, prompts, and raw tool logs.
- Protect queued or active image consumers and never delete project originals.

## Private files and Git metadata

Do not commit tokens, real settings, paired account IDs, databases, logs, session
records, image caches, backups, or local verification assets. Use sanitized
examples. Inspect the staged diff and file list before committing; ignore rules
do not protect files that are already tracked.

Use English and printable ASCII for all Git/GitHub metadata and change-history
entries, including commit messages, branch names, tags, pull requests, and release
notes. `main` is the default branch. Base contributions on `main` and use
`<type>/<short-english-description>` for working branches, with `feat`, `fix`,
`docs`, `style`, `refactor`, `perf`, `test`, or `chore` as the type.
