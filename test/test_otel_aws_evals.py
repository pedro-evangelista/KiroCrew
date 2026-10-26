"""Contract tests for the optional AWS-evals chat/workflow exporters."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = ROOT / "scripts/otel/aws-evals"
HOOK_PATH = ADAPTER_ROOT / "hook-runner-v2/otel-crew-hook-v2.py"
INSTALLER_PATH = HOOK_PATH.with_name("install-v2.py")
CONVERTER_PATH = ADAPTER_ROOT / "acp-tap/acp-to-spans.py"
SHIM_BUILDER_PATH = ADAPTER_ROOT / "acp-tap/make-shim.sh"


def load_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_OTEL_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("KIRO_OTEL_STATE_DIR", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location("aws_eval_hook_under_test", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_converter(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_OTEL_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("KIRO_OTEL_STATE_DIR", str(tmp_path / "state"))
    return load_module(CONVERTER_PATH, "aws_eval_acp_converter_under_test")


def attribute(span: dict, name: str):
    return next(
        (
            next(iter(entry["value"].values()), None)
            for entry in span.get("attributes", [])
            if entry.get("key") == name
        ),
        None,
    )


def test_hook_uses_root_only_session_key(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt-1", "session_key": "chat-root"},
        10,
    )

    assert attribute(spans[0], "session.id") == "chat-root"


def test_hook_selects_agentcore_parent_and_keeps_both_session_keys(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    parent = "dashboard:agentcore-eval-root-1"

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-2",
            "session_key": "agent-slot-1",
            "parent_session_key": parent,
        },
        20,
    )

    assert attribute(spans[0], "session.id") == parent
    assert attribute(spans[0], "crew.session_key") == "agent-slot-1"
    assert attribute(spans[0], "crew.parent_session_key") == parent


def test_hook_ignores_non_root_parent_when_own_session_key_exists(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-3",
            "session_key": "own-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        30,
    )

    assert attribute(spans[0], "session.id") == "own-chat"


def test_hook_falls_back_to_legacy_session_id(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-4",
            "session_id": "legacy-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        40,
    )

    assert attribute(spans[0], "session.id") == "legacy-chat"


def test_hook_correlation_key_retains_own_and_parent_session_identity(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    base = {"tool_call_id": "call-1", "workflow_run_id": "run-1", "agent_id": "agent-1"}

    original = hook.correlation_key(
        {**base, "session_key": "own-a", "parent_session_key": "dashboard:agentcore-eval-a"}
    )
    other_own = hook.correlation_key(
        {**base, "session_key": "own-b", "parent_session_key": "dashboard:agentcore-eval-a"}
    )
    other_parent = hook.correlation_key(
        {**base, "session_key": "own-a", "parent_session_key": "dashboard:agentcore-eval-b"}
    )

    assert original != other_own
    assert original != other_parent


def test_hook_ignores_duplicate_event_delivery(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt", "session_key": "chat-1"},
        10,
    )
    pre = {
        "hook_event_name": "PreToolUse",
        "event_id": "pre-1",
        "tool_call_id": "call-1",
        "session_key": "chat-1",
        "tool_name": "ReadFile",
    }
    hook.handle(pre, 20)
    hook.handle(pre, 21)

    spans = hook.handle(
        {
            "hook_event_name": "PostToolUse",
            "event_id": "post-1",
            "tool_call_id": "call-1",
            "session_key": "chat-1",
            "tool_name": "ReadFile",
            "tool_response": {"output": "done"},
        },
        30,
    )

    assert len(spans) == 1
    assert attribute(spans[0], "crew.pre_events") == "1"


def test_hook_quarantines_tool_event_without_call_id(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    assert (
        hook.handle(
            {"hook_event_name": "PreToolUse", "event_id": "missing-call", "tool_name": "ReadFile"},
            50,
        )
        == []
    )
    errors = [
        json.loads(line)
        for line in (tmp_path / "state" / "correlation-errors.jsonl").read_text().splitlines()
    ]

    assert errors[0]["missing_ids"] == "tool_call_id"


def test_hook_does_not_infer_tool_pair_from_time_name_or_input(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.ORPHANS = "emit"
    common = {
        "session_key": "chat-1",
        "tool_name": "RunCommand",
        "tool_input": {"command": "same"},
        "timestamp": "same-time",
    }
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre", "tool_call_id": "call-a"},
        60,
    )

    spans = hook.handle(
        {
            **common,
            "hook_event_name": "PostToolUse",
            "event_id": "post",
            "tool_call_id": "call-b",
            "tool_response": {"output": "not paired"},
        },
        60,
    )
    errors = [
        json.loads(line)
        for line in (tmp_path / "state" / "correlation-errors.jsonl").read_text().splitlines()
    ]

    assert spans == []
    assert errors[0]["missing_ids"] == "matching_pre_tool_event"


def test_hook_bounds_pending_tool_state(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_PENDING_CALLS = 1
    common = {"session_key": "chat-1", "tool_name": "ReadFile"}
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre-a", "tool_call_id": "call-a"},
        70,
    )
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre-b", "tool_call_id": "call-b"},
        71,
    )

    assert [item["tool_call_id"] for item in hook.load_state()["pending"]] == ["call-b"]


def test_workflow_turn_joins_by_run_agent_and_acp_session(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    stamp = "2026-10-21T10:00:00+00:00"
    run = {
        "run_id": "run-1",
        "session_key": "dashboard:agentcore-eval-root-1",
        "name": "research",
        "args": {},
        "result": "done",
        "events": [
            {"type": "run_started", "ts": stamp, "data": {}},
            {
                "type": "agent_started",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "label": "worker"},
            },
            {
                "type": "agent_session_bound",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-1"},
            },
            {"type": "agent_finished", "ts": stamp, "data": {"agent_id": "agent-1", "ok": True}},
            {"type": "run_finished", "ts": stamp, "data": {}},
        ],
    }
    turns = [
        {
            "sid": "acp-1",
            "workflow_run_id": "run-1",
            "agent_id": "agent-1",
            "start": 1,
            "end": 2,
            "frames": [
                {
                    "ids": {"toolCallId": "matched"},
                    "update": "tool_call",
                    "ts_ns": "1",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
        {
            "sid": "acp-other",
            "workflow_run_id": "run-1",
            "agent_id": "agent-1",
            "start": 3,
            "end": 4,
            "frames": [
                {
                    "ids": {"toolCallId": "wrong-session"},
                    "update": "tool_call",
                    "ts_ns": "3",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
        {
            "sid": "acp-1",
            "workflow_run_id": "run-1",
            "agent_id": "agent-other",
            "start": 5,
            "end": 6,
            "frames": [
                {
                    "ids": {"toolCallId": "wrong-agent"},
                    "update": "tool_call",
                    "ts_ns": "5",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
    ]

    spans = converter.workflow_spans(run, turns, set(), [], set(), [])
    tool_ids = {
        attribute(span, "gen_ai.tool.call.id")
        for span in spans
        if span["name"].startswith("execute_tool ")
    }

    assert tool_ids == {"matched"}


def test_workflow_turn_does_not_join_by_timestamp_name_or_input(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    stamp = "2026-10-21T10:00:00+00:00"
    run = {
        "run_id": "run-1",
        "session_key": "chat-1",
        "name": "same-name",
        "args": {},
        "events": [
            {"type": "run_started", "ts": stamp, "data": {}},
            {
                "type": "agent_started",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "label": "same-name"},
            },
            {
                "type": "agent_session_bound",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-1"},
            },
            {"type": "run_finished", "ts": stamp, "data": {}},
        ],
    }
    unrelated = {
        "sid": "acp-1",
        "workflow_run_id": "other-run",
        "agent_id": "agent-1",
        "start": 1,
        "end": 2,
        "frames": [
            {
                "ids": {"toolCallId": "unrelated"},
                "update": "tool_call",
                "ts_ns": "1",
                "content": {"name": "same-name", "input": "same"},
            }
        ],
        "usage": {},
    }

    spans = converter.workflow_spans(run, [unrelated], set(), [], set(), [])

    assert not any(span["name"].startswith("execute_tool ") for span in spans)


def test_acp_session_resolution_uses_agentcore_parent_root(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {
            "sid": "acp-1",
            "session_key": "agent-slot-1",
            "parent_session_key": "dashboard:agentcore-eval-root-1",
        },
        {"acp-1": "mapped-chat"},
    )

    assert session == "dashboard:agentcore-eval-root-1"


def test_acp_session_resolution_ignores_non_root_parent(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {
            "sid": "acp-1",
            "session_key": "own-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        {"acp-1": "mapped-chat"},
    )

    assert session == "own-chat"


def test_acp_session_resolution_uses_legacy_id_before_session_map(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {"sid": "acp-1", "session_id": "legacy-chat"},
        {"acp-1": "mapped-chat"},
    )

    assert session == "legacy-chat"


def test_acp_chat_span_keeps_own_and_parent_session_attributes(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    turn = {
        "sid": "acp-1",
        "session_key": "agent-slot-1",
        "parent_session_key": "dashboard:agentcore-eval-root-1",
        "start": 1,
        "end": 2,
        "frames": [],
        "usage": {"inputTokens": 1},
    }

    spans = converter.turn_spans(
        turn,
        converter.resolve_session_key(turn),
        "a" * 32,
        None,
    )
    chat = next(span for span in spans if span["name"] == "chat")

    assert attribute(chat, "session.id") == "dashboard:agentcore-eval-root-1"
    assert attribute(chat, "crew.session_key") == "agent-slot-1"
    assert attribute(chat, "crew.parent_session_key") == "dashboard:agentcore-eval-root-1"


def test_exporter_redacts_and_truncates_content():
    hook = load_module(HOOK_PATH, "aws_eval_redaction_under_test")
    value, truncated = hook.safe_text("password=hunter2 " + ("x" * 9000))

    assert value.startswith("password=[REDACTED]")
    assert len(value) == 8192
    assert truncated is True


def test_hook_export_failure_is_fail_open(monkeypatch, tmp_path):
    hook = load_module(HOOK_PATH, "aws_eval_export_failure_under_test")
    monkeypatch.setattr(hook, "DRY_RUN_FILE", None)
    monkeypatch.setattr(hook, "TRACE_ROOT", tmp_path)
    monkeypatch.setattr(
        hook.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    hook.export([hook.span("a" * 32, "b" * 16, "test", 1, 2, [])])


def test_installer_is_import_safe_and_requires_explicit_action(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = '{"hooks":[{"id":"custom","enabled":true}]}'
    hooks_path.write_text(original, encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_under_test")

    try:
        installer.main([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("installer without an action should show usage")

    assert hooks_path.read_text(encoding="utf-8") == original


def test_installer_backups_and_restores_hook_configuration(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = {
        "hooks": [
            {"id": "otel-legacy", "enabled": True, "command": "legacy"},
            {"id": "custom", "enabled": True, "command": "custom"},
        ]
    }
    hooks_path.write_text(json.dumps(original), encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_actions_under_test")

    installer.main(["enable", "--hooks", str(hooks_path)])
    enabled = json.loads(hooks_path.read_text(encoding="utf-8"))
    enable_backups = list(tmp_path.glob("hooks.json.before-v2-enable-*.bak"))
    assert len(enable_backups) == 1
    assert json.loads(enable_backups[0].read_text(encoding="utf-8")) == original
    assert next(h for h in enabled["hooks"] if h["id"] == "otel-legacy")["enabled"] is False
    assert any(h["id"] == "otelv2-userpromptsubmit" for h in enabled["hooks"])

    installer.main(["disable", "--hooks", str(hooks_path)])
    restored = json.loads(hooks_path.read_text(encoding="utf-8"))
    disable_backups = list(tmp_path.glob("hooks.json.before-v2-disable-*.bak"))
    assert len(disable_backups) == 1
    assert json.loads(disable_backups[0].read_text(encoding="utf-8")) == enabled
    assert restored["hooks"] == original["hooks"]


def run_packaged_tap(tmp_path, *, env_overrides=None, raw_input="tool payload"):
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the optional ACP tap"
    shim = tmp_path / "shim" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    build_env = os.environ.copy()
    build_env["HOME"] = str(tmp_path / "home")
    build_env["PATH"] = os.pathsep.join(
        (str(Path(node).parent), "/usr/bin", "/bin", build_env.get("PATH", ""))
    )
    (tmp_path / "home").mkdir()
    subprocess.run(
        ["/bin/sh", str(SHIM_BUILDER_PATH), str(shim)],
        cwd=tmp_path,
        env=build_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    fake_adapter = tmp_path / "fake-acp-adapter.py"
    fake_adapter.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.buffer.read()\n"
        "for update in [\n"
        " {'sessionUpdate':'tool_call','toolCallId':'call-1','rawInput':os.environ.get('ACP_FAKE_INPUT', 'tool payload'),"
        "  'rawOutput':'password=adapter-secret','title':'run'},\n"
        " {'sessionUpdate':'agent_message_chunk','content':{'text':'assistant text'}},\n"
        "]: print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':"
        "{'sessionId':'acp-1','update':update}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_adapter.chmod(0o700)
    output = tmp_path / "frames.jsonl"
    prompts = tmp_path / "prompts.jsonl"
    env = build_env.copy()
    env.update(
        {
            "ACP_TAP_REAL_BIN": str(fake_adapter),
            "ACP_TAP_OUT": str(output),
            "ACP_TAP_PROMPTS_OUT": str(prompts),
            "ACP_FAKE_INPUT": raw_input,
        }
    )
    if env_overrides:
        env.update(env_overrides)
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/prompt",
                "params": {
                    "sessionId": "acp-1",
                    "prompt": [
                        {
                            "text": "[CURRENT USER REQUEST -- respond to this]\npassword=prompt-secret"
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    command = [node, str(shim)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(request, timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    frames = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    prompt_rows = (
        [json.loads(line) for line in prompts.read_text(encoding="utf-8").splitlines()]
        if prompts.exists()
        else []
    )
    return frames, prompt_rows, result


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_packaged_shim_captures_redacted_bounded_content_by_default(tmp_path):
    frames, prompts, _ = run_packaged_tap(
        tmp_path,
        raw_input="password=tool-secret " + ("x" * 9000),
    )
    tool_content = next(frame["content"] for frame in frames if frame.get("update") == "tool_call")
    prompt_content = prompts[0]["text"]

    assert len(tool_content["input"]) == 8192
    assert "password=[REDACTED]" in tool_content["input"]
    assert "adapter-secret" not in tool_content["output"]
    assert "password=[REDACTED]" in tool_content["output"]
    assert prompt_content == "password=[REDACTED]"
    assert "tool-secret" not in json.dumps(frames)


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_content_opt_out_does_not_disable_prompt_capture(tmp_path):
    frames, prompts, _ = run_packaged_tap(tmp_path, env_overrides={"ACP_TAP_CONTENT": "0"})

    assert all("content" not in frame for frame in frames if frame.get("update"))
    assert prompts[0]["text"] == "password=[REDACTED]"


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_prompt_opt_out_does_not_disable_content_capture(tmp_path):
    frames, prompts, _ = run_packaged_tap(tmp_path, env_overrides={"ACP_TAP_PROMPTS": "0"})

    assert any(frame.get("content", {}).get("input") for frame in frames)
    assert prompts == []
