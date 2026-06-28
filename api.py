#!/usr/bin/env python3
"""AI Quotas API — two endpoints with separate refresh cycles.

  GET /tokens   → Claude transcript token totals (lightweight, every 2 min)
  GET /usage    → Usage % for Claude/Codex/Antigravity (tmux scrape, every 10 min)

Each endpoint reads from a pre-filled cache; no request blocks on a fetch.
"""

import json
import time
import datetime
import subprocess
import re
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, "/Users/fausto/Software/scripts-ai/quota-monitoring")
sys.path.insert(0, "/Users/fausto/Software/scripts-ai/ai-quota-lib")
try:
    from lib import (
        claude_usage_from_transcripts,
        codex_interactive_status,
        claude_interactive_usage,
        antigravity_interactive_usage,
    )
except ImportError:
    pass

from pathlib import Path
import os

HOME = Path.home()

# ── caches ──────────────────────────────────────────────────────────────────
cached_tokens = {
    "claude": {"ok": False, "error": "Not loaded yet"},
    "last_update": None,
}
cached_usage = {
    "claude": {"ok": False, "error": "Not loaded yet"},
    "codex": {"ok": False, "error": "Not loaded yet"},
    "antigravity": {"ok": False, "error": "Not loaded yet"},
    "openrouter": {"ok": False, "error": "Not loaded yet"},
    "aggregate": {"max_used_percent": 0, "providers": []},
    "last_update": None,
}
def _strip_raw(d):
    """Recursively remove raw_text fields."""
    if isinstance(d, dict):
        return {k: _strip_raw(v) for k, v in d.items() if k != "raw_text"}
    if isinstance(d, list):
        return [_strip_raw(v) for v in d]
    return d

cache_lock = threading.Lock()
fetch_tick = 0  # incremented every ~2 min


# ── lightweight fetchers ────────────────────────────────────────────────────

def fetch_claude_tokens():
    """Claude transcript totals — fast, no tmux."""
    try:
        claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
        tokens = claude_usage_from_transcripts(claude_home, since_days=30)
        return {"ok": True, "error": None, "tokens_last_30_days": tokens}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── heavy fetchers (tmux, every 10 min) ─────────────────────────────────────

def fetch_claude_usage():
    try:
        result = claude_interactive_usage(timeout=35)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_codex_usage():
    try:
        result = codex_interactive_status(timeout=30)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_antigravity_usage():
    try:
        result = antigravity_interactive_usage(timeout=35)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_openrouter_credits() -> dict:
    """Fetch OpenRouter credits balance via their API."""
    try:
        import subprocess as _sp
        or_key = _sp.check_output(
            ["zsh", "-ic", 'echo "$OPENROUTER_API_KEY"'],
            stderr=_sp.DEVNULL, timeout=10
        ).decode().strip()
        if not or_key:
            return {"ok": False, "error": "OPENROUTER_API_KEY not found in shell env"}
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": "Bearer " + or_key}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read()).get("data", {})
        total = data.get("total_credits")
        usage = data.get("total_usage")
        remaining = round(total - usage, 2) if total is not None and usage is not None else None
        used_pct = round((usage / total) * 100, 1) if total and total > 0 else 0
        return {
            "ok": True,
            "parsed": {
                "total_credits": total,
                "total_usage": usage,
                "credits_remaining": remaining,
                "used_percent": used_pct,
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── helpers ─────────────────────────────────────────────────────────────────

def compute_aggregate(claude, codex, antigravity):
    """Find the max used-percent across all providers."""
    used_values = []
    providers = []

    if claude.get("ok") and claude.get("parsed"):
        providers.append("claude")
        p = claude["parsed"]
        s = p.get("current_session") or {}
        w = p.get("current_week_all_models") or {}
        if s.get("used_percent") is not None:
            used_values.append(s["used_percent"])
        if w.get("used_percent") is not None:
            used_values.append(w["used_percent"])

    if codex.get("ok") and codex.get("parsed"):
        providers.append("codex")
        p = codex["parsed"]
        fh = p.get("five_hour_limit") or {}
        wk = p.get("weekly_limit") or {}
        if fh.get("used_percent") is not None:
            used_values.append(fh["used_percent"])
        if wk.get("used_percent") is not None:
            used_values.append(wk["used_percent"])

    if antigravity.get("ok") and antigravity.get("parsed"):
        providers.append("antigravity")
        p = antigravity["parsed"]
        highest = p.get("highest_used_percent")
        if highest is not None:
            used_values.append(highest)

    return {
        "max_used_percent": max(used_values) if used_values else 0,
        "providers": providers,
    }


# ── background loop ─────────────────────────────────────────────────────────

def background_fetch_loop():
    global fetch_tick
    time.sleep(5)  # let launchd settle

    while True:
        fetch_tick += 1

        # ── Lightweight: tokens (every cycle, ~2 min) ──
        try:
            claude_tokens = fetch_claude_tokens()
            with cache_lock:
                cached_tokens["claude"] = claude_tokens
                cached_tokens["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        # ── Heavy: usage (first cycle, then every 5 cycles ~10 min) ──
        if fetch_tick == 1 or fetch_tick % 5 == 0:
            now = time.time()
            sys.stderr.write(f"[quota_api] Fetching usage data (tick {fetch_tick})...\n")

            claude = fetch_claude_usage()
            codex = fetch_codex_usage()
            antigravity = fetch_antigravity_usage()
            openrouter = fetch_openrouter_credits()
            agg = compute_aggregate(claude, codex, antigravity)

            with cache_lock:
                cached_usage["claude"] = _strip_raw(claude)
                cached_usage["codex"] = _strip_raw(codex)
                cached_usage["antigravity"] = _strip_raw(antigravity)
                cached_usage["openrouter"] = openrouter
                cached_usage["aggregate"] = agg
                cached_usage["last_update"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            elapsed = time.time() - now
            sys.stderr.write(f"[quota_api] Usage fetch done in {elapsed:.0f}s\n")

        time.sleep(120)  # ~2 min


# ── HTTP handlers ───────────────────────────────────────────────────────────

class TokensHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/tokens":
            self.send_error(404)
            return
        with cache_lock:
            data = dict(cached_tokens)

        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


class UsageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/usage":
            self.send_error(404)
            return
        with cache_lock:
            data = dict(cached_usage)

        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


# ── router ──────────────────────────────────────────────────────────────────

class RouterHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/tokens":
            TokensHandler.do_GET(self)
        elif self.path == "/usage":
            UsageHandler.do_GET(self)
        else:
            self.send_error(404)


# ── main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=background_fetch_loop, daemon=True).start()
    server = HTTPServer(("127.0.0.1", 9899), RouterHandler)
    print("Serving AI Quotas API on 127.0.0.1:9899")
    print("  GET /tokens  — Claude token totals (every ~2 min)")
    print("  GET /usage   — Usage % for all providers (every ~10 min)")
    server.serve_forever()