#!/usr/bin/env python3
"""
Compute Claude Code monthly usage stats from local JSONL files.

Outputs Claude-only token cost (LiteLLM-aligned pricing), Lines of Code
written via Edit/Write/MultiEdit tool uses, and acceptance rate (1 minus
the proportion of those tool uses rejected by the user).

Cross-platform: Mac, Linux, Windows native (PowerShell), WSL.

Usage:
    python compute_stats.py                    # last calendar month
    python compute_stats.py --month 2026-04    # explicit month
    python compute_stats.py --engineer "Ryan"  # override auto-detected name
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import subprocess
import sys
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.5.0"

# LiteLLM-aligned pricing per 1M tokens (USD). Anthropic public direct API.
# Anthropic re-priced Opus 4.5/4.6/4.7 to $5/$25 (vs older Opus 4/4.1 $15/$75).
PRICING = {
    "claude-opus-4-1":   {"in": 15.00, "out": 75.00, "cache_w": 18.75, "cache_r": 1.50},
    "claude-opus-4":     {"in": 15.00, "out": 75.00, "cache_w": 18.75, "cache_r": 1.50},
    "claude-opus-4-5":   {"in":  5.00, "out": 25.00, "cache_w":  6.25, "cache_r": 0.50},
    "claude-opus-4-6":   {"in":  5.00, "out": 25.00, "cache_w":  6.25, "cache_r": 0.50},
    "claude-opus-4-7":   {"in":  5.00, "out": 25.00, "cache_w":  6.25, "cache_r": 0.50},
    "claude-sonnet-4":   {"in":  3.00, "out": 15.00, "cache_w":  3.75, "cache_r": 0.30},
    "claude-sonnet-4-5": {"in":  3.00, "out": 15.00, "cache_w":  3.75, "cache_r": 0.30},
    "claude-sonnet-4-6": {"in":  3.00, "out": 15.00, "cache_w":  3.75, "cache_r": 0.30},
    "claude-haiku-4-5":  {"in":  1.00, "out":  5.00, "cache_w":  1.25, "cache_r": 0.10},
}

FAMILY_FALLBACK = {
    "opus":   PRICING["claude-opus-4-7"],
    "sonnet": PRICING["claude-sonnet-4-6"],
    "haiku":  PRICING["claude-haiku-4-5"],
}

REJECTION_MARKER = "User rejected tool use"


def claude_projects_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        p = Path(env) / "projects"
        if p.exists():
            return p
    return Path.home() / ".claude" / "projects"


def lookup_price(model: str):
    if model in PRICING:
        return PRICING[model]
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        if parts[0] in PRICING:
            return PRICING[parts[0]]
    for fam, price in FAMILY_FALLBACK.items():
        if fam in model:
            return price
    return None


def cost_for(model: str, usage: dict) -> float:
    p = lookup_price(model)
    if p is None:
        return 0.0
    return (
        usage.get("input_tokens", 0)                  * p["in"]      / 1_000_000
        + usage.get("output_tokens", 0)               * p["out"]     / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * p["cache_w"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0)     * p["cache_r"] / 1_000_000
    )


def count_lines(s) -> int:
    if not isinstance(s, str) or not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _parse_iso(ts: str):
    """Parse an ISO-8601 timestamp (handles trailing Z). Returns tz-aware datetime or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rolling_peak(events, hours: int, idx: int) -> int:
    """Max token sum within any rolling window of `hours`.

    events: list of (datetime, fresh, total) sorted ascending by datetime.
    idx: 1 -> use fresh, 2 -> use total. Limit pressure is about how much was
    consumed within a window, which is what trips the 5h / weekly caps; it is
    independent of monthly total (spread-out usage has low peaks).
    """
    win = timedelta(hours=hours)
    dq: deque = deque()
    cur = 0
    best = 0
    for dt, fr, to in events:
        v = fr if idx == 1 else to
        dq.append((dt, v))
        cur += v
        while dq and dt - dq[0][0] > win:
            cur -= dq.popleft()[1]
        if cur > best:
            best = cur
    return best


def previous_month(today: date) -> str:
    if today.month == 1:
        return f"{today.year - 1}-12"
    return f"{today.year}-{today.month - 1:02d}"


def detect_engineer() -> str:
    for cmd in [["git", "config", "user.name"], ["git", "config", "user.email"]]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def detect_platform() -> str:
    sysname = platform.system().lower()
    if sysname == "darwin":
        return "mac"
    if sysname == "windows":
        return "windows"
    # Detect WSL
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower():
                return "wsl"
    except Exception:
        pass
    return sysname


def analyze(target_month: str) -> dict:
    base = claude_projects_dir()
    if not base.exists():
        return {"error": f"Claude projects directory not found: {base}"}

    seen_msgs = set()
    events = []  # (datetime, fresh, total) for target_month, deduped — limit-pressure metric
    monthly = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "cost_usd": 0.0, "models": set(),
    })

    pending_tool_uses = {}
    rejected_tool_use_ids = set()
    edit_records = {}

    for fp in base.rglob("*.jsonl"):
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = (obj.get("timestamp") or "")[:7]
                    if not ts:
                        continue

                    # Aggregate cost per assistant Claude message
                    if obj.get("type") == "assistant":
                        msg = obj.get("message") or {}
                        model = msg.get("model") or ""
                        if model.startswith("claude-"):
                            usage = msg.get("usage") or {}
                            if any(usage.get(k) for k in (
                                "input_tokens", "output_tokens",
                                "cache_creation_input_tokens", "cache_read_input_tokens"
                            )):
                                key = (msg.get("id") or "", obj.get("requestId") or "")
                                if key == ("", "") or key not in seen_msgs:
                                    seen_msgs.add(key)
                                    bucket = monthly[ts]
                                    for k in ("input_tokens", "output_tokens",
                                              "cache_creation_input_tokens",
                                              "cache_read_input_tokens"):
                                        bucket[k] += usage.get(k, 0)
                                    bucket["cost_usd"] += cost_for(model, usage)
                                    bucket["models"].add(model)
                                    if ts == target_month:
                                        edt = _parse_iso(obj.get("timestamp") or "")
                                        if edt is not None:
                                            fr = (usage.get("input_tokens", 0)
                                                  + usage.get("output_tokens", 0)
                                                  + usage.get("cache_creation_input_tokens", 0))
                                            events.append(
                                                (edt, fr, fr + usage.get("cache_read_input_tokens", 0))
                                            )

                            # Track Edit/Write/MultiEdit tool uses
                            content = msg.get("content")
                            if isinstance(content, list):
                                for block in content:
                                    if not isinstance(block, dict):
                                        continue
                                    if block.get("type") != "tool_use":
                                        continue
                                    name = block.get("name")
                                    if name not in ("Edit", "Write", "MultiEdit"):
                                        continue
                                    tu_id = block.get("id")
                                    if not tu_id:
                                        continue
                                    inp = block.get("input") or {}
                                    if name == "Edit":
                                        lines = count_lines(inp.get("new_string", ""))
                                    elif name == "Write":
                                        lines = count_lines(inp.get("content", ""))
                                    else:  # MultiEdit
                                        lines = sum(
                                            count_lines((e or {}).get("new_string", ""))
                                            for e in (inp.get("edits") or [])
                                        )
                                    edit_records[tu_id] = {
                                        "month": ts, "tool": name, "lines": lines,
                                    }

                    # Detect rejection in user tool_result
                    if obj.get("type") == "user":
                        if obj.get("toolUseResult") == REJECTION_MARKER:
                            msg = obj.get("message") or {}
                            for block in (msg.get("content") or []):
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    tu_id = block.get("tool_use_id")
                                    if tu_id:
                                        rejected_tool_use_ids.add(tu_id)
        except OSError:
            continue

    # Roll up edits into monthly buckets
    monthly_edits = defaultdict(lambda: {
        "edits_proposed": 0, "edits_rejected": 0, "lines_accepted": 0,
    })
    for tu_id, rec in edit_records.items():
        m = rec["month"]
        monthly_edits[m]["edits_proposed"] += 1
        if tu_id in rejected_tool_use_ids:
            monthly_edits[m]["edits_rejected"] += 1
        else:
            monthly_edits[m]["lines_accepted"] += rec["lines"]

    target = monthly.get(target_month, None)
    target_edits = monthly_edits.get(target_month, None)

    if not target and not target_edits:
        return {"error": f"No data found for {target_month}"}

    target = target or {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "cost_usd": 0.0, "models": set(),
    }
    target_edits = target_edits or {
        "edits_proposed": 0, "edits_rejected": 0, "lines_accepted": 0,
    }

    accepted = target_edits["edits_proposed"] - target_edits["edits_rejected"]
    rate = accepted / target_edits["edits_proposed"] if target_edits["edits_proposed"] else None

    # Limit-pressure metric (for Premium/Standard tier-fit research, NOT cost).
    # Reuses the exact same deduped event set as the token totals above, so it
    # never alters the cost / token figures — it is a pure additional aggregation.
    events.sort(key=lambda e: e[0])
    lp_fresh_total = sum(e[1] for e in events)
    peak_5h_fresh = _rolling_peak(events, 5, 1)
    limit_pressure = {
        "fresh_total": lp_fresh_total,
        "peak_5h_fresh": peak_5h_fresh,
        "peak_7d_fresh": _rolling_peak(events, 24 * 7, 1),
        "peak_5h_total": _rolling_peak(events, 5, 2),
        "concentration_5h": round(peak_5h_fresh / lp_fresh_total, 4) if lp_fresh_total else 0.0,
        "active_days": len({e[0].astimezone(timezone.utc).date() for e in events}),
    }

    return {
        "month": target_month,
        "tokens": {
            "input": target["input_tokens"],
            "output": target["output_tokens"],
            "cache_create": target["cache_creation_input_tokens"],
            "cache_read": target["cache_read_input_tokens"],
            "total": (target["input_tokens"] + target["output_tokens"]
                      + target["cache_creation_input_tokens"]
                      + target["cache_read_input_tokens"]),
        },
        "cost_usd": round(target["cost_usd"], 2),
        "models_used": sorted(target["models"]),
        "loc_accepted": target_edits["lines_accepted"],
        "edits_proposed": target_edits["edits_proposed"],
        "edits_rejected": target_edits["edits_rejected"],
        "edits_accepted": accepted,
        "acceptance_rate": round(rate, 4) if rate is not None else None,
        "limit_pressure": limit_pressure,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code monthly stats (cost + LoC + acceptance)")
    ap.add_argument("--month", help="Target YYYY-MM (default: previous calendar month)")
    ap.add_argument("--engineer", help="Override engineer identifier")
    ap.add_argument("--team", help="Team label (e.g. 紘揚科技 / AI事業群 / 創泓技術服務)")
    ap.add_argument("--out", help="Path for JSON fallback output (default: ~/claude-team-stats-<month>.json)")
    args = ap.parse_args()

    target_month = args.month or previous_month(date.today())
    if len(target_month) != 7 or target_month[4] != "-":
        print(f"Invalid --month '{target_month}', expected YYYY-MM", file=sys.stderr)
        return 2

    result = analyze(target_month)
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    result["engineer"] = args.engineer or detect_engineer()
    result["team"] = args.team or ""
    result["platform"] = detect_platform()
    result["script_version"] = VERSION
    result["claude_projects_dir"] = str(claude_projects_dir())

    out_path = Path(args.out) if args.out else Path.home() / f"claude-team-stats-{target_month}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 64)
    print(f"Claude Code 月度用量 — {target_month}")
    print("=" * 64)
    print(f"工程師    : {result['engineer']}")
    print(f"團隊      : {result['team'] or '(未指定，請在 skill 中提供)'}")
    print(f"平台      : {result['platform']}")
    print(f"資料來源  : {result['claude_projects_dir']}")
    print()
    print(f"成本 (USD): ${result['cost_usd']:.2f}")
    print(f"Total tokens: {result['tokens']['total']:,}")
    print(f"  input        : {result['tokens']['input']:,}")
    print(f"  output       : {result['tokens']['output']:,}")
    print(f"  cache_create : {result['tokens']['cache_create']:,}")
    print(f"  cache_read   : {result['tokens']['cache_read']:,}")
    print(f"使用模型  : {', '.join(result['models_used']) or '-'}")
    print()
    print(f"Lines of Code accepted : {result['loc_accepted']:,}")
    print(f"  Edits proposed       : {result['edits_proposed']:,}")
    print(f"  Edits rejected       : {result['edits_rejected']:,}")
    rate = result["acceptance_rate"]
    print(f"  Acceptance rate      : {rate:.1%}" if rate is not None else "  Acceptance rate      : N/A")
    lp = result.get("limit_pressure") or {}
    if lp:
        print()
        print("限額壓力（Premium/Standard 配置研究用，非成本指標）:")
        print(f"  5h 滾動尖峰 (fresh)  : {lp.get('peak_5h_fresh', 0):,}")
        print(f"  7d 滾動尖峰 (fresh)  : {lp.get('peak_7d_fresh', 0):,}")
        print(f"  5h 集中度 (尖峰/月量) : {lp.get('concentration_5h', 0) * 100:.1f}%")
        print(f"  活躍天數             : {lp.get('active_days', 0)}")
    print()
    print(f"JSON 已輸出至: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
