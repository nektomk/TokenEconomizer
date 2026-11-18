---

# TokenEconomizer on Qwen Code

Qwen Code support is experimental. It was built and tested against Qwen Code `0.28.0` on
Windows 11; no other version or OS has been tested. The unquoted hook command form required
on Windows has not been verified on POSIX, so a Qwen Code install on macOS or Linux may break
if the home directory path contains a space.

## What works, and what does not

| Mechanism | Qwen Code | Why |
|---|---|---|
| Micro-skills (repository facts at session start) | yes | `SessionStart` supports `hookSpecificOutput.additionalContext` |
| History import (learn from earlier sessions) | yes | `~/.qwen/projects/<slug>/chats/<id>.jsonl` has the tool calls and `usageMetadata` |
| Session cost / effective tokens | yes | the reader extracts usage from each turn's `usageMetadata`; `promptTokenCount` includes cache reads, so `effective_tokens = input - cache_read` |
| Loop guard (redirect a failing loop) | yes | `PostToolUse` supports `additionalContext`; the judge runs as a side call (`qwen --safe-mode --max-tool-calls 0 --output-format json`) |
| Tool pruning | yes | `tools.disabled` in `.qwen/settings.json` (project scope) removes any tool, MCP included; `permissions.deny` is weaker (MCP exempt) |

The side call was verified live on 2026-11-18: `qwen --safe-mode --max-tool-calls 0` with a
neutral `--system-prompt`, every bundled tool named in `--exclude-tools`, and the prompt as a
positional before that array. `--safe-mode` leaves out hooks, extensions, skills, MCP servers
and QWEN.md, so the repository's own instructions never reach the call. Qwen Code has no option
that unregisters every tool, so `--max-tool-calls 0` is the guarantee: told to write a file,
the call aborted with exit 55 having printed nothing, and the file was not there. Excluding the
tools and overriding the system prompt cuts the call's input from 8 105 to 265 tokens.

## Why a session may learn nothing

Micro-skills reports what it has learned per repository. A directory that is not a git
repository is keyed by its own path, so starting every session in a folder that holds many
unrelated projects mixes evidence from all of them, and a fact from one is contradicted by
another. Start Qwen Code in the project's own folder when you want it to learn that project.

The free layer (`microskills=envfacts`) learns only from repeated environment failures: a
command that fails the same way several sessions in a row and a working alternative. It needs
3 confirmations and 4 sessions in which the fact was exercised before it injects anything.
A repository whose sessions never run tests, builds or failing commands will produce no fact,
by design. The paid layer (`microskills=envfacts+distiller`, or `on`) asks the model for
repository knowledge every few sessions, and runs the proposer as a side call.

## Install

```sh
git clone https://github.com/microsoft/TokenEconomizer.git
cd TokenEconomizer
python scripts/build_plugin.py --qwen
qwen extensions install --consent "$PWD/dist/tokeneconomizer-qwen/tokeneconomizer"
```

The bundle is a native Qwen Code extension: `qwen-extension.json` at its root (six inline
hooks), `scripts/` with the Windows launcher, and the Python package in `lib/` (Qwen Code
copies the extension directory, so a symlink would break; the bundle puts a real copy there).
Start a new Qwen Code session after installing: hooks are snapshotted at startup.

Check it in the repository:

```sh
~/.tokeneconomizer/bin/te --host qwen --cwd "$PWD" status
```

**Hook commands must carry no quotes.** Qwen Code runs a hook as
`spawn(command, [], {shell: true})`, which on Windows reaches `cmd.exe` as one argv element
with its inner `"` escaped to `\"`; `cmd.exe` has no `\"` rule, so a quoted launcher fails
with «is not recognized». The same string goes to `/bin/sh -c` unescaped on POSIX, so quotes
work there and an unquoted path breaks on a home directory containing a space. The unquoted
form is the only one that runs on both; a Windows install path with a space was already
broken. A test pins the rule.

## Import the sessions you already ran

A plugin installed today knows nothing about yesterday's sessions. Qwen Code's own log
directory is read by the same history import the other hosts use:

```sh
~/.tokeneconomizer/bin/te --host qwen --cwd "$PWD" history import
```

The import runs only into a repository with nothing folded yet, folds oldest first, skips
sessions with no prompt and sessions still being written, and makes no model call.
`history_import=on` lets the first session start in a repository do it by itself.

Their usage is recorded, but `te status` counts imported sessions apart from the plugin's
own (`+N from earlier history`), because they ran before it: the totals in
`sessions ... effective tokens` cover the sessions that end after the plugin is installed.

## Settings

The plugin's settings live in `~/.tokeneconomizer/config.json`:

```sh
te --host qwen config set microskills=envfacts   # learn facts, no model call
te --host qwen config set microskills=on         # and let a model propose more, every k sessions
```

`qwen_paid_model` in `~/.tokeneconomizer/config.json` names the judge and proposer model
when it differs from the session's model; when empty, the session's default model is used.
The side call itself is always available on this host (it is not gated by a `qwen_sidecall`
flag), because `qwen` is not in `sidecall.NO_SIDE_CALLS` on 0.28.0.

Tool pruning is on by default. It writes to `.qwen/settings.json` in the repository
(`tools.disabled` array), and adds `/.qwen/settings.json` to `.git/info/exclude` when it
creates the file. `te --host qwen toolprune restore` gives the tools back; what is restored
is never removed again.

The loop guard is on by default (`loopguard=nudge`). The judge runs as a side call and
writes its redirect as `hookSpecificOutput.additionalContext` for `SessionStart` and
`additionalContext` for `PostToolUse`; the notice goes through the model's reply because
Qwen Code relays it (`relays_notices`).

## Model calls and API keys

The plugin needs no API key of its own on Qwen Code: side calls run through the user's own
`qwen` CLI, billed where the session is. What each mechanism calls, how often and which
model it uses:

| Mechanism | Call | When | Model |
|---|---|---|---|
| Micro-skills (free layer) | none | -- | -- |
| Micro-skills (paid layer) | proposer | every `microskills_propose_every` sessions (3 by default) | `qwen_paid_model` if set, else the session's default model |
| Tool pruning | none (envelope learned from live turns) | -- | -- |
| Loop guard | judge | when a turn shows loop signs, after `envelope_min_samples` turns (5) | `qwen_paid_model` if set, else the session's default model |
| Loop guard | redirect writer | after a "stuck" verdict | `qwen_paid_model` if set, else the session's default model |

Side calls run with `TE_SIDECALL=1`, which stops the nested run's own hooks from firing.
The call leaves a session log of its own under `~/.qwen/projects/<slug>/chats/`; no hook
tracks it and `QwenCode.keeps_history` is off, so nothing folds it.

## Known gaps and caveats

- **POSIX not tested.** The unquoted hook command form was verified on Windows only. On macOS
  or Linux, a home directory containing a space would break the unquoted form; the quoted
  form works on POSIX but fails on Windows. Until the unquoted form is verified on POSIX,
  Qwen Code support is Windows-only.
- **No catalog probe.** Qwen Code has no `--tools` or features-list command, so tool pruning
  can only propose a tool that has been called and then abandoned; it never proposes a tool
  that was loaded but never touched. The saving is therefore conservative.
- **`qwen_safe_session_id` unused.** The reader exports a function to extract a safe session
  id from the log, but `QwenCode.keeps_history = False`, so the history import is not wired
  for this host. Decide later: wire it or drop the function.
- **Pricing fallback.** Qwen models are absent from the price table, so totals are shown with
  `priced_by: fallback` (a placeholder rate). Add qwen entries to `tokeneconomizer/pricing.py`
  to see real figures.
