#!/usr/bin/env python3
"""Print AI CLI telemetry from the local usage API at port 9899."""
import json, sys, urllib.request

try:
    resp = urllib.request.urlopen("http://127.0.0.1:9899/usage", timeout=5)
    d = json.loads(resp.read())
except Exception as e:
    print(f"Error fetching telemetry: {e}")
    sys.exit(1)

ts = d.get("last_update", "n/a")
print(f"=== AI CLI Telemetry ===")
print(f"Last reading: {ts}")
print()

c = d.get("claude", {}).get("parsed", {})
if c:
    s = c.get("current_session", {})
    w = c.get("current_week_all_models", {})
    print("--- Claude Code ---")
    print(f'  Session: {s.get("used_percent","?")}% used — resets {s.get("resets","?")}')
    print(f'  Weekly:  {w.get("used_percent","?")}% used — resets {w.get("resets","?")}')
    print()

x = d.get("codex", {}).get("parsed", {})
if x:
    fh = x.get("five_hour_limit", {})
    wk = x.get("weekly_limit", {})
    print("--- Codex CLI ---")
    print(f'  5h Window: {fh.get("used_percent","?")}% used — resets {fh.get("resets","?")}')
    print(f'  Weekly:    {wk.get("used_percent","?")}% used — resets {wk.get("resets","?")}')
    print()

a = d.get("antigravity", {}).get("parsed", {}).get("models", [])
if a:
    print("--- Antigravity ---")
    for m in a:
        reset = f' — refreshes {m.get("resets","")}' if m.get("resets") else ""
        print(f'  {m["label"]}')
        print(f'    {m["window"]}: {m["used_percent"]}% used ({m["left_percent"]}% left){reset}')
    print()

o = d.get("openrouter", {}).get("parsed", {})
if o:
    print("--- OpenRouter ---")
    if d.get("openrouter", {}).get("ok"):
        print(f'  Credits: ${o.get("credits_remaining","?")} / ${o.get("total_credits","?")}')
        print(f'  Used: {o.get("used_percent","?")}%')
    else:
        print(f'  {d["openrouter"].get("error","unavailable")}')
