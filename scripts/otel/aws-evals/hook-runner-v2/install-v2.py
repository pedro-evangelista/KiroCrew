#!/usr/bin/env python3
"""Explicitly enable or disable the optional KiroCrew OTLP hook adapter.

  install-v2.py enable  [--hooks FILE] [--service NAME]
  install-v2.py disable [--hooks FILE]

enable : backs up the file, disables the old otel-* runner entries (kept, not deleted),
         adds five otelv2-* entries whose command ends with '; exit 0'.
disable: backs up the file, removes the otelv2-* entries, re-enables the old otel-* ones.
Other hooks (for example the payload recorder) are left alone.
"""

import argparse
import json
import shlex
import shutil
import time
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=["enable", "disable"])
    result.add_argument("--hooks", default=str(Path.home() / ".kiro/crew/hooks.json"))
    result.add_argument("--service", default="kiro-linux-v2")
    result.add_argument(
        "--acp",
        action="store_true",
        help="also run the ACP chat/workflow converter after each Stop",
    )
    result.add_argument("--endpoint", default="http://127.0.0.1:14318")
    result.add_argument(
        "--runs-dir",
        default="/var/lib/otelcol/workflow-runs",
        help="KiroCrew's configured workflow run store, read by the ACP converter",
    )
    return result


def backup_path(path: Path, action: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.before-v2-{action}-{stamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.before-v2-{action}-{stamp}-{suffix}.bak")
        suffix += 1
    return candidate


def build_command(args: argparse.Namespace, runner: Path) -> str:
    assignments = [
        f"KIRO_OTEL_ENDPOINT={shlex.quote(args.endpoint)}",
        f"KIRO_OTEL_SERVICE_NAME={shlex.quote(args.service)}",
    ]
    if args.acp:
        converter = runner.parent.parent / "acp-tap" / "acp-to-spans.py"
        assignments.extend(
            [
                "KIRO_OTEL_HOOK_TOOLS=off",
                f"KIRO_OTEL_ACP_CONVERTER={shlex.quote(str(converter))}",
                f"KIRO_OTEL_WORKFLOW_RUNS_DIR={shlex.quote(args.runs_dir)}",
            ]
        )
    return " ".join(assignments) + f" python3 {shlex.quote(str(runner))}; exit 0"


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    path = Path(args.hooks)
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, list):
        raise ValueError(f"hooks.json must contain a hooks list: {path}")

    backup = backup_path(path, args.action)
    shutil.copy2(path, backup)
    print("backup:", backup)

    hooks[:] = [hook for hook in hooks if not str(hook.get("id", "")).startswith("otelv2-")]
    if args.action == "enable":
        runner = Path(__file__).resolve().with_name("otel-crew-hook-v2.py")
        command = build_command(args, runner)
        for hook in hooks:
            if str(hook.get("id", "")).startswith("otel-"):
                hook["enabled"] = False
        for event, matcher in [
            ("AgentSpawn", ""),
            ("UserPromptSubmit", ""),
            ("PreToolUse", "*"),
            ("PostToolUse", "*"),
            ("Stop", ""),
        ]:
            hooks.append(
                {
                    "id": f"otelv2-{event.lower()}",
                    "name": f"OTEL v2 {event}",
                    "event": event,
                    "matcher": matcher,
                    "matcher_mode": "glob",
                    "command": command,
                    "skills": [],
                    "timeout": 30,
                    "enabled": True,
                }
            )
    else:
        for hook in hooks:
            if str(hook.get("id", "")).startswith("otel-"):
                hook["enabled"] = True

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"{args.action}: "
        + ", ".join(f"{hook['id']}={'on' if hook['enabled'] else 'off'}" for hook in hooks)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
