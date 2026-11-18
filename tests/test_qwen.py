"""Qwen Code: the session reader, its host adapter, usage, the side call, and the micro-skill
loop."""
import json
import os
import subprocess
import time

import pytest

from tokeneconomizer import trace
from tokeneconomizer.plugin import accounting, hosts
from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir, state_root
from tokeneconomizer.plugin.store import Store
from tests.test_live_hooks import env, repo  # noqa: F401


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


# ---- tool pruning ---------------------------------------------------------------------------
def _qwen_catalog(env, tools):
    """The tool list a fresh session loads, as `te toolprune refresh-catalog` would record it.
    Qwen Code has no probe for this (its log names only the tools it called), so a repository's
    pruning can drop nothing until the catalog is known -- which is what these tests seed."""
    d = os.path.join(env["TOKENECONOMIZER_HOME"], "hosts", "qwen")
    os.makedirs(d, exist_ok=True)
    json.dump({"tools": tools}, open(os.path.join(d, "catalog.json"), "w"))


def _qwen_store(env, repo):
    return Store(repo_state_dir(repo_identity(repo, env), "qwen", env))


def _disabled(settings):
    return json.load(open(settings, encoding="utf-8"))["tools"]["disabled"]


def test_toolprune_writes_tools_disabled(env, repo):
    """The lever is `tools.disabled` in the repository's `.qwen/settings.json`: a name there is
    never registered, so its schema leaves the request -- unlike `permissions.deny`, which
    exempts MCP tools and leaves their schemas billed. The writer is the shared JSON-list one;
    only the key path is this host's, and the rule names are the aliases the documentation
    accepts, so a proposal crosses unchanged."""
    from tokeneconomizer.plugin import denylist

    settings = denylist.settings_path(repo, "qwen")
    os.makedirs(os.path.dirname(settings))
    # A rule the person wrote themselves is theirs: it must survive what the plugin takes back.
    json.dump({"tools": {"disabled": ["todo_write"]}}, open(settings, "w", encoding="utf-8"))

    store = _qwen_store(env, repo)
    assert denylist.supports("qwen") and denylist.unit("qwen") == "tool"
    assert denylist.apply(repo, ["WebSearch", "Bash"], store, host="qwen") == ["WebSearch", "Bash"]
    assert _disabled(settings) == ["todo_write", "WebSearch", "Bash"]
    assert denylist.apply(repo, ["Bash"], store, host="qwen") == []        # idempotent
    assert denylist.restore(repo, store, ["Bash"], host="qwen") == ["Bash"]
    assert _disabled(settings) == ["todo_write", "WebSearch"]
    assert denylist.restore(repo, store, host="qwen") == ["WebSearch"]      # all of ours
    assert _disabled(settings) == ["todo_write"]


def test_toolprune_created_settings_stay_out_of_git(env, repo):
    """A per-repository deny list is per-user: when the plugin creates the file it keeps it out
    of the commit that would otherwise carry it to everyone."""
    from tokeneconomizer.plugin import denylist

    assert denylist.apply(repo, ["Workflow"], _qwen_store(env, repo), host="qwen") == ["Workflow"]
    exclude = open(os.path.join(repo, ".git", "info", "exclude"), encoding="utf-8").read()
    assert "/.qwen/settings.json" in exclude


def test_toolprune_auto_applies_from_folded_qwen_sessions(env, repo, tmp_path):
    """The whole path on this host: five folded sessions plus a known catalog form a proposal,
    and under the default `auto` the fold writes it into `.qwen/settings.json`. Protected tools
    (the work surface, a person's AskUserQuestion, the web) are never in it."""
    from tokeneconomizer.plugin import denylist

    env = dict(env, TE_TOOLPRUNE="auto")
    _qwen_catalog(env, ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Workflow",
                        "CronCreate", "ShareOnboardingGuide", "WebFetch"])
    from test_live_hooks import call
    chats = tmp_path / "chats"
    chats.mkdir()
    for i in range(5):
        sid = f"p{i}"
        path = _write(chats, sid, [
            _user(sid, f"look at a.py {i}", f"u{i}", cwd=repo),
            _assistant(sid, f"a{i}", "qwen/qwen3-x", cwd=repo, usage=USAGE,
                       calls=[(f"c{i}r", "read_file", {"file_path": f"{repo}/a.py"}),
                              (f"c{i}b", "run_shell_command", {"command": "ls"})]),
            _result(sid, f"c{i}r", "read_file", output="x"),
            _result(sid, f"c{i}b", "run_shell_command", output="a.py"),
        ])
        base = {"session_id": sid, "cwd": repo, "transcript_path": path}
        call(env, dict(base, hook_event_name="SessionStart", source="startup"), "qwen")
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"), "qwen")

    settings = denylist.settings_path(repo, "qwen")
    assert sorted(_disabled(settings)) == ["CronCreate", "ShareOnboardingGuide", "Workflow"]
    assert _qwen_store(env, repo).read("untool.json")["proposal"] == []


def test_toolprune_refresh_catalog_degrades_for_qwen(env, repo, monkeypatch, capsys):
    """`te toolprune refresh-catalog` has no probe here: it says so and returns, rather than
    raising out of `toolcheck`, which knows only Claude and Copilot."""
    from tokeneconomizer.plugin import cli
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "qwen", "--cwd", repo, "toolprune", "refresh-catalog"]) == 0
    assert "no catalog probe" in capsys.readouterr().out


# ---- the micro-skill loop -------------------------------------------------------------------
USAGE = {"promptTokenCount": 1200, "candidatesTokenCount": 120, "thoughtsTokenCount": 10,
         "totalTokenCount": 1320, "cachedContentTokenCount": 900}


def _trap_session(root, sid, repo, i):
    """A session falling into the trap every session falls into: bare `pytest` is missing
    here, `python -m pytest` works, and each session edits a different module."""
    recs = [
        _user(sid, f"fix bug {i}", f"u{i}", cwd=repo),
        _assistant(sid, f"a{i}", "qwen/qwen3-x", cwd=repo, usage=USAGE,
                   calls=[(f"c{i}e", "edit", {"file_path": f"{repo}/pkg/mod{i}.py",
                                              "old_string": "a", "new_string": "b"}),
                          (f"c{i}f", "run_shell_command",
                           {"command": "pytest -q tests/test_a.py"}),
                          (f"c{i}w", "run_shell_command",
                           {"command": "python -m pytest -q tests/test_a.py"})]),
        _result(sid, f"c{i}e", "edit", output="ok"),
        _result(sid, f"c{i}f", "run_shell_command",
                error="bash: pytest: command not found", status="error"),
        _result(sid, f"c{i}w", "run_shell_command", output="1 passed in 0.1s"),
    ]
    return _write(root, sid, recs)


def _loop_env(state, ceiling):
    return {"TOKENECONOMIZER_HOME": str(state), "TE_FOLD_SYNC": "1",
            "TE_MICROSKILLS": "envfacts", "TE_TOOLPRUNE": "off",
            "GIT_CEILING_DIRECTORIES": str(ceiling), "PATH": os.environ.get("PATH", "")}


def _qwen_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "pyproject.toml").write_text("[project]\nname='w'\n")
    return str(repo)


def test_envfacts_learn_from_folded_qwen_sessions(tmp_path):
    """Six Qwen Code sessions folded through the hook teach the free extractor the same fact
    Claude's sessions teach: the trace `mechanism_trace` writes carries the mechanisms' own
    tool names, so the learning path needs no Qwen branch."""
    from test_live_hooks import call

    chats = tmp_path / "chats"
    chats.mkdir()
    repo = _qwen_repo(tmp_path)
    env = _loop_env(tmp_path / "home", tmp_path)

    starts = []
    for i in range(6):
        base = {"session_id": f"s{i}", "cwd": repo,
                "transcript_path": _trap_session(chats, f"s{i}", repo, i)}
        starts.append(call(env, dict(base, hook_event_name="SessionStart",
                                     source="startup"), "qwen"))
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"), "qwen")

    assert all(s == {} for s in starts[:4])       # the fact must clear its firing gate first
    ctx = starts[4]["hookSpecificOutput"]["additionalContext"]
    assert "`pytest …` fails here with command not found; `python -m pytest …` works." in ctx
    store = Store(repo_state_dir(repo_identity(repo, env), "qwen", env))
    assert store.read("injection.json")["profile"] == "envfacts"


def test_a_learned_fact_reaches_the_next_qwen_session(tmp_path):
    """The offline end to end: the sessions above teach the fact, and the next session start's
    own stdout carries it in the channel Qwen Code hands the model."""
    from test_live_hooks import call

    chats = tmp_path / "chats"
    chats.mkdir()
    repo = _qwen_repo(tmp_path)
    env = _loop_env(tmp_path / "home", tmp_path)
    for i in range(6):
        base = {"session_id": f"s{i}", "cwd": repo,
                "transcript_path": _trap_session(chats, f"s{i}", repo, i)}
        call(env, dict(base, hook_event_name="SessionStart", source="startup"), "qwen")
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"), "qwen")

    out = call(env, {"session_id": "s9", "cwd": repo, "hook_event_name": "SessionStart",
                    "source": "startup",
                    "transcript_path": _trap_session(chats, "s9", repo, 9)}, "qwen")
    assert "`pytest …` fails here with command not found; `python -m pytest …` works." in \
        out["hookSpecificOutput"]["additionalContext"]


def test_microskills_on_learns_the_free_facts_when_the_side_call_answers_nothing(tmp_path):
    """`on` is envfacts plus the paid proposer. Qwen Code has a side call (`qwen_call`), so the
    proposer does run here -- and, offline, answers nothing: the free facts are still learned, the
    tick is skipped rather than the fold aborted, and nothing is recorded as an error."""
    from test_live_hooks import call

    chats = tmp_path / "chats"
    chats.mkdir()
    repo = _qwen_repo(tmp_path)
    env = _loop_env(tmp_path / "home", tmp_path)
    env["TE_MICROSKILLS"] = "envfacts+distiller"

    for i in range(6):
        base = {"session_id": f"s{i}", "cwd": repo,
                "transcript_path": _trap_session(chats, f"s{i}", repo, i)}
        call(env, dict(base, hook_event_name="SessionStart", source="startup"), "qwen")
        call(env, dict(base, hook_event_name="SessionEnd", reason="other"), "qwen")

    store = Store(repo_state_dir(repo_identity(repo, env), "qwen", env))
    assert store.read("injection.json")["profile"] == "envfacts+distiller"
    assert "`python -m pytest" in store.read("injection.json")["rendered"]
    assert not os.path.exists(os.path.join(state_root(env), "errors.log"))


# ---- the side call ---------------------------------------------------------------------------
# A `qwen --output-format json` run as 0.28.0 writes it: one JSON array, an `init` record naming
# the model and the tools it registered, the assistant records, and a closing `result` whose
# `usage` counts the cached reads inside `input_tokens`.
QWEN_RUN = [
    {"type": "system", "subtype": "init", "session_id": "c1", "cwd": "/tmp/te-side-x",
     "tools": [], "mcp_servers": [], "model": "qwen/qwen3.8-max"},
    {"type": "assistant", "session_id": "c1",
     "message": {"role": "assistant", "model": "qwen/qwen3.8-max",
                 "content": [{"type": "text", "text": '{"stuck": false}'}]},
     "usage": {"input_tokens": 20, "output_tokens": 5}},
    {"type": "result", "subtype": "success", "session_id": "c1", "is_error": False,
     "result": '{"stuck": false}',
     "usage": {"input_tokens": 265, "output_tokens": 26, "cache_read_input_tokens": 100,
               "total_tokens": 291}},
]


def test_json_reply_reads_the_result_record_and_subtracts_the_cached_reads():
    text, usage = trace.qwen_json_reply(json.dumps(QWEN_RUN))
    assert text == '{"stuck": false}'
    assert usage == {"input_tokens": 165, "cache_read_input_tokens": 100,
                     "cache_creation_input_tokens": 0, "output_tokens": 26,
                     "model": "qwen/qwen3.8-max"}


@pytest.mark.parametrize("stdout", ["", "not json at all", "[]",
                                    json.dumps([{"type": "result", "result": "x"}])])
def test_json_reply_is_empty_for_a_run_that_printed_no_usage(stdout):
    """A run aborted at its tool-call limit prints nothing at all (exit 55, verified on 0.28.0),
    and one with no `usage` block prices as `no_usage` rather than as a guess."""
    text, usage = trace.qwen_json_reply(stdout)
    assert usage is None
    assert text in ("", "x")


@pytest.mark.real_sidecalls
def test_the_side_call_runs_with_no_tool_and_no_customization(monkeypatch):
    """The flags that make the call inert, and the order that keeps the prompt out of the
    `--exclude-tools` array: `--safe-mode` (no hooks, extensions, skills, MCP servers or QWEN.md),
    `--max-tool-calls 0` (the first tool call aborts it), every bundled tool excluded, and the
    prompt as a positional before them. `TE_SIDECALL=1` stops the nested run's own hooks."""
    from tokeneconomizer.plugin import sidecall
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"), cwd=kw.get("cwd"))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(QWEN_RUN), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    text, eff = sidecall.qwen_call("PROMPT")
    assert text == '{"stuck": false}' and eff == accounting.effective({
        "input_tokens": 165, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
        "output_tokens": 26, "model": "qwen/qwen3.8-max"})[0]
    cmd = seen["cmd"]
    assert cmd[:6] == ["qwen", "--safe-mode", "--max-tool-calls", "0", "--output-format", "json"]
    assert cmd[cmd.index("--system-prompt") + 1] == sidecall.QWEN_SYSTEM_PROMPT
    # The positional prompt comes before the `--exclude-tools` array, or one more tool name would
    # be read where the prompt belongs.
    assert cmd.index("PROMPT") < cmd.index("--exclude-tools")
    assert cmd[cmd.index("--exclude-tools") + 1:] == list(sidecall.QWEN_EXCLUDE_TOOLS)
    assert seen["env"]["TE_SIDECALL"] == "1"
    assert "te-side-" in os.path.basename(seen["cwd"])
    assert "--model" not in cmd                                  # the person's default model


@pytest.mark.real_sidecalls
def test_the_side_call_takes_a_model_and_notes_a_run_that_answered_nothing(monkeypatch,
                                                                           tmp_path):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path))
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 55, stdout="", stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    monkeypatch.setattr(sidecall, "program", lambda cli: [cli])
    assert sidecall.qwen_call("PROMPT", model="qwen/qwen3-coder") == ("", None)
    assert seen[0][seen[0].index("--model") + 1] == "qwen/qwen3-coder"
    # One line, and not the command: the prompt is session content on argv.
    log = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "sidecall qwen: no reply (exit 55)" in log and "PROMPT" not in log


def test_qwen_has_a_runner_and_a_notice_names_its_command():
    """The loop guard's judge and micro-skills' proposer get a runner here, so neither waits on
    a device-local model; a notice names the command that ran, and qwen takes the prompt as a
    positional (its `-p` is deprecated)."""
    from tokeneconomizer.plugin import config as live_config, sidecall
    assert "qwen" not in sidecall.NO_SIDE_CALLS
    assert sidecall.runner_for("qwen", live_config.LiveConfig()) is not None
    assert sidecall.call_name("qwen") == "qwen"


def test_the_live_tool_call_is_renamed_into_the_mechanisms_vocabulary():
    """A hook payload names the tool as Qwen Code registers it (`tool_name` is the tool's own
    `name`, 0.28.0). The mechanisms key on their own vocabulary, so the adapter renames it --
    without that, `canon_tool` sees no `bash`, the guard reads no command to take a test status
    from, and `claude_success_code` answers None for a success."""
    host = hosts.QwenCode()
    ok = {"hook_event_name": "PostToolUse", "tool_name": "run_shell_command",
          "tool_input": {"command": "pytest -q", "description": "run the tests"},
          "tool_response": "1 passed in 0.1s"}
    assert host.tool_call(ok) == ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s", 0)
    read = {"hook_event_name": "PostToolUse", "tool_name": "read_file",
            "tool_input": {"file_path": "/w/x.py", "offset": 4}, "tool_response": "src"}
    assert host.tool_call(read)[:2] == ("Read", {"file_path": "/w/x.py", "offset": 4})
    fail = {"hook_event_name": "PostToolUseFailure", "tool_name": "run_shell_command",
            "tool_input": {"command": "pytest -q"}, "error": "Exit code 1\nFAILED"}
    name, args, text, code = host.tool_call(fail)
    assert (name, args, code) == ("Bash", {"command": "pytest -q"}, 1) and "FAILED" in text


def test_the_guard_sees_a_qwen_shell_run_as_a_verification():
    """What the rename buys: a qwen `run_shell_command` running the tests counts as the turn's
    verification, so a turn that ends on a failing run is not 'ended green' and does not feed the
    envelope -- the proxy the guard's thresholds are learned from. The failure arrives as a
    `PostToolUseFailure`, as on Claude Code: a `PostToolUse` is a success, read as exit 0."""
    from tokeneconomizer import loopguard
    host = hosts.QwenCode()

    def turn(shell_payload):
        g = loopguard.TurnGuard(turn_key="t")
        edit = {"hook_event_name": "PostToolUse", "tool_name": "edit",
                "tool_input": {"file_path": "/r/a.py", "old_string": "x", "new_string": "y"},
                "tool_response": "ok"}
        for p in (edit, shell_payload):
            g.fold(*host.tool_call(p))
        return g.resolved_proxy()

    passed = {"hook_event_name": "PostToolUse", "tool_name": "run_shell_command",
              "tool_input": {"command": "pytest -q"}, "tool_response": "1 passed in 0.1s"}
    failed = {"hook_event_name": "PostToolUseFailure", "tool_name": "run_shell_command",
              "tool_input": {"command": "pytest -q"},
              "error": "Exit code 1\nFAILED tests/test_a.py::test_x - AssertionError"}
    assert turn(passed) is True
    assert turn(failed) is False


def test_a_refired_prompt_hook_keeps_the_working_turn(env, repo):
    """Qwen Code re-fires UserPromptSubmit inside one prompt-turn -- once per model request, so
    again after every tool result, with `prompt_id` unchanged (verified live 2026-11-18: the turn
    that ran the call was recorded as interrupted, and `envelope.json` was never written). Same
    key, same turn: it keeps accumulating, so it still ends green and feeds the envelope. Without
    one the guard may not act at all -- neither the judge nor the rule fallback redirects a
    repository whose usual turns it has not learned."""
    from test_live_hooks import _post, call
    base = {"session_id": "s", "transcript_path": "", "cwd": repo, "prompt_id": "s########0"}
    prompt = dict(base, hook_event_name="UserPromptSubmit", prompt="go")
    call(env, prompt, "qwen")
    call(env, _post(base, "read_file", {"file_path": f"{repo}/a.py"}, "ok"), "qwen")
    call(env, prompt, "qwen")                      # the re-fire, after the tool result
    call(env, dict(base, hook_event_name="Stop", stop_hook_active=False), "qwen")

    store = _qwen_store(env, repo)
    turns = store.read(os.path.join("sessions", "s.json"))["turns"]
    assert [(t["tool_calls"], t["resolved_proxy"]) for t in turns] == [(1, True)]
    assert store.read("envelope.json")["resolved_turns"] == [1]


def test_a_new_prompt_still_closes_a_turn_whose_stop_never_came(env, repo):
    """The other side of the re-fire rule: a DIFFERENT `prompt_id` is a new prompt, so a turn left
    open by an interrupted run is still closed as unknown-outcome and does not feed the envelope."""
    from test_live_hooks import _post, call
    base = {"session_id": "s", "transcript_path": "", "cwd": repo}
    call(env, dict(base, prompt_id="s########0", hook_event_name="UserPromptSubmit",
                   prompt="go"), "qwen")
    call(env, _post(dict(base, prompt_id="s########0"), "read_file",
                    {"file_path": f"{repo}/a.py"}, "ok"), "qwen")
    call(env, dict(base, prompt_id="s########1", hook_event_name="UserPromptSubmit",
                   prompt="next"), "qwen")
    call(env, dict(base, prompt_id="s########1", hook_event_name="Stop",
                   stop_hook_active=False), "qwen")

    store = _qwen_store(env, repo)
    turns = store.read(os.path.join("sessions", "s.json"))["turns"]
    assert [(t["tool_calls"], t["resolved_proxy"]) for t in turns] == [(1, False), (0, False)]
    assert store.read("envelope.json") is None


# ---- packaging ------------------------------------------------------------------------------
def test_the_qwen_manifest_declares_every_event_it_is_wired_for():
    """The extension is the manifest and nothing else: Qwen Code reads `qwen-extension.json` at
    the extension root, and the six events are the ones the controller answers."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = json.load(open(os.path.join(repo_root, "plugin", "qwen-extension.json"), encoding="utf-8"))
    assert d["name"] == "tokeneconomizer"
    assert list(d["hooks"]) == ["SessionStart", "UserPromptSubmit", "PostToolUse",
                                "PostToolUseFailure", "Stop", "SessionEnd"]
    for event, groups in d["hooks"].items():
        for hook in groups[0]["hooks"]:
            assert "--host qwen" in hook["command"], event


def test_the_qwen_hook_commands_carry_no_quotes():
    """Qwen Code runs a hook as `spawn(command, [], {shell: true})`, which on Windows reaches
    cmd.exe as one argv element, and libuv escapes the inner `"` to `\\"` on the way. cmd.exe has
    no `\\"` rule -- it takes the backslash-quote as part of the program name -- so every quoted
    launcher fails with `"...\te-hook.cmd" is not recognized`, the hook answers nothing, and the
    plugin never runs. Verified live 2026-11-18: with the quotes the debug log reads
    `Hook ... ended for event SessionStart: failed` for all six events and no state directory is
    created; without them it reads `: success` and the session is recorded.

    POSIX is the other half: there the same string goes to `/bin/sh -c` unescaped, so quotes work
    and an unquoted path would break on a home directory with a space. The unquoted form is the
    one that runs on both, and it costs a person whose install path contains a space -- which on
    Windows was already broken -- so it is the only shape that works at all."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = json.load(open(os.path.join(repo_root, "plugin", "qwen-extension.json"), encoding="utf-8"))
    for event, groups in d["hooks"].items():
        for hook in groups[0]["hooks"]:
            assert '"' not in hook["command"], event


def test_qwen_plugin_packaging(tmp_path):
    """The bundle Qwen Code can install. It copies the extension directory, so an installed copy
    of `plugin/` alone carries `lib/tokeneconomizer` as it is in the checkout -- where git cannot
    make a symlink it writes a plain file holding the relative path, and the hook then finds no
    core and answers `{}`. The bundle puts the real package there instead."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_plugin_qwen", os.path.join(repo_root, "scripts", "build_plugin.py"))
    build_plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_plugin)
    out = tmp_path / "qwen-bundle"
    plugin = build_plugin.build_qwen(str(out))

    # The manifest is at the root, which is where Qwen Code looks for it, and the package is a
    # real copy inside lib/, not the checkout's stub.
    assert plugin == str(out / "tokeneconomizer")
    assert json.load(open(os.path.join(plugin, "qwen-extension.json"),
                          encoding="utf-8"))["name"] == "tokeneconomizer"
    lib_pkg = os.path.join(plugin, "lib", "tokeneconomizer")
    assert os.path.isdir(lib_pkg)
    assert os.path.isfile(os.path.join(lib_pkg, "plugin", "hook.py"))
    assert os.path.isfile(os.path.join(plugin, "scripts", "te-hook.cmd"))
    assert not list(__import__("pathlib").Path(plugin).rglob("__pycache__"))

    # Only what Qwen Code loads: another agent's manifest would describe the same directory as a
    # plugin of that agent, and Qwen Code converts several of those layouts on its own. Its hooks
    # are already inline in the manifest, so `hooks/` would risk registering them twice.
    for foreign in (".claude-plugin", ".codex-plugin", ".cursor-plugin", ".github",
                    ".factory-plugin", ".antigravity-plugin", "hooks", "hooks.json",
                    "codex-skills", "hermes", "opencode", "pi", "slash-commands",
                    "package.json"):
        assert not os.path.exists(os.path.join(plugin, foreign)), foreign

    # Qwen Code installs a local path, so no marketplace file is written around it.
    assert os.listdir(str(out)) == ["tokeneconomizer"]

    # The proof the bundle exists for: the launcher finds the core through its own lib/, with
    # nothing above it. Loaded from the bundle's path, so __file__ resolves inside the bundle.
    spec = importlib.util.spec_from_file_location(
        "te_bootstrap_bundled", os.path.join(plugin, "scripts", "te_bootstrap.py"))
    te_bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(te_bootstrap)
    assert te_bootstrap.core_path() == os.path.join(plugin, "lib")


def test_build_plugin_chooses_one_layout(tmp_path):
    """`--qwen` and `--droid` write different directories, so naming both is refused rather than
    silently writing one of them; with no flag the default stays the Claude marketplace."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_plugin_main", os.path.join(repo_root, "scripts", "build_plugin.py"))
    build_plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_plugin)
    try:
        build_plugin.main(["--droid", "--qwen", "--out", str(tmp_path / "x")])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("--droid with --qwen was accepted")
    assert build_plugin.main(["--qwen", "--out", str(tmp_path / "q")]) == 0
    assert os.path.isdir(os.path.join(str(tmp_path / "q"), "tokeneconomizer", "lib",
                                      "tokeneconomizer"))


# ---- the history import ---------------------------------------------------------------------
def test_the_projects_dir_follows_qwens_runtime_base(tmp_path):
    """`QWEN_RUNTIME_DIR` wins, then `QWEN_HOME`, then the home directory's `.qwen` -- the
    precedence of Qwen Code's own `Storage.getRuntimeBaseDir()` and `getGlobalQwenDir()`."""
    both = {"QWEN_RUNTIME_DIR": str(tmp_path / "rt"), "QWEN_HOME": str(tmp_path / "home")}
    assert trace.qwen_projects_dir(both) == os.path.join(str(tmp_path / "rt"), "projects")
    assert trace.qwen_projects_dir({"QWEN_HOME": str(tmp_path / "home")}) == \
        os.path.join(str(tmp_path / "home"), "projects")
    home_key = "USERPROFILE" if os.name == "nt" else "HOME"
    assert trace.qwen_projects_dir({home_key: str(tmp_path)}) == \
        os.path.join(str(tmp_path), ".qwen", "projects")


def test_the_project_folder_is_qwens_sanitize_cwd():
    """Qwen Code's `sanitizeCwd`: lower-cased on Windows only, then every character that is not a
    letter or a digit becomes `-` -- so the `:` and the separator of a Windows path give the
    double dash its real slugs carry (`c--users-max-desktop-...`). Claude Code's rule, which this
    is not, leaves the case alone."""
    assert trace.qwen_project_folder("/home/u/widget") == "-home-u-widget"
    assert trace.qwen_project_folder(r"C:\Users\Max\Widget") == \
        ("c--users-max-widget" if os.name == "nt" else "C--Users-Max-Widget")


def test_history_logs_read_a_projects_chats_directory(tmp_path):
    """Only `<folder>/chats/*.jsonl`: not the `*.ledger.jsonl` a session may have beside it, not
    Qwen Code's `chats/archive/` (its own scanner is not recursive either), and not a project
    folder that has no `chats/` at all."""
    projects = tmp_path / "projects"
    chats = projects / "c--r" / "chats"
    chats.mkdir(parents=True)
    (chats / "aaaa.jsonl").write_text("{}\n", encoding="utf-8")
    (chats / "aaaa.ledger.jsonl").write_text("{}\n", encoding="utf-8")
    (chats / "notes.txt").write_text("x", encoding="utf-8")
    (chats / "archive").mkdir()
    (chats / "archive" / "bbbb.jsonl").write_text("{}\n", encoding="utf-8")
    (projects / "c--other").mkdir()
    rows = trace.qwen_history_logs(str(projects))
    assert [(r[0], os.path.basename(r[1])) for r in rows] == [("c--r", "aaaa.jsonl")]
    assert rows[0][2] > 0 and rows[0][3] > 0        # its mtime and size are real


def test_prompt_turns_cut_at_the_prompt_and_keep_the_answer(tmp_path):
    """A turn starts at the prompt record, because only the `user` record the person typed
    carries a `promptId` -- the assistant and tool_result records of its turn carry none. The
    assistant's own text survives the conversion and its reasoning (`thought`) does not, which is
    what tells an answered turn from one the log cut off mid-flight. A task notification Qwen
    Code adds itself is no prompt and opens no turn."""
    sid = "s1"
    path = _write(tmp_path, sid, [
        _user(sid, "first", "u1", cwd="/w/repo", prompt_id=f"{sid}########0"),
        _assistant(sid, "a1", "qwen/qwen3-x", thought="let me look", cwd="/w/repo",
                   calls=[("c1", "run_shell_command", {"command": "pytest -q"})]),
        _result(sid, "c1", "run_shell_command", output="1 failed in 0.1s", status="error"),
        _assistant(sid, "a2", "qwen/qwen3-x", text="it fails", cwd="/w/repo"),
        _notify(sid, "n1"),
        _user(sid, "second", "u2", cwd="/w/repo", prompt_id=f"{sid}########1"),
        _assistant(sid, "a3", "qwen/qwen3-x", text="done", cwd="/w/repo"),
    ])
    turns = list(trace.qwen_prompt_turns(path))
    assert len(turns) == 2
    first = [json.loads(ln) for ln in turns[0]]
    assert [r["type"] for r in first] == ["user", "assistant", "user", "assistant"]
    assert first[0]["message"]["content"] == "first"
    assert "let me look" not in "".join(turns[0])       # reasoning is not an answer
    assert first[3]["message"]["content"] == [{"type": "text", "text": "it fails"}]
    assert [json.loads(ln)["message"]["content"] for ln in turns[1]] == \
        ["second", [{"type": "text", "text": "done"}]]


def test_the_host_reads_an_earlier_turns_calls_and_whether_it_was_answered(tmp_path):
    """What the envelope replay asks of the adapter: the turn's calls in the mechanisms'
    vocabulary with their outcome, and whether the turn was answered. Both come from the shared
    reader, because `prompt_turns` hands it Claude-shaped lines."""
    host = hosts.QwenCode()
    turns = list(host.prompt_turns(_session(tmp_path)))
    assert len(turns) == 1
    assert [(n, a, ok) for n, a, _t, ok, _c in host.turn_calls(turns[0])] == [
        ("Bash", {"command": "pytest -q"}, False),
        ("Read", {"file_path": "/w/repo/x.py"}, True)]
    # that session ends on a call with no answer after it, as one killed mid-turn does
    assert host.turn_answered(turns[0]) is False
    assert host.turn_interrupted(turns[0]) is False


def test_history_lists_the_sessions_of_a_qwen_projects_dir(tmp_path):
    """`history` walks the projects directory the environment names and takes a session's id from
    its file name, as the other hosts' do."""
    home = tmp_path / "qwenhome"
    chats = home / "projects" / "c--w-repo" / "chats"
    chats.mkdir(parents=True)
    (chats / "aaaa-bbbb.jsonl").write_text("{}\n", encoding="utf-8")
    host = hosts.QwenCode()
    assert host.keeps_history is True
    rows = host.history({"QWEN_HOME": str(home)})
    assert [(r["session_id"], r["folder"]) for r in rows] == [("aaaa-bbbb", "c--w-repo")]


def test_folder_matches_uses_the_qwen_slug_rule(tmp_path):
    """Asked only where a log's recorded cwd is gone: the folder must be this repository's root
    under Qwen Code's slug rule, and nothing else."""
    host = hosts.QwenCode()
    root = str(tmp_path / "Widget")
    os.makedirs(root)
    assert host.folder_matches(root, trace.qwen_project_folder(root)) is True
    assert host.folder_matches(root, "c--somewhere-else") is False


def test_history_import_folds_an_earlier_qwen_session(env, repo, tmp_path, monkeypatch):
    """The import end to end: a session logged before the plugin was installed is read from Qwen
    Code's own projects directory, folded once and marked `imported`, priced by its own model,
    and its ended-green turn seeds the envelope the live guard needs before it may act."""
    from tokeneconomizer.plugin import cli
    home = tmp_path / "qwenhome"
    chats = home / "projects" / trace.qwen_project_folder(repo) / "chats"
    chats.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    path = _write(chats, sid, [
        _user(sid, "fix the build", "u1", cwd=repo, prompt_id=f"{sid}########0"),
        _assistant(sid, "a1", "qwen/qwen3.8-max", cwd=repo, usage=USAGE, text="running it",
                   calls=[("c1", "run_shell_command", {"command": "pytest -q"})]),
        _result(sid, "c1", "run_shell_command", output="1 passed in 0.10s"),
        _assistant(sid, "a2", "qwen/qwen3.8-max", cwd=repo, usage=USAGE, text="it is green"),
    ])
    quiet = time.time() - 3600          # over, but well inside `history_days`
    os.utime(path, (quiet, quiet))
    for key, value in dict(env, QWEN_HOME=str(home)).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["--host", "qwen", "--cwd", repo, "history", "import"]) == 0

    store = _qwen_store(env, repo)
    with open(store.path("ledger.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(ln) for ln in fh if ln.strip()]
    assert [r["session_id"] for r in rows] == [sid]
    assert rows[0]["imported"] is True
    assert rows[0]["model"] == "qwen/qwen3.8-max" and rows[0]["priced_by"] == "model"
    assert rows[0]["effective_tokens"] > 0
    assert (store.read("folded.json") or {}).get(sid, {}).get("imported") is True
    assert store.read("envelope.json")["resolved_turns"] == [1]


def test_the_import_line_names_qwen_and_not_claude():
    """`te status` says whose history it read. `HOST_LABEL` falls back to «Claude Code» for a host
    it does not name, so without this host's entry a qwen import reported Claude Code's logs --
    caught live 2026-11-18, where the import itself worked and only the line lied."""
    from tokeneconomizer.plugin import history
    assert history.HOST_LABEL["qwen"] == "Qwen Code"
    assert history.describe({"state": "done", "imported": 1}, "qwen") == \
        "read 1 earlier session(s) from Qwen Code's history"
