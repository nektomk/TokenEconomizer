"""Qwen Code sessions: a JSONL file per session under `~/.qwen/projects/<slug>/chats/`.

Each line is a record, `uuid`-bearing, with a `type`:

  * `user`     -- a prompt the person typed: `message.parts[].text`, `provenance`
                  `"real_user"`. One `user` record with `provenance` `"system"` is a
                  background-task notification (`subtype` "notification") and is not a prompt.
  * `assistant`-- the model's turn: `message.parts[]` with a `text` part (`thought: true`
                  marks reasoning, `responsesMessage` grips the reply), and a `functionCall`
                  part per tool call: `{id, name, args}`. Every one carries `usageMetadata`
                  (`{promptTokenCount, candidatesTokenCount, thoughtsTokenCount,
                  totalTokenCount, cachedContentTokenCount}`) and `model`.
  * `tool_result` -- one per finished call: `message.parts[].functionResponse` is
                  `{id, name, response: {output | error}}`, paired to its call by `id`; the
                  outcome is `toolCallResult.status` ("success"/"error") with
                  `executionStatus` ("success"/"error"/"not_started").
  * `system`   -- Qwen Code's own bookkeeping (`subtype` "ui_telemetry",
                  "attribution_snapshot", "file_history_snapshot", "slash_command", ...),
                  ignored here.

`qwen_session_to_claude` writes the conversation as the Claude Code envelope the other
readers already handle, so no mechanism needs a Qwen branch. Token accounting is direct: the
log carries it in `usageMetadata` on every assistant entry (verified 2026-11-17 against a
0.27.4 session live log). `totalTokenCount` is `promptTokenCount + candidatesTokenCount`, so
the reasoning tokens (broken out in `thoughtsTokenCount`) are inside the candidate count;
`promptTokenCount` includes the cached reads, so the fresh input is the difference, as on
Grok.

`qwen_json_reply` reads the same figures from a headless run's `--output-format json` array,
which is what a side call (`plugin.sidecall.qwen_call`) answers with.
"""
import json
import os
import re

from .jsonl import records as _records

# Qwen Code's tool names, as the mechanisms know them (the names the micro-skill vocabulary,
# the classifiers and tool pruning key on). The inputs are already the mechanisms' shapes:
# `file_path`, `command`, `pattern`/`path`, `old_string`/`new_string`.
_NAMES = {"read_file": "Read", "write_file": "Write", "edit": "Edit",
          "grep_search": "Grep", "glob": "Glob", "run_shell_command": "Bash",
          "web_fetch": "WebFetch", "web_search": "WebSearch"}
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def qwen_tool(name, args):
    """(name, args) of a Qwen Code tool call in Claude Code's vocabulary: the file, command,
    search and edit tools renamed; any other tool as it is."""
    args = args if isinstance(args, dict) else {}
    mapped = _NAMES.get(name or "")
    if mapped == "Bash":
        return mapped, {"command": str(args.get("command") or "")}
    if mapped == "Read":
        out = {"file_path": args.get("file_path") or ""}
        out.update({k: args[k] for k in ("offset", "limit") if args.get(k) is not None})
        return mapped, out
    if mapped == "Grep":
        out = {"pattern": args.get("pattern") or "", "path": args.get("path") or ""}
        if args.get("glob"):
            out["glob"] = args["glob"]
        return mapped, out
    return mapped or name or "", args


def _is_prompt(ev):
    """A message the person typed, not a task notification Qwen Code added itself."""
    if ev.get("type") != "user" or ev.get("provenance") != "real_user":
        return False
    parts = (ev.get("message") or {}).get("parts")
    if not isinstance(parts, list):
        return False
    return any(isinstance(p, dict) and p.get("text") for p in parts)


def _parts_calls(ev):
    """[(id, name, args)] of the `functionCall` parts of an assistant record, in order."""
    out = []
    parts = (ev.get("message") or {}).get("parts")
    if not isinstance(parts, list):
        return out
    for p in parts:
        if not isinstance(p, dict):
            continue
        fc = p.get("functionCall")
        if not isinstance(fc, dict) or not fc.get("id"):
            continue
        out.append((fc.get("id"), fc.get("name"), fc.get("args") or {}))
    return out


def _parts_result(ev):
    """The first `functionResponse` of a tool_result record: ({id, name, text, failed} | None).

    The outcome's authority is `toolCallResult.status`: "error" (or anything but "success")
    marks the call failed. The response body carries the text -- `output` on success, `error`
    on failure, either inside `response`."""
    failed = (ev.get("toolCallResult") or {}).get("status", "") != "success"
    parts = (ev.get("message") or {}).get("parts")
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            fr = p.get("functionResponse")
            if not isinstance(fr, dict) or not fr.get("id"):
                continue
            body = fr.get("response")
            if isinstance(body, dict):
                text = str(body.get("output") or body.get("error") or "")
                return (fr.get("id"), fr.get("name"), text, failed)
            return (fr.get("id"), fr.get("name"), "", failed)
    return None


def qwen_session_to_claude(path, out_path):
    """Write a Qwen Code session as Claude Code transcript lines, for `load_trajectory`.

    Every record carries `cwd`, so a call is placed where the session ran (a command typed as
    `cd sub && make` is read from the directory where the session was). Returns the number of
    tool calls written, or None when the file is missing or holds no conversation."""
    if not path or not os.path.exists(path):
        return None
    results = {}
    events = []
    n = 0
    for ev in _records(path):
        if ev.get("type") == "user":
            if not _is_prompt(ev):
                continue
            text = "\n".join(p.get("text", "") for p in (ev.get("message") or {}).get("parts")
                             if isinstance(p, dict) and p.get("text"))
            if text.strip():
                events.append(("user", text))
        elif ev.get("type") == "assistant":
            for call_id, name, args in _parts_calls(ev):
                name, args = qwen_tool(name, args)
                cwd = ev.get("cwd") if isinstance(ev.get("cwd"), str) else None
                events.append(("call", call_id, name, args, cwd))
                n += 1
        elif ev.get("type") == "tool_result":
            r = _parts_result(ev)
            if r and r[0]:
                results.setdefault(r[0], r)
    if not events:
        return None
    with open(out_path, "w", encoding="utf-8") as out:
        for ev in events:
            if ev[0] == "user":
                out.write(json.dumps({
                    "type": "user", "message": {"role": "user", "content": ev[1]}}) + "\n")
                continue
            _c, call_id, name, args, cwd = ev
            rec = {"type": "assistant", "message": {
                "role": "assistant", "content": [{
                    "type": "tool_use", "id": call_id, "name": name, "input": args}]}}
            if cwd:
                rec["cwd"] = cwd
            out.write(json.dumps(rec) + "\n")
            res = results.get(call_id)
            if res:
                _rid, _name, text, failed = res
                out.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call_id, "content": text,
                    "is_error": failed}]}}) + "\n")
    return n


def qwen_prompt_count(path):
    """How many prompts the session ran: user records the person typed (not Qwen Code's own
    task notifications)."""
    return sum(1 for ev in _records(path) if _is_prompt(ev))


def qwen_session_usage(path):
    """Token breakdown of a Qwen Code session from its own `usageMetadata`.

    Returns the four breakdown fields `pricing.effective_tokens` reads (`input_tokens` is the
    fresh input: `promptTokenCount` counts the cached reads too, so the cached share is
    subtracted), plus `model` (the most used one) and `requests` (assistant records carrying
    usage). There is no cache-write figure in the log, so `cache_creation_input_tokens` is 0.
    Without any entry, `input_tokens` is None so the ledger says `no_usage` rather than
    guessing.
    """
    totals = {"input_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "output_tokens": 0}
    models, requests = {}, 0
    for ev in _records(path):
        if ev.get("type") != "assistant":
            continue
        um = ev.get("usageMetadata")
        if not isinstance(um, dict) or um.get("promptTokenCount") is None:
            continue
        cached = int(um.get("cachedContentTokenCount") or 0)
        totals["input_tokens"] += max(0, int(um["promptTokenCount"]) - cached)
        totals["cache_read_input_tokens"] += cached
        totals["output_tokens"] += int(um.get("candidatesTokenCount") or 0)
        requests += 1
        model = ev.get("model")
        if model:
            models[model] = models.get(model, 0) + 1
    if not requests:
        return {"input_tokens": None, "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None, "output_tokens": None,
                "requests": 0, "model": None}
    totals["requests"] = requests
    totals["model"] = max(models, key=models.get) if models else None
    return totals


def qwen_safe_session_id(sid):
    """A log's id when it is a plain token (safe as a file name), else None."""
    return sid if isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid) else None


def qwen_json_reply(stdout):
    """(text, usage) of a headless `qwen --output-format json` run: one JSON array of events, a
    `system`/`init` record, the `assistant` records, and a closing `result`.

    The reply is the `result` record's `result`, and `usage` is its `usage` block in the fields
    `pricing.effective_tokens` reads, with the cached reads subtracted from `input_tokens` exactly
    as `qwen_session_usage` does; `model` comes from the `init` record. `usage` is None when the
    run names none -- one aborted at its tool-call limit prints nothing at all, so both come back
    empty and the caller reports no reply."""
    data = None
    for candidate in ((stdout or "").strip(), *reversed((stdout or "").strip().splitlines())):
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    text, usage, model = "", None, None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if isinstance(ev.get("model"), str) and ev["model"]:
            model = ev["model"]
        if ev.get("type") != "result":
            continue
        if isinstance(ev.get("result"), str):
            text = ev["result"]
        u = ev.get("usage")
        if isinstance(u, dict) and u.get("input_tokens") is not None:
            cached = int(u.get("cache_read_input_tokens") or 0)
            usage = {"input_tokens": max(0, int(u["input_tokens"]) - cached),
                     "cache_read_input_tokens": cached,
                     "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
                     "output_tokens": int(u.get("output_tokens") or 0),
                     "model": model}
    return text.strip(), usage
