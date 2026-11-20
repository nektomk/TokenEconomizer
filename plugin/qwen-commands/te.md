---
description: Inspect or steer TokenEconomizer for this repository - status, settings, micro-skills, tool pruning, loop guard
argument-hint: "[status | config [name[=value]] | microskills | facts | repos | toolprune | untool | help]"
when_to_use: The person asks what TokenEconomizer has learned or done for this repository, wants to turn one of its mechanisms on or off, wants a pruned tool back, or asks for the TokenEconomizer CLI that sits behind the `te` launcher. Run the command and relay its output; do not guess the numbers.
---

Run TokenEconomizer's own CLI for this working directory and show me its output verbatim in a code block, with no commentary.

Arguments: `{{args}}` - when empty, use `status`; otherwise use the first word as the subcommand and pass the rest unchanged. `help` prints the CLI's own list of subcommands. If the arguments name something the CLI rejects, show the CLI's error rather than substituting a different command.

Run this command with the Bash tool:

```
te="$(git rev-parse --show-toplevel 2>/dev/null)/tokeneconomizer/te_bootstrap.py"; [ -f "$te" ] || te="${CLAUDE_PLUGIN_ROOT}/scripts/te"; [ -x "$te" ] || te="${TOKENECONOMIZER_HOME:-$HOME/.tokeneconomizer}/bin/te"; if [ "${te%.py}" != "$te" ]; then "$(command -v python3 || command -v python)" "$te" cli --host qwen --cwd "$(pwd)" {{args}}; else "$te" --host qwen --cwd "$(pwd)" {{args}}; fi
```

The cascade is the one the other hosts use: a checkout of this repository runs its own
`te_bootstrap.py`, an installed extension its `scripts/te` (where the host substitutes the plugin
root), and a plain install the launcher from `~/.tokeneconomizer/bin`. On Windows the same three
are `<repo>\tokeneconomizer\te_bootstrap.py`, `%CLAUDE_PLUGIN_ROOT%\scripts\te.cmd` and
`%TOKENECONOMIZER_HOME%\bin\te.cmd`, with `py -3` in front of the first.

`--host qwen` names the destination the CLI reads its state from; the launchers drop it so passing
it is safe for all three. Do not add `--global` unless the person asked for a repository-wide change.

Never invent numbers: every figure you report must come from this run.
