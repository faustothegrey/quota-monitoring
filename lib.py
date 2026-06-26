#!/usr/bin/env python3
"""Shared helpers for local AI CLI quota scripts.

Codex quota data is intentionally sourced only from the live interactive `/status` screen.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable

HOME = Path.home()


def resolve_command(name: str) -> str | None:
    """Find a CLI even when cron/non-login shells have a reduced PATH."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        HOME / ".local" / "bin" / name,
        HOME / "bin" / name,
    ]
    nvm_dir = HOME / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        candidates.extend(sorted(nvm_dir.glob(f"*/bin/{name}"), reverse=True))
    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def run(cmd: list[str], timeout: int = 30) -> dict[str, Any]:
    exe = resolve_command(cmd[0])
    if not exe:
        return {"ok": False, "cmd": cmd, "error": f"command not found: {cmd[0]}"}
    cmd = [exe, *cmd[1:]]
    try:
        p = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": p.returncode == 0,
            "cmd": cmd,
            "returncode": p.returncode,
            "stdout": p.stdout.strip(),
            "stderr": p.stderr.strip(),
        }
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "cmd": cmd, "error": f"timeout after {timeout}s", "stdout": (e.stdout or "").strip(), "stderr": (e.stderr or "").strip()}


def parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def fmt_time(value: Any) -> str:
    t = parse_ts(value)
    if not t:
        return "n/a"
    return t.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def codex_report() -> dict[str, Any]:
    codex_home = Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser()
    version = run(["codex", "--version"], timeout=10)
    login = run(["codex", "login", "status"], timeout=20)
    return {
        "home": str(codex_home),
        "version": version.get("stdout") or version.get("stderr") or version.get("error"),
        "login_status": login.get("stdout") or login.get("stderr") or login.get("error"),
        "interactive_status": codex_interactive_status(),
    }


def parse_codex_interactive_status(text: str) -> dict[str, Any]:
    """Parse Codex CLI's interactive /status screen."""
    out: dict[str, Any] = {"raw_text": text}

    m = re.search(r"Account:\s*([^\n│]+)", text, flags=re.I)
    if m:
        out["account"] = m.group(1).strip()

    model_matches = [m.group(1).strip() for m in re.finditer(r"Model:\s*([^\n│]+)", text, flags=re.I)]
    model_matches = [m for m in model_matches if "loading" not in m.lower()]
    if model_matches:
        out["model"] = model_matches[-1]

    # Example:
    # 5h limit: [████░░] 69% left (resets 13:04)
    # Weekly limit: [████░░] 68% left (resets 10:07 on 12 Jun)
    for key, label in [("five_hour_limit", r"5h\s+limit"), ("weekly_limit", r"Weekly\s+limit")]:
        m = re.search(label + r":\s*(?:\[[^\]]*\]\s*)?([0-9]+)%\s+left\s+\(resets\s+([^\)]+)\)", text, flags=re.I)
        if m:
            left = int(m.group(1))
            out[key] = {"left_percent": left, "used_percent": 100 - left, "resets": m.group(2).strip()}

    return out


def codex_interactive_status(timeout: int = 30) -> dict[str, Any]:
    """Run Codex in tmux, send `/status`, capture current quota lines, exit."""
    codex_cmd = resolve_command("codex")
    if not codex_cmd:
        return {"ok": False, "error": "command not found: codex"}
    if not resolve_command("tmux"):
        return {"ok": False, "error": "command not found: tmux"}
    if not resolve_command("git"):
        return {"ok": False, "error": "command not found: git"}

    session = f"ai_cli_quotas_codex_{os.getpid()}_{int(time.time())}"
    workdir = Path(subprocess.check_output(["mktemp", "-d", "/tmp/codex-quota-XXXXXX"], text=True).strip())

    def tmux_cmd(args: list[str], cmd_timeout: int = 10) -> dict[str, Any]:
        return run(["tmux", *args], timeout=cmd_timeout)

    try:
        subprocess.run([resolve_command("git") or "git", "-C", str(workdir), "init", "-q"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        start = tmux_cmd(["new-session", "-d", "-s", session, "-x", "140", "-y", "45", "-c", str(workdir), codex_cmd])
        if not start.get("ok"):
            return {"ok": False, "error": start.get("stderr") or start.get("stdout") or start.get("error")}

        time.sleep(5)
        tmux_cmd(["send-keys", "-t", session, "Enter"])  # accept trust dialog if present
        time.sleep(8)  # wait for model/MCP startup; early Enter can be ignored by the TUI
        tmux_cmd(["send-keys", "-t", session, "/status"])
        time.sleep(1)
        tmux_cmd(["send-keys", "-t", session, "Enter"])
        time.sleep(max(6, min(timeout, 20)))
        captured = tmux_cmd(["capture-pane", "-t", session, "-p", "-S", "-160"], cmd_timeout=10)
        raw = captured.get("stdout") or ""
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        tmux_cmd(["kill-session", "-t", session], cmd_timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    clean = clean_tui_text(raw)
    parsed = parse_codex_interactive_status(clean)
    return {"ok": bool(parsed.get("five_hour_limit") or parsed.get("weekly_limit")), "parsed": parsed}


def parse_antigravity_interactive_usage(text: str) -> dict[str, Any]:
    """Parse Google Antigravity CLI's interactive /usage screen.

    agy shows usage by model groups (GEMINI MODELS, CLAUDE AND GPT MODELS)
    each with a Weekly Limit and Five Hour Limit.
    """
    out: dict[str, Any] = {"raw_text": text, "window": "5h"}

    # Header line showing account & model
    m = re.search(r"[▀▄]+\s+([^\n]+?\\([^\n]+?\\))", text)
    if m:
        out["current_model"] = m.group(1).strip()

    marker = re.search(r"Model\s+Quota", text, flags=re.I)
    quota_text = text[marker.end():] if marker else text
    quota_text = re.split(r"↑/↓\s*Scroll|esc\s+to\s+cancel", quota_text, maxsplit=1, flags=re.I)[0]

    models: list[dict[str, Any]] = []
    current_group: str | None = None
    lines = [line.strip() for line in quota_text.splitlines() if line.strip()]

    for i, line in enumerate(lines[:-1]):
        # Track model group headers
        gm = re.match(r"(GEMINI MODELS|CLAUDE AND GPT MODELS)", line, flags=re.I)
        if gm:
            current_group = gm.group(1).title()
            continue

        # Skip progress bar lines and structural markers
        if re.search(r"[█░▓▒]", line) or line.startswith(("└", ">", "─")):
            continue

        # Skip CLAUDE AND GPT MODELS — noise, always at 100% unused
        if current_group and "claude and gpt" in current_group.lower():
            continue

        # Look for a percentage on the next line (the bar value)
        pct_match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", lines[i + 1])
        if not pct_match:
            continue

        name = line.strip("│ ")
        if not name or name.lower() in {"quota available"}:
            continue

        # Full integer percent: 5.10% → 5; fallback to "-" if unparsable
        try:
            left_val = int(float(pct_match.group(1)))
            left = max(0, min(100, left_val))
            used = 100 - left
        except (ValueError, TypeError):
            left = "-"
            used = "-"

        # Determine window type from entry name — do NOT hardcode "5h"
        if "weekly" in name.lower():
            window_val = "weekly"
        elif "five hour" in name.lower():
            window_val = "5h"
        else:
            window_val = "5h"

        # Prefix label with model group name
        label = f"{current_group} ({name})" if current_group else name

        status = lines[i + 2].strip("│ ") if i + 2 < len(lines) and not re.search(r"[█░▓▒]", lines[i + 2]) else None
        entry: dict[str, Any] = {
            "label": label,
            "left_percent": left,
            "used_percent": used,
            "window": window_val,
        }
        if status:
            entry["status"] = status
            reset_match = re.search(r"(?:reset(?:s)?\s*(?:at|in)?|Refreshes?)\s+(.+)$", status, flags=re.I)
            if reset_match:
                entry["resets"] = reset_match.group(1).strip()
        if not any(existing.get("label") == label for existing in models):
            models.append(entry)

    if models:
        out["models"] = models
        # Aggregate — skip entries with "-" (unparsable)
        numeric_left = [m["left_percent"] for m in models if isinstance(m["left_percent"], (int, float))]
        numeric_used = [m["used_percent"] for m in models if isinstance(m["used_percent"], (int, float))]
        if numeric_left:
            out["lowest_left_percent"] = min(numeric_left)
            out["highest_used_percent"] = max(numeric_used)
    return out


def antigravity_interactive_usage(timeout: int = 35) -> dict[str, Any]:
    """Run agy in tmux, send `/usage`, capture the 5-hour model quota screen, exit."""
    agy_cmd = resolve_command("agy")
    if not agy_cmd:
        return {"ok": False, "error": "command not found: agy"}
    if not resolve_command("tmux"):
        return {"ok": False, "error": "command not found: tmux"}
    if not resolve_command("git"):
        return {"ok": False, "error": "command not found: git"}

    session = f"ai_cli_quotas_agy_{os.getpid()}_{int(time.time())}"
    workdir = Path(subprocess.check_output(["mktemp", "-d", "/tmp/agy-quota-XXXXXX"], text=True).strip())

    def tmux_cmd(args: list[str], cmd_timeout: int = 10) -> dict[str, Any]:
        return run(["tmux", *args], timeout=cmd_timeout)

    try:
        subprocess.run([resolve_command("git") or "git", "-C", str(workdir), "init", "-q"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        start = tmux_cmd(["new-session", "-d", "-s", session, "-x", "140", "-y", "45", "-c", str(workdir), agy_cmd])
        if not start.get("ok"):
            return {"ok": False, "error": start.get("stderr") or start.get("stdout") or start.get("error")}

        time.sleep(6)
        tmux_cmd(["send-keys", "-t", session, "Enter"])  # harmless if no trust dialog is present
        time.sleep(3)
        tmux_cmd(["send-keys", "-t", session, "/usage", "Enter"])
        time.sleep(max(8, min(timeout, 25)))
        captured = tmux_cmd(["capture-pane", "-t", session, "-p", "-S", "-220"], cmd_timeout=10)
        tmux_cmd(["send-keys", "-t", session, "Escape"])
        raw = captured.get("stdout") or ""
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        tmux_cmd(["kill-session", "-t", session], cmd_timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    clean = clean_tui_text(raw)
    parsed = parse_antigravity_interactive_usage(clean)
    return {"ok": bool(parsed.get("models")), "parsed": parsed}


def antigravity_report(since_days: int | None = 30, interactive_usage: bool = True) -> dict[str, Any]:
    """Best-effort local activity and quota report for Google Antigravity CLI (`agy`).

    agy exposes current quota through interactive `/usage` only. It currently
    shows a 5-hour model quota window, not weekly/monthly usage.
    """
    agy_home = Path(os.environ.get("ANTIGRAVITY_HOME", HOME / ".gemini" / "antigravity-cli")).expanduser()
    version = run(["agy", "--version"], timeout=10)
    models_cmd = run(["agy", "models"], timeout=30)
    models = [line.strip() for line in (models_cmd.get("stdout") or "").splitlines() if line.strip()]

    cutoff: dt.datetime | None = None
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)

    def file_time(path: Path) -> dt.datetime:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)

    history_entries = 0
    history_latest: dt.datetime | None = None
    history_path = agy_home / "history.jsonl"
    if history_path.exists():
        for obj in read_jsonl(history_path):
            ts = obj.get("timestamp")
            t = None
            if isinstance(ts, (int, float)):
                # Antigravity history uses milliseconds since epoch.
                t = dt.datetime.fromtimestamp(ts / 1000 if ts > 10_000_000_000 else ts, tz=dt.timezone.utc)
            if cutoff and t and t < cutoff:
                continue
            history_entries += 1
            if t and (history_latest is None or t > history_latest):
                history_latest = t

    conversations_dir = agy_home / "conversations"
    conversation_files = []
    if conversations_dir.exists():
        for path in conversations_dir.iterdir():
            if path.suffix in {".db", ".pb"}:
                try:
                    t = file_time(path)
                except OSError:
                    continue
                if cutoff and t < cutoff:
                    continue
                conversation_files.append(path)

    transcript_files = list(agy_home.glob("brain/*/.system_generated/logs/transcript.jsonl"))
    transcript_full_files = list(agy_home.glob("brain/*/.system_generated/logs/transcript_full.jsonl"))
    transcript_lines = 0
    transcript_latest: dt.datetime | None = None
    matching_transcripts = 0
    for path in transcript_files:
        try:
            t = file_time(path)
        except OSError:
            continue
        if cutoff and t < cutoff:
            continue
        matching_transcripts += 1
        transcript_latest = t if transcript_latest is None or t > transcript_latest else transcript_latest
        for _ in read_jsonl(path):
            transcript_lines += 1

    logs_dir = agy_home / "log"
    log_files = []
    stream_generate_calls = 0
    load_code_assist_calls = 0
    fetch_models_calls = 0
    print_mode_sessions = 0
    auth_success_seen = False
    latest_log_at: dt.datetime | None = None
    selected_models: dict[str, int] = {}
    rate_or_quota_messages: list[str] = []
    if logs_dir.exists():
        for path in logs_dir.glob("cli-*.log"):
            try:
                t = file_time(path)
            except OSError:
                continue
            if cutoff and t < cutoff:
                continue
            log_files.append(path)
            latest_log_at = t if latest_log_at is None or t > latest_log_at else latest_log_at
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "streamGenerateContent" in line:
                            stream_generate_calls += 1
                        if "loadCodeAssist" in line:
                            load_code_assist_calls += 1
                        if "fetchAvailableModels" in line:
                            fetch_models_calls += 1
                        if "Print mode: starting" in line:
                            print_mode_sessions += 1
                        if "OAuth: authenticated successfully" in line:
                            auth_success_seen = True
                        m = re.search(r'Propagating selected model override to backend: label="([^"]+)"', line)
                        if m:
                            selected_models[m.group(1)] = selected_models.get(m.group(1), 0) + 1
                        if re.search(r"rate limit|usage limit|RESOURCE_EXHAUSTED|too many requests", line, flags=re.I):
                            cleaned = re.sub(r"email=[^,\s]+", "email=[REDACTED]", line.strip())
                            if cleaned not in rate_or_quota_messages and len(rate_or_quota_messages) < 10:
                                rate_or_quota_messages.append(cleaned[:300])
            except OSError:
                continue

    interactive = antigravity_interactive_usage(timeout=35) if interactive_usage else {"ok": False, "skipped": True}

    return {
        "home": str(agy_home),
        "version": version.get("stdout") or version.get("stderr") or version.get("error"),
        "models": models,
        "interactive_usage": interactive,
        "auth_success_seen_in_logs": auth_success_seen,
        "history_entries": history_entries,
        "history_latest_at": history_latest.isoformat() if history_latest else None,
        "conversation_files": len(conversation_files),
        "transcript_files": matching_transcripts,
        "transcript_full_files_total": len(transcript_full_files),
        "transcript_steps": transcript_lines,
        "transcript_latest_at": transcript_latest.isoformat() if transcript_latest else None,
        "log_files": len(log_files),
        "latest_log_at": latest_log_at.isoformat() if latest_log_at else None,
        "stream_generate_calls": stream_generate_calls,
        "load_code_assist_calls": load_code_assist_calls,
        "fetch_available_models_calls": fetch_models_calls,
        "print_mode_sessions": print_mode_sessions,
        "selected_models_seen_in_logs": selected_models,
        "rate_or_quota_messages": rate_or_quota_messages,
        "since_days": since_days,
        "quota_note": "Antigravity agy exposes current quota via interactive `/usage` only; parsed values are the 5-hour model window. Local streamGenerateContent counts are request/activity counts, not token totals.",
    }


def claude_usage_from_transcripts(claude_home: Path, since_days: int | None = None) -> dict[str, Any]:
    projects = claude_home / "projects"
    totals: dict[str, int] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    files = 0
    messages = 0
    cutoff: dt.datetime | None = None
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    latest_ts: dt.datetime | None = None
    earliest_ts: dt.datetime | None = None

    if not projects.exists():
        return {"files": 0, "messages_with_usage": 0, "totals": totals}

    for path in projects.rglob("*.jsonl"):
        files += 1
        for obj in read_jsonl(path):
            ts = parse_ts(obj.get("timestamp"))
            if cutoff and ts and ts < cutoff:
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict) or not usage:
                continue
            messages += 1
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            if ts and (earliest_ts is None or ts < earliest_ts):
                earliest_ts = ts
            for key in totals:
                val = usage.get(key) or 0
                if isinstance(val, (int, float)):
                    totals[key] += int(val)
    return {
        "files": files,
        "messages_with_usage": messages,
        "earliest_usage_at": earliest_ts.isoformat() if earliest_ts else None,
        "latest_usage_at": latest_ts.isoformat() if latest_ts else None,
        "totals": totals,
        "since_days": since_days,
    }


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_tui_text(raw: str) -> str:
    text = raw.replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def parse_claude_interactive_usage(text: str) -> dict[str, Any]:
    """Parse useful fields from Claude Code's interactive /usage screen."""
    out: dict[str, Any] = {"raw_text": text}

    m = re.search(r"Total\s*cost:\s*\$([0-9.]+)", text, flags=re.I)
    if m:
        out["session_total_cost_usd"] = float(m.group(1))

    m = re.search(r"Usage:\s*([0-9,]+)\s*input,\s*([0-9,]+)\s*output,\s*([0-9,]+)\s*cache\s*read,\s*([0-9,]+)\s*cache\s*write", text, flags=re.I)
    if m:
        out["session_tokens"] = {
            "input": int(m.group(1).replace(",", "")),
            "output": int(m.group(2).replace(",", "")),
            "cache_read": int(m.group(3).replace(",", "")),
            "cache_write": int(m.group(4).replace(",", "")),
        }

    # Multi-line format: header line, then progress bar line, then "Resets HH:MM" line
    # Progress bar line uses Unicode blocks (█▌▐▊▋░▓▒) that can't be exhaustively listed,
    # so we skip the entire progress line and go straight to the %/Resets.
    m = re.search(
        r"Current\s*session\s*\n\s*[^\n]*?([0-9]+)%\s*used\s*\n\s*Resets\s*([^\n]+)",
        text, flags=re.I)
    if m:
        out["current_session"] = {"used_percent": int(m.group(1)), "resets": m.group(2).strip()}

    m = re.search(
        r"Current\s*week\s*\(all\s*models\)\s*\n\s*[^\n]*?([0-9]+)%\s*used\s*\n\s*Resets\s*([^\n]+)",
        text, flags=re.I)
    if m:
        out["current_week_all_models"] = {"used_percent": int(m.group(1)), "resets": m.group(2).strip()}

    m = re.search(r"Usage\s*credits\s+(.*?)(?:\n|$)", text, flags=re.I)
    if m:
        out["usage_credits"] = m.group(1).strip()

    return out


def claude_interactive_usage(workdir: Path = HOME, timeout: int = 35) -> dict[str, Any]:
    """Run Claude Code in tmux, send `/usage`, capture screen text, exit.

    tmux keeps the script simple and avoids manual PTY/select handling. This is
    still best-effort because it automates a full TUI.
    """
    claude_cmd = resolve_command("claude")
    if not claude_cmd:
        return {"ok": False, "error": "command not found: claude"}
    if not resolve_command("tmux"):
        return {"ok": False, "error": "command not found: tmux"}

    session = f"ai_cli_quotas_claude_{os.getpid()}_{int(time.time())}"

    def tmux_cmd(args: list[str], cmd_timeout: int = 10) -> dict[str, Any]:
        return run(["tmux", *args], timeout=cmd_timeout)

    try:
        start = tmux_cmd(["new-session", "-d", "-s", session, "-x", "140", "-y", "45", "-c", str(workdir), claude_cmd])
        if not start.get("ok"):
            return {"ok": False, "error": start.get("stderr") or start.get("stdout") or start.get("error")}

        time.sleep(4)
        tmux_cmd(["send-keys", "-t", session, "Enter"])  # accept trust dialog if present
        time.sleep(2)
        tmux_cmd(["send-keys", "-t", session, "/usage", "Enter"])
        time.sleep(max(8, min(timeout, 25)))
        captured = tmux_cmd(["capture-pane", "-t", session, "-p", "-S", "-220"], cmd_timeout=10)
        tmux_cmd(["send-keys", "-t", session, "Escape"])
        tmux_cmd(["send-keys", "-t", session, "/exit", "Enter"])
        raw = captured.get("stdout") or ""
    finally:
        tmux_cmd(["kill-session", "-t", session], cmd_timeout=5)

    clean = clean_tui_text(raw)
    parsed = parse_claude_interactive_usage(clean)
    return {"ok": bool(parsed.get("current_week_all_models") or parsed.get("current_session")), "parsed": parsed}


def claude_report(since_days: int | None = 30, interactive_usage: bool = True) -> dict[str, Any]:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
    version = run(["claude", "--version"], timeout=10)
    auth_json = run(["claude", "auth", "status", "--json"], timeout=30)
    auth: Any = None
    if auth_json.get("stdout"):
        try:
            auth = json.loads(auth_json["stdout"])
        except json.JSONDecodeError:
            auth = auth_json["stdout"]
    usage_cmd = run(["claude", "-p", "/usage", "--max-turns", "1", "--output-format", "json", "--no-session-persistence"], timeout=60)
    usage_text = None
    if usage_cmd.get("stdout"):
        try:
            usage_text = json.loads(usage_cmd["stdout"]).get("result")
        except json.JSONDecodeError:
            usage_text = usage_cmd["stdout"]
    interactive = claude_interactive_usage(timeout=35) if interactive_usage else {"ok": False, "skipped": True}
    return {
        "home": str(claude_home),
        "version": version.get("stdout") or version.get("stderr") or version.get("error"),
        "auth_status": auth if auth is not None else (auth_json.get("stderr") or auth_json.get("error")),
        "usage_command_result": usage_text or usage_cmd.get("stderr") or usage_cmd.get("error"),
        "interactive_usage": interactive,
        "local_transcript_usage": claude_usage_from_transcripts(claude_home, since_days=since_days),
        "quota_note": "Claude detailed current/week percentages come from interactive `/usage`; transcript totals are local historical usage and may not include other devices or claude.ai.",
    }

