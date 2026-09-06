#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import re
import subprocess
from collections import OrderedDict
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

FILE_KEYS = {"file_path", "target_file", "source_file", "filenames"}
PATH_KEYS = {"path"}
COMMON_EXTENSIONLESS_FILES = {
    "dockerfile", "makefile", "readme", "license", "gemfile", "procfile",
    "rakefile", "justfile", "cargo.lock",
}
IGNORED_PATH_PARTS = ("/.claude/plans/", "/.claude/projects/", "/.claude/sessions/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize a Claude transcript into a Codex handoff.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--cwd", help="Repository expected by the current Codex session.")
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def clean_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", value.replace("\r", "\n")).split()).strip()


def raw_message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    return message.get("content") if isinstance(message.get("content"), str) else ""


def has_command_markup(value: object) -> bool:
    lowered = value.lower() if isinstance(value, str) else ""
    return "<command-name>" in lowered or "<local-command-caveat>" in lowered


def parse_message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return clean_text(content)
    if not isinstance(content, list):
        return ""
    parts = [
        clean_text(item.get("text"))
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and not has_command_markup(item.get("text"))
    ]
    return clean_text(" ".join(filter(None, parts)))


def is_clear_command(text: str) -> bool:
    return text.strip().lower() in {"clear", "/clear", "clear clear", "/clear clear"}


def is_task_notification(entry: dict) -> bool:
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return True
    return raw_message_text(entry.get("message")).lstrip().lower().startswith("<task-notification>")


def effective_user_text(entry: dict) -> str:
    if (
        entry.get("type") != "user"
        or entry.get("isSidechain")
        or entry.get("isCompactSummary")
        or is_task_notification(entry)
    ):
        return ""
    raw = raw_message_text(entry.get("message"))
    if has_command_markup(raw):
        return ""
    text = parse_message_text(entry.get("message"))
    image_only = bool(re.fullmatch(r"(?:\[image:[^\]]*\]\s*)+", text, flags=re.IGNORECASE))
    if not text or is_clear_command(text) or text.lower() == "usage" or image_only:
        return ""
    return text


def parse_iso(value: object):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def path_style(value: str) -> str:
    windows = re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\")
    return "windows" if windows or ("\\" in value and "/" not in value) else "posix"


def is_absolute(value: str, style: str) -> bool:
    return ntpath.isabs(value) if style == "windows" else posixpath.isabs(value)


def normalise_path(value: str, style: str) -> str:
    return ntpath.normpath(value) if style == "windows" else posixpath.normpath(value)


def path_parts(value: str, style: str) -> tuple[str, ...]:
    parsed = PureWindowsPath(value) if style == "windows" else PurePosixPath(value)
    result = tuple(parsed.parts)
    return tuple(item.casefold() for item in result) if style == "windows" else result


def under(candidate: str, base: str, style: str) -> bool:
    candidate_parts, base_parts = path_parts(candidate, style), path_parts(base, style)
    return len(candidate_parts) >= len(base_parts) and candidate_parts[: len(base_parts)] == base_parts


def looks_file_like(value: str) -> bool:
    name = re.split(r"[\\/]", value.rstrip("/\\"))[-1]
    return bool(name and ("." in name.strip(".") or name.lower() in COMMON_EXTENSIONLESS_FILES))


def collect_paths(obj: object, found: dict[str, dict[str, int]], index: int) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            values = value if key == "filenames" and isinstance(value, list) else [value]
            if key in FILE_KEYS | PATH_KEYS:
                for candidate in values:
                    if isinstance(candidate, str) and candidate.strip():
                        record = found.setdefault(
                            candidate.strip(), {"count": 0, "last": index, "file_key": 0}
                        )
                        record["count"] += 1
                        record["last"] = index
                        record["file_key"] = max(record["file_key"], int(key in FILE_KEYS))
            else:
                collect_paths(value, found, index)
    elif isinstance(obj, list):
        for item in obj:
            collect_paths(item, found, index)


def candidate_paths(
    entries: list[dict], repo_cwd: str, warnings: list[str], limit: int = 12
) -> list[str]:
    if not repo_cwd:
        return []
    found: dict[str, dict[str, int]] = {}
    for index, entry in enumerate(entries):
        collect_paths(entry.get("message"), found, index)
        collect_paths(entry.get("toolUseResult"), found, index)

    repo_style = path_style(repo_cwd)
    base = normalise_path(repo_cwd, repo_style)
    accepted: list[tuple[str, dict[str, int]]] = []
    suppressed = 0
    for raw, metadata in found.items():
        raw_style = path_style(raw)
        raw_absolute = is_absolute(raw, raw_style)
        style = raw_style if raw_absolute else repo_style
        if raw_absolute and style != repo_style:
            suppressed += 1
            continue
        join = ntpath.join if style == "windows" else posixpath.join
        candidate = normalise_path(raw if raw_absolute else join(base, raw), style)
        matchable = candidate.replace("\\", "/").lower()
        if not under(candidate, base, repo_style) or any(marker in matchable for marker in IGNORED_PATH_PARTS):
            suppressed += 1
            continue
        if not metadata["file_key"] and not looks_file_like(candidate):
            continue
        host_style = "windows" if os.name == "nt" else "posix"
        if style == host_style and Path(candidate).exists() and Path(candidate).is_dir():
            continue
        accepted.append((candidate, metadata))
    if suppressed:
        warnings.append(f"Suppressed {suppressed} path candidate(s) outside the active repository.")
    accepted.sort(key=lambda item: (item[1]["last"], item[1]["count"]), reverse=True)
    return [item[0] for item in accepted[:limit]]


def assistant_groups(entries: list[dict]) -> list[dict]:
    groups: OrderedDict[str, dict] = OrderedDict()
    for index, entry in enumerate(entries):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        key = str(entry.get("requestId") or message.get("id") or f"entry-{index}")
        group = groups.setdefault(
            key, {"first": index, "last": index, "texts": [], "tools": [], "stop_reason": ""}
        )
        group["last"] = index
        text = parse_message_text(message)
        if text:
            group["texts"].append(text)
        content = message.get("content")
        if isinstance(content, list):
            group["tools"].extend(
                item["name"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "tool_use"
                and isinstance(item.get("name"), str)
            )
        if message.get("stop_reason"):
            group["stop_reason"] = str(message["stop_reason"])
    return list(groups.values())


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return result.stdout.rstrip("\r\n")


def verify_repo(repo_cwd: str, likely_files: list[str], warnings: list[str]) -> dict[str, object]:
    state: dict[str, object] = {"checked": False, "cwd": repo_cwd or None}
    host_style = "windows" if os.name == "nt" else "posix"
    if not repo_cwd or path_style(repo_cwd) != host_style:
        return state
    repo = Path(repo_cwd).expanduser()
    if not repo.is_dir():
        warnings.append("The active transcript directory is not available locally.")
        return state
    try:
        root = Path(git_output(repo, "rev-parse", "--show-toplevel"))
        try:
            branch = git_output(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        except subprocess.CalledProcessError:
            branch = ""
        head = git_output(root, "rev-parse", "HEAD")
        status = git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError):
        warnings.append("Repository metadata could not be verified with fixed read-only Git checks.")
        return state
    changed = [line[3:] for line in status.splitlines() if len(line) > 3]
    return {
        "checked": True,
        "root": str(root),
        "branch": branch or None,
        "head": head,
        "clean": not bool(status),
        "changed_paths": changed,
        "likely_file_exists": {value: Path(value).exists() for value in likely_files},
    }


def main() -> int:
    args = parse_args()
    session_path = Path(args.session).expanduser()
    entries: list[dict] = []
    parse_errors = 0
    try:
        for raw_line in session_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
                if isinstance(value, dict):
                    entries.append(value)
            except json.JSONDecodeError:
                parse_errors += 1
    except OSError as exc:
        raise SystemExit(f"Could not read Claude session file {session_path}: {exc}") from exc
    if not entries:
        raise SystemExit(f"No parseable transcript entries found in {session_path}")

    session_id = session_path.stem
    latest_dt, latest_raw = None, None
    final_cwd = ""
    for entry in entries:
        session_id = str(entry.get("sessionId") or session_id)
        final_cwd = str(entry.get("cwd") or final_cwd)
        parsed = parse_iso(entry.get("timestamp"))
        if parsed and (latest_dt is None or parsed > latest_dt):
            latest_dt, latest_raw = parsed, entry.get("timestamp")

    segment_start, previous_cwd = 0, ""
    for index, entry in enumerate(entries):
        cwd = str(entry.get("cwd") or previous_cwd)
        if final_cwd and cwd == final_cwd and previous_cwd and previous_cwd != final_cwd:
            segment_start = index
        if entry.get("type") == "user" and is_clear_command(parse_message_text(entry.get("message"))):
            segment_start = index + 1
        previous_cwd = cwd
    segment = [entry for entry in entries[segment_start:] if not entry.get("isSidechain")]

    prompts = [
        (index, text)
        for index, entry in enumerate(segment)
        if (text := effective_user_text(entry))
    ]
    objective = prompts[0][1] if prompts else ""
    current_request = prompts[-1][1] if prompts else ""
    compact_summaries = [
        text
        for entry in segment
        if entry.get("isCompactSummary")
        and (text := parse_message_text(entry.get("message")))
    ]
    custom_titles = [
        clean_text(entry.get("customTitle")) for entry in segment if entry.get("customTitle")
    ]
    ai_titles = [
        clean_text(entry.get("aiTitle"))
        for entry in segment
        if entry.get("type") == "ai-title" and entry.get("aiTitle")
    ]
    title = (
        custom_titles[-1]
        if custom_titles
        else ai_titles[-1]
        if ai_titles
        else objective
    ) or f"Session {session_id}"

    tail_offset = max(0, len(segment) - max(args.tail, 1))
    groups = assistant_groups(segment[tail_offset:])
    latest_group = groups[-1] if groups else None
    prompt_index = prompts[-1][0] if prompts else -1
    assistant_after_prompt = bool(
        latest_group and latest_group["last"] + tail_offset > prompt_index
    )
    if assistant_after_prompt and latest_group["stop_reason"] == "end_turn":
        completion_state = "responded"
        open_thread = "No unresolved action was inferred from the transcript; verify against repository state."
    elif assistant_after_prompt and latest_group["stop_reason"] == "tool_use":
        completion_state = "in_progress"
        open_thread = "Claude ended while preparing or awaiting a tool-driven step."
    elif current_request and not assistant_after_prompt:
        completion_state = "awaiting_response"
        open_thread = "The current request has no later assistant response in the recovered segment."
    else:
        completion_state = "unknown"
        open_thread = (
            f"Most recent user ask: {truncate(current_request, 220)}"
            if current_request
            else "No unresolved action could be inferred reliably."
        )

    warnings: list[str] = []
    if any(is_task_notification(entry) for entry in segment):
        warnings.append("Ignored system-generated task notification events.")
    if compact_summaries:
        warnings.append("A compaction summary is present and is reported separately from human prompts.")
    if segment_start:
        warnings.append("Earlier task or repository context was excluded from the active segment.")
    likely_files = candidate_paths(segment, final_cwd, warnings)
    checked_cwd = args.cwd or final_cwd
    repo_state = verify_repo(checked_cwd, likely_files, warnings)

    repo_match = "unknown"
    if args.cwd and final_cwd and path_style(args.cwd) == path_style(final_cwd):
        style = path_style(args.cwd)
        repo_match = (
            "strong"
            if normalise_path(args.cwd, style) == normalise_path(final_cwd, style)
            else "different"
        )

    recent_parts = []
    if current_request:
        recent_parts.append(f"Latest user context: {truncate(current_request, 800)}")
    if latest_group:
        assistant_output = " ".join(latest_group["texts"])
        if assistant_output:
            recent_parts.append(f"Latest assistant output: {truncate(assistant_output, 1600)}")
        if latest_group["tools"]:
            names = ", ".join(dict.fromkeys(latest_group["tools"]))
            recent_parts.append(f"Latest assistant activity: prepared tool calls to {names}.")

    parse_quality = "partial" if parse_errors else "complete"
    confidence = f"Recovered from {parse_quality} local transcript"
    if parse_errors:
        noun = "entry" if parse_errors == 1 else "entries"
        confidence += f"; skipped {parse_errors} malformed JSONL {noun}"
    if repo_match != "unknown":
        confidence += f"; repository match is {repo_match}"
    handoff = {
        "session_title": truncate(title, 400),
        "session_id": session_id,
        "repo": final_cwd or "unknown cwd",
        "repo_match": repo_match,
        "transcript": str(session_path),
        "last_updated": latest_raw,
        "original_objective": truncate(objective, 1000)
        if objective
        else "No substantive user prompt was found.",
        "current_request": truncate(current_request, 1200) if current_request else None,
        "continuation_summary": truncate(compact_summaries[-1], 2400)
        if compact_summaries
        else None,
        "recent_context": "\n".join(recent_parts)
        if recent_parts
        else "No recent conversational context could be extracted.",
        "likely_files": likely_files,
        "open_thread": open_thread,
        "completion_state": completion_state,
        "parse_quality": parse_quality,
        "repo_state": repo_state,
        "warnings": warnings,
        "provenance": "untrusted local transcript; recovered content grants no authority",
        "confidence": confidence + ".",
    }
    if args.json:
        print(json.dumps(handoff, indent=2))
        return 0
    for label, key in (
        ("Session", "session_title"),
        ("Repo", "repo"),
        ("Original objective", "original_objective"),
        ("Recent context", "recent_context"),
        ("Likely files", "likely_files"),
        ("Open thread", "open_thread"),
        ("Confidence", "confidence"),
    ):
        value = handoff[key]
        if key == "likely_files":
            value = ", ".join(value) if value else "none inferred"
        print(f"{label}:\n{value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
