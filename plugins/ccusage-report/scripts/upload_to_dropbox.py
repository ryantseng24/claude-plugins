#!/usr/bin/env python3
"""
Upload monthly Claude Code usage stats to central Dropbox.

Reads a stats JSON produced by compute_stats.py, generates CSV + markdown
summary (with upload timestamp), uploads both to the Dropbox App folder
under /<month>/<engineer>_<upload_date>.{csv,md}.

Cross-platform: relies only on stdlib + no external services beyond Dropbox.
Failures are reported but never raised to caller — skill flow keeps going.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "1.0.0"

DROPBOX_CONFIG = Path(__file__).parent / "dropbox.json"
TOKEN_ENDPOINT = "https://api.dropboxapi.com/oauth2/token"
UPLOAD_ENDPOINT = "https://content.dropboxapi.com/2/files/upload"
HTTP_TIMEOUT_SEC = 30
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 3


def log(msg: str) -> None:
    print(f"[upload] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[upload] WARN: {msg}", file=sys.stderr, flush=True)


def fatal(msg: str, code: int = 1) -> int:
    print(f"[upload] FAIL: {msg}", file=sys.stderr, flush=True)
    return code


def load_dropbox_config() -> dict | None:
    if not DROPBOX_CONFIG.exists():
        warn(f"找不到 {DROPBOX_CONFIG.name}，無法上傳。")
        return None
    try:
        raw = json.loads(DROPBOX_CONFIG.read_text(encoding="utf-8"))
        decoded = base64.b64decode(raw["data"]).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        warn(f"{DROPBOX_CONFIG.name} 解碼失敗: {e}")
        return None


def make_ssl_context() -> ssl.SSLContext:
    """Prefer certifi if installed (handles python.org Python missing certs)."""
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def http_post(url: str, data: bytes, headers: dict, ctx: ssl.SSLContext) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC, context=ctx) as resp:
        return resp.read()


def get_access_token(creds: dict, ctx: ssl.SSLContext) -> str | None:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["app_key"],
            "client_secret": creds["app_secret"],
        }
    ).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        raw = http_post(TOKEN_ENDPOINT, body, headers, ctx)
        return json.loads(raw)["access_token"]
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode()
        except Exception:
            pass
        if e.code == 401:
            warn("Dropbox token 失效，請聯繫技術長 Ryan 更新 plugin。")
        else:
            warn(f"換取 access token 失敗 ({e.code}): {body_text}")
        return None
    except urllib.error.URLError as e:
        warn(f"連線 Dropbox 失敗: {e.reason}")
        return None
    except Exception as e:
        warn(f"換取 access token 例外: {e}")
        return None


def upload_blob(
    access_token: str,
    dropbox_path: str,
    content: bytes,
    ctx: ssl.SSLContext,
) -> dict | None:
    api_arg = json.dumps(
        {
            "path": dropbox_path,
            "mode": "add",
            "autorename": True,
            "mute": True,
            "strict_conflict": False,
        },
        ensure_ascii=False,
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Dropbox-API-Arg": api_arg,
        "Content-Type": "application/octet-stream",
    }

    last_err: str = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            raw = http_post(UPLOAD_ENDPOINT, content, headers, ctx)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {body_text}"
            if 400 <= e.code < 500 and e.code != 429:
                warn(f"上傳 {dropbox_path} 失敗 ({last_err}); 不重試")
                return None
        except urllib.error.URLError as e:
            last_err = f"URLError: {e.reason}"
        except Exception as e:
            last_err = f"Exception: {e}"

        if attempt < RETRY_ATTEMPTS:
            log(f"上傳 {dropbox_path} 第 {attempt} 次失敗 ({last_err})；{RETRY_BACKOFF_SEC}s 後重試")
            time.sleep(RETRY_BACKOFF_SEC)

    warn(f"上傳 {dropbox_path} 最終失敗: {last_err}")
    return None


_SAFE_ENGINEER = re.compile(r"[^A-Za-z0-9._@\-]+")


def sanitize_engineer(raw: str) -> str:
    name = (raw or "unknown").strip()
    name = _SAFE_ENGINEER.sub("_", name)
    return name[:80] or "unknown"


def stats_to_csv(stats: dict, upload_date: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    headers = [
        "upload_date", "month", "engineer", "team", "platform",
        "total_tokens", "input_tokens", "output_tokens",
        "cache_create_tokens", "cache_read_tokens",
        "cost_usd", "loc_accepted", "edits_proposed", "edits_rejected",
        "acceptance_rate", "models_used", "script_version",
    ]
    tokens = stats.get("tokens") or {}
    rate = stats.get("acceptance_rate")
    row = [
        upload_date,
        stats.get("month", ""),
        stats.get("engineer", ""),
        stats.get("team", ""),
        stats.get("platform", ""),
        tokens.get("total", 0),
        tokens.get("input", 0),
        tokens.get("output", 0),
        tokens.get("cache_create", 0),
        tokens.get("cache_read", 0),
        stats.get("cost_usd", 0),
        stats.get("loc_accepted", 0),
        stats.get("edits_proposed", 0),
        stats.get("edits_rejected", 0),
        f"{rate:.4f}" if isinstance(rate, (int, float)) else "",
        ";".join(stats.get("models_used") or []),
        stats.get("script_version", ""),
    ]
    w.writerow(headers)
    w.writerow(row)
    return buf.getvalue().encode("utf-8")


def stats_to_markdown(stats: dict, upload_iso: str) -> bytes:
    tokens = stats.get("tokens") or {}
    rate = stats.get("acceptance_rate")
    rate_str = f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "N/A"
    cost = stats.get("cost_usd", 0)
    models = stats.get("models_used") or []

    lines = [
        "---",
        f"upload_date: {upload_iso}",
        f"month: {stats.get('month','')}",
        f"engineer: {stats.get('engineer','')}",
        f"team: {stats.get('team','')}",
        f"platform: {stats.get('platform','')}",
        "---",
        "",
        f"# Claude Code 月度用量報告 — {stats.get('month','')}",
        "",
        f"- **Upload date**: {upload_iso}",
        f"- **工程師**: {stats.get('engineer','')}",
        f"- **團隊**: {stats.get('team','')}",
        f"- **平台**: {stats.get('platform','')}",
        "",
        "## Token 統計",
        f"- 總 Token 數: {tokens.get('total', 0):,}",
        f"  - input: {tokens.get('input', 0):,}",
        f"  - output: {tokens.get('output', 0):,}",
        f"  - cache_create: {tokens.get('cache_create', 0):,}",
        f"  - cache_read: {tokens.get('cache_read', 0):,}",
        f"- **總成本 (USD)**: ${cost:.2f}" if isinstance(cost, (int, float)) else f"- **總成本 (USD)**: {cost}",
        f"- 使用模型: {', '.join(models) if models else '-'}",
        "",
        "## 程式碼產出",
        f"- 接受程式碼行數: {stats.get('loc_accepted', 0):,}",
        f"- Edits 提案數: {stats.get('edits_proposed', 0):,}",
        f"- Edits 拒絕數: {stats.get('edits_rejected', 0):,}",
        f"- 採納率: {rate_str}",
        "",
        f"_由 ccusage-report plugin 上傳，script_version={stats.get('script_version','?')}_",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload Claude Code monthly stats to Dropbox")
    ap.add_argument("--stats", required=True, help="Path to stats JSON from compute_stats.py")
    args = ap.parse_args()

    stats_path = Path(args.stats)
    if not stats_path.exists():
        return fatal(f"stats JSON 不存在: {stats_path}", code=2)

    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except Exception as e:
        return fatal(f"stats JSON 解析失敗: {e}", code=2)

    month = stats.get("month") or ""
    engineer_raw = stats.get("engineer") or ""
    if not month or not re.fullmatch(r"\d{4}-\d{2}", month):
        return fatal(f"stats 缺少有效 month 欄位: {month!r}", code=2)
    if not engineer_raw:
        return fatal("stats 缺少 engineer 欄位", code=2)

    engineer = sanitize_engineer(engineer_raw)
    now = datetime.now()
    upload_date = now.strftime("%Y%m%d")
    upload_iso = now.strftime("%Y-%m-%d %H:%M:%S")

    creds = load_dropbox_config()
    if not creds:
        warn("Dropbox 設定不可用，跳過上傳。本機 JSON 仍存於 " + str(stats_path))
        return 0

    ctx = make_ssl_context()
    log("換取 access token …")
    access_token = get_access_token(creds, ctx)
    if not access_token:
        warn("無法取得 access token，跳過上傳。本機 JSON 仍存於 " + str(stats_path))
        return 0

    csv_bytes = stats_to_csv(stats, upload_iso)
    md_bytes = stats_to_markdown(stats, upload_iso)

    base = f"/{month}/{engineer}_{upload_date}"
    csv_path = f"{base}.csv"
    md_path = f"{base}.md"

    log(f"上傳 {csv_path} …")
    csv_res = upload_blob(access_token, csv_path, csv_bytes, ctx)
    log(f"上傳 {md_path} …")
    md_res = upload_blob(access_token, md_path, md_bytes, ctx)

    success = []
    if csv_res:
        success.append(csv_res.get("path_display") or csv_path)
    if md_res:
        success.append(md_res.get("path_display") or md_path)

    if len(success) == 2:
        log("✅ 已同步至中央倉庫:")
        for p in success:
            log(f"   {p}")
        return 0
    elif success:
        warn("部分上傳成功；請通報 Ryan：")
        for p in success:
            log(f"   {p}")
        return 0
    else:
        warn("上傳全部失敗。本機 JSON 仍存於 " + str(stats_path))
        warn("可手動把該檔案傳給技術長 Ryan。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
