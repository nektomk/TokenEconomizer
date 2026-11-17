"""Qwen Code: the session reader, its host adapter, and usage."""
import json
import time

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import accounting, hosts


def _user(sid, text, uuid, cwd=None, prompt_id=None, provenance="real_user"):
    """A Qwen Code `user` record — a prompt the person typed."""
    rec = {"uuid": uuid, "sessionId": sid, "timestamp": "2026-01-01T00:00:00.000Z",
           "type": "user", "provenance": provenance, "version": "0.27.4", "cwd": cwd,
           "message": {"role": "user", "parts": [{"text": text, "responsesMessage": {}}]}}
    if prompt_id:
        rec["promptId"] = prompt_id
    return rec


def _assistant(sid, uuid, model, calls=(), text=None, thought=None, usage=None, cwd=None):
    """A Qwen Code `assistant` record; `calls` is [(call_id, name, args)]."""
    parts = []
    if thought:
        parts.append({"text": thought, "thought": True})
    if text:
        parts.append({"text": text, "responsesMessage": {"id": "msg-" + uuid}})
    for call_id, name, args in calls:
        parts.append({"functionCall": {"id": call_id, "name": name, "args": args}})
    um = usage or {"promptTokenCount": 0, "candidatesTokenCount": 0,
                   "thoughtsTokenCount": 0, "totalTokenCount": 0,
                   "cachedContentTokenCount": 0}
    return {"uuid": uuid, "sessionId": sid, "timestamp": "2026-01-01T00:01:00.000Z",
            "type": "assistant", "provenance": "assistant_output", "version": "0.27.4",
            "model": model, "cwd": cwd, "usageMetadata": um,
            "message": {"role": "model", "parts": parts}}


def _result(sid, call_id, name, output=None, error=None, status="success", uuid=None):
    """A Qwen Code `tool_result` record, paired to its call by id."""
    body = {"output": output} if output is not None else {"error": error}
    return {"uuid": uuid or ("r" + call_id), "sessionId": sid,
            "timestamp": "2026-01-01T00:02:00.000Z", "type": "tool_result",
            "provenance": "tool_result", "version": "0.27.4",
            "message": {"role": "user", "parts": [
                {"functionResponse": {"id": call_id, "name": name, "response": body}}]},
            "toolCallResult": {"callId": call_id, "status": status,
                               "executionStatus": "success" if status == "success" else "error"}}


def _notify(sid, uuid, task="b1"):
    """A background-task notification Qwen Code adds as a `user` record of its own."""
    return {"uuid": uuid, "sessionId": sid, "timestamp": "2026-01-01T00:03:00.000Z",
            "type": "user", "provenance": "system", "subtype": "notification",
            "deliveredTurn": True, "version": "0.27.4",
            "message": {"role": "user", "parts": [
                {"text": f"<task-notification><task-id>{task}</task-id><status>completed"
                         "</status></task-notification>"}]}}


def _write(root, sid, records):
    path = root / f"{sid}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def _session(tmp_path, sid="s1"):
    """A session: one prompt, two calls (one failed), usage on both assistant entries."""
    recs = [
        _user(sid, "run the tests", "u1", cwd="/w/repo"),
        _assistant(sid, "a1", "qwen/qwen3-x", calls=[("c1", "run_shell_command",
                                                      {"command": "pytest -q"})],
                   text="running now", usage={"promptTokenCount": 100,
                                              "candidatesTokenCount": 30,
                                              "thoughtsTokenCount": 5,
                                              "totalTokenCount": 130,
                                              "cachedContentTokenCount": 40},
                   cwd="/w/repo"),
        _result(sid, "c1", "run_shell_command", output="Traceback... failed", status="error"),
        _assistant(sid, "a2", "qwen/qwen3-x", calls=[("c2", "read_file",
                                                      {"file_path": "/w/repo/x.py"})],
                   usage={"promptTokenCount": 200, "candidatesTokenCount": 60,
                          "thoughtsTokenCount": 10, "totalTokenCount": 260,
                          "cachedContentTokenCount": 0}, cwd="/w/repo"),
        _result(sid, "c2", "read_file", output="def f(): ..."),
        _notify(sid, "n1"),
    ]
    return _write(tmp_path, sid, recs)


# ---- the reader -----------------------------------------------------------------------------
def test_reader_converts_calls_to_the_claude_envelope(tmp_path):
    path = _session(tmp_path)
    out = str(tmp_path / "claude.jsonl")
    assert trace.qwen_session_to_claude(path, out) == 2
    calls = trace.load_trajectory(out)
    assert [c[0] for c in calls] == ["Bash", "Read"]
    assert calls[0][1]["command"] == "pytest -q"
    assert calls[1][1]["file_path"] == "/w/repo/x.py"
    assert calls[0][3] is False and calls[1][3] is True   # failed / succeeded
    assert calls[1][2] == "def f(): ..."


def test_prompt_count_counts_typed_prompts_not_notifications(tmp_path):
    path = _session(tmp_path)
    assert trace.qwen_prompt_count(path) == 1              # the notification is not a prompt
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user("s1", "and fix the docs", "u2", prompt_id="2")) + "\n")
    assert trace.qwen_prompt_count(path) == 2


def test_missing_or_empty_session_is_none(tmp_path):
    assert trace.qwen_session_to_claude(str(tmp_path / "none.jsonl"),
                                        str(tmp_path / "t")) is None
    p = _write(tmp_path, "e", [])
    assert trace.qwen_session_to_claude(p, str(tmp_path / "t")) is None


def test_tool_names_and_args_map_to_the_mechanisms(tmp_path):
    assert trace.qwen_tool("run_shell_command", {"command": "pytest", "description": "x"}) == \
        ("Bash", {"command": "pytest"})
    assert trace.qwen_tool("read_file", {"file_path": "/w/x", "offset": 4}) == \
        ("Read", {"file_path": "/w/x", "offset": 4})
    assert trace.qwen_tool("grep_search", {"pattern": "x", "path": "/w"}) == \
        ("Grep", {"pattern": "x", "path": "/w"})
    assert trace.qwen_tool("write_file", {"file_path": "/w/x", "content": "y"})[0] == "Write"
    assert trace.qwen_tool("strange_tool", {"a": 1}) == ("strange_tool", {"a": 1})


# ---- usage ----------------------------------------------------------------------------------
def test_usage_reads_fresh_input_and_candidates(tmp_path):
    path = _session(tmp_path)
    usage = trace.qwen_session_usage(path)
    # Two assistant entries; the first had 40 cached of 100 prompt tokens.
    assert usage["input_tokens"] == (100 - 40) + 200
    assert usage["cache_read_input_tokens"] == 40
    assert usage["output_tokens"] == 30 + 60
    assert usage["requests"] == 2
    assert usage["model"] == "qwen/qwen3-x"
    assert usage["cache_creation_input_tokens"] == 0
    effective, priced_by = accounting.effective(usage)
    assert priced_by in ("model", "fallback") and effective > 0


def test_usage_with_no_entries_is_not_guessed(tmp_path):
    p = _write(tmp_path, "s2", [_user("s2", "hi", "u1")])
    usage = trace.qwen_session_usage(p)
    assert usage["input_tokens"] is None
    assert accounting.effective(usage) == (None, "no_usage")


# ---- the host -------------------------------------------------------------------------------
def test_host_reads_a_qwen_session(tmp_path):
    host = hosts.QwenCode()
    assert host.name == "qwen"
    path = _session(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    trace_path = host.mechanism_trace(path, str(workdir))
    assert trace_path is not None
    calls = trace.load_trajectory(trace_path)
    assert [c[0] for c in calls] == ["Bash", "Read"]
    assert host.session_usage(path)["input_tokens"] == (100 - 40) + 200
    assert host.prompt_count(path) == 1
    # A missing transcript folds to nothing, never a zero-token ledger.
    assert host.mechanism_trace(str(tmp_path / "none.jsonl"), str(workdir)) is None


def test_host_headless_and_session_id_use_the_real_markers(monkeypatch):
    host = hosts.QwenCode()
    monkeypatch.delenv("QWEN_CODE", raising=False)
    assert host.headless({}, None) is True              # no marker: not an interactive session
    assert host.headless({"QWEN_CODE": "1"}, None) is False
    assert host.env_session_id({"QWEN_CODE_SESSION_ID": "abc"}) == "abc"
    assert host.env_session_id({"TE_SESSION_ID": "te-1"}) == "te-1"
    assert host.env_session_id({}) == ""
