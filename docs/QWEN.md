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
| History import (learn from earlier sessions) | **no** | `QwenCode.keeps_history` is off: the log is readable, but the adapter does not name the directory to scan, so `te history import` answers «no session history the plugin reads» |
| Session cost / effective tokens | yes | the reader extracts usage from each turn's `usageMetadata`; `promptTokenCount` includes cache reads, so `effective_tokens = input - cache_read` |
| Loop guard (redirect a failing loop) | yes | `PostToolUse` supports `additionalContext`. Verified live 2026-11-18: a turn repeating one failing test was redirected at the hard backstop with no model call at all. A paid judge is one rung of the cascade, not a prerequisite |
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

`bin/te` is a POSIX `sh` script. From `cmd.exe` it does not run («is not recognized as an
internal or external command»), and `python bin\te` is a syntax error, because the file is
shell code. Drive the CLI through Python instead, pointing `PYTHONPATH` at the installed
bundle's package:

```bat
set PYTHONPATH=%USERPROFILE%\.qwen\extensions\tokeneconomizer\lib
python -m tokeneconomizer.plugin.cli --host qwen --cwd "%CD%" status
```

**Updating.** The installed copy is a snapshot: a change to `tokeneconomizer/` in this
checkout does not reach it, and the hooks import the installed copy, not the checkout.
`qwen extensions install` also refuses to write over an existing extension («already
installed. Please uninstall it first»), so after any change under `tokeneconomizer/`:

```sh
python scripts/build_plugin.py --qwen
qwen extensions uninstall tokeneconomizer
qwen extensions install --consent "$PWD/dist/tokeneconomizer-qwen/tokeneconomizer"
```

Verify the installed copy by reading it back rather than trusting the exit codes -- a stale
`lib/tokeneconomizer/pricing.py` silently changes every figure the plugin reports, and
`qwen extensions list` reports «enabled» even when nothing of the extension runs.

**Hook commands must carry no quotes.** Qwen Code runs a hook as
`spawn(command, [], {shell: true})`, which on Windows reaches `cmd.exe` as one argv element
with its inner `"` escaped to `\"`; `cmd.exe` has no `\"` rule, so a quoted launcher fails
with «is not recognized». The same string goes to `/bin/sh -c` unescaped on POSIX, so quotes
work there and an unquoted path breaks on a home directory containing a space. The unquoted
form is the only one that runs on both; a Windows install path with a space was already
broken. A test pins the rule.

## The sessions you already ran

**History import is not wired for this host.** `te --host qwen history import` answers
«no session history the plugin reads» and changes nothing, because `QwenCode.keeps_history`
is off. The log format itself is readable -- `~/.qwen/projects/<slug>/chats/<id>.jsonl`
carries the tool calls and a `usageMetadata` block per assistant entry -- but the adapter
does not yet name the directory to scan, and no session id filter is applied. Until it does,
the plugin learns only from sessions that run after it is installed.

That has a consequence worth knowing: a repository's loop-guard envelope starts empty, and
the guard does not act at all until `envelope_min_samples` turns (5 by default) have ended
green. Neither the judge nor the rule fallback redirects a repository whose usual turns it
has not learned -- a deliberate resolve-safety choice, not a fault.

A session is folded when it ends. A one-shot run (`qwen "..."`) does not fire `SessionEnd`,
so its fold waits for the next session start in that repository, which picks up any session
whose transcript has been quiet for 600 s (`fold.STALE_S`); `te --host qwen fold` folds it
now instead, and `te --host qwen fold --session <id>` names one.

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
- **Pricing is a snapshot, and one ratio differs from every other vendor's.** 14 Qwen models
  are priced in `tokeneconomizer/pricing.py` from the provider's `/v1/pricing`, cross-checked
  against the `ratePerMillion` that `/v1/generations` actually bills. A billed id missing from
  that table is priced on a labelled fallback curve and its record says `priced_by: fallback`,
  so watch for that in `te status`. Note `qwen3.8-max` bills a cache hit at **0.267x** input
  (`cacheShare` 4/15), not the 0.1x the other vendors use -- and cache reads are most of an
  agent turn's tokens, so that one ratio moves every total. Re-check the table when the
  provider changes its prices.
- **`headless()` reads an inherited variable.** `QwenCode.headless()` answers from `QWEN_CODE`
  in the hook's environment, and hooks inherit the parent's environment. A one-shot run
  launched from inside an interactive Qwen Code session is therefore seen as interactive, and
  the still-stuck notice says «Press Esc to interrupt it.» where there is no UI to press it in.
  Cosmetic, but the same flag gates side calls, so unset `QWEN_CODE` when a headless run must
  be measured as one.
- **`UserPromptSubmit` fires more than once inside a prompt-turn** -- observed again after a
  tool result, with `prompt_id` unchanged. The controller keeps such a re-fire on the open turn
  instead of closing it as interrupted; without that, every turn that did any work was recorded
  as interrupted, no turn ever ended green, the repository's envelope was never written, and the
  loop guard could not act at all (neither the judge nor the rule fallback redirects without an
  envelope). Verified live 2026-11-18: one turn of four calls, `resolved_proxy: true`,
  `envelope.json` written. A test pins it.
