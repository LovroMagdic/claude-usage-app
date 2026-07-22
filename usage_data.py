from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from config import config, app_dir

# --- constants -------------------------------------------------------------

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
CREDENTIALS_PATH = os.path.join(CLAUDE_DIR, ".credentials.json")
PROJECTS_GLOB = os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")

# where we cache the last successful limits response (survives 429s & restarts)
CACHE_PATH = str(app_dir() / ".usage_cache.json")

# The /usage endpoint is rate-limited, so we call it at most this often and
# back off on 429. In between, the last-good cached value is returned.
# All of these come from the environment via config.py (see .env.example).
MIN_LIMITS_INTERVAL = config.MIN_LIMITS_INTERVAL   # seconds between endpoint calls
_BACKOFF_BASE = config.BACKOFF_BASE                # first 429 backoff
_BACKOFF_MAX = config.BACKOFF_MAX

_last_call_ts = 0.0
_backoff_until = 0.0
_backoff_step = _BACKOFF_BASE

USAGE_URL = config.USAGE_URL
TOKEN_URL = config.TOKEN_URL
OAUTH_BETA = config.OAUTH_BETA
CLIENT_ID = config.CLIENT_ID

# Approximate public API pricing, USD per token. Subscription users are not
# billed per token, so this is an "equivalent API cost" estimate.
#   input, output, cache_write (5m), cache_read
_PRICING = {
    "opus":   (15e-6, 75e-6, 18.75e-6, 1.5e-6),
    "sonnet": (3e-6,  15e-6, 3.75e-6,  0.3e-6),
    "haiku":  (0.8e-6, 4e-6, 1.0e-6,   0.08e-6),
    "fable":  (3e-6,  15e-6, 3.75e-6,  0.3e-6),
}


def _price_for(model: str):
    m = (model or "").lower()
    for key in _PRICING:
        if key in m:
            return _PRICING[key]
    return _PRICING["sonnet"]  # sensible default


# --- credentials / oauth ---------------------------------------------------

def _load_oauth() -> dict:
    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("claudeAiOauth", data)


def _save_oauth(oauth: dict) -> None:
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if "claudeAiOauth" in data:
        data["claudeAiOauth"] = oauth
    else:
        data = oauth
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CREDENTIALS_PATH)


def _refresh_token(oauth: dict) -> dict | None:
    """Exchange the refresh token for a fresh access token. Returns updated
    oauth dict on success, or None on failure (caller degrades gracefully)."""
    refresh = oauth.get("refreshToken") or oauth.get("refresh_token")
    if not refresh:
        return None
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "claude-cli"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
    except Exception:
        return None
    access = tok.get("access_token")
    if not access:
        return None
    oauth["accessToken"] = access
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + tok["expires_in"]) * 1000)
    _save_oauth(oauth)
    return oauth


def _access_token(oauth: dict, force_refresh: bool = False) -> str | None:
    exp = oauth.get("expiresAt") or 0
    near_expiry = exp and (exp / 1000 - time.time()) < 60
    if force_refresh or near_expiry:
        refreshed = _refresh_token(oauth)
        if refreshed:
            oauth = refreshed
    return oauth.get("accessToken") or oauth.get("access_token")


# --- source 1: subscription limits ----------------------------------------

def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(limits: dict):
    try:
        data = dict(limits)
        data["cached_at"] = time.time()
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _cached_result(note: str):
    """Return the last-good limits. If the cache is still fresh (younger than
    one poll interval) an intermittent failure is invisible — the numbers are
    effectively current, so no note is shown. Only genuinely old data gets a
    'showing data from Nm ago' note."""
    c = _load_cache()
    if c:
        age = int(time.time() - c.get("cached_at", 0))
        c = dict(c)
        if age < MIN_LIMITS_INTERVAL:
            c["stale"] = False
            c["error"] = None
        else:
            c["stale"] = True
            c["error"] = f"{note} · showing data from {age // 60}m ago"
        return c
    return {"ok": False, "error": note, "stale": True,
            "session": None, "weekly": None, "weekly_scoped": []}


def fetch_limits(force: bool = False) -> dict:
    """Return {'ok': bool, 'error': str|None, 'session': {...}, 'weekly': {...},
    'weekly_scoped': [...], 'stale': bool}.  Percentages are 0-100 floats.

    Rate-limit aware: hits the endpoint at most every MIN_LIMITS_INTERVAL
    seconds (unless `force`), backs off on 429, and otherwise returns the
    last cached value so the popup always has something to show."""
    global _last_call_ts, _backoff_until, _backoff_step

    now = time.time()
    # A manual/forced refresh bypasses both the soft throttle AND the 429
    # backoff — the user explicitly asked for fresh data, so try the call.
    if not force:
        if now < _backoff_until:
            return _cached_result("Rate limited")
        if now - _last_call_ts < MIN_LIMITS_INTERVAL:
            c = _load_cache()
            if c:
                c = dict(c); c["stale"] = False; c["error"] = None
                return c

    try:
        oauth = _load_oauth()
    except Exception as e:
        return {"ok": False, "error": f"No credentials: {e}", "stale": False}

    def _call(token):
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
            "User-Agent": "claude-cli",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    token = _access_token(oauth)
    if not token:
        return {"ok": False, "error": "No access token", "stale": False}

    _last_call_ts = time.time()
    try:
        data = _call(token)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _backoff_until = time.time() + _backoff_step
            _backoff_step = min(_backoff_step * 2, _BACKOFF_MAX)
            return _cached_result("Rate limited")
        if e.code in (401, 403):
            token = _access_token(oauth, force_refresh=True)
            if not token:
                return _cached_result("Auth expired — run `claude` once")
            try:
                data = _call(token)
            except Exception:
                return _cached_result("Auth error")
        else:
            return _cached_result(f"HTTP {e.code}")
    except Exception as e:
        return _cached_result(f"{type(e).__name__}")

    # success -> reset backoff
    _backoff_until = 0.0
    _backoff_step = _BACKOFF_BASE

    def block(d):
        if not isinstance(d, dict):
            return None
        return {
            "percent": d.get("utilization"),
            "resets_at": d.get("resets_at"),
        }

    def money(m):
        """A {'amount_minor', 'currency', 'exponent'} money object -> float in
        major units (e.g. {'amount_minor': 1234, 'exponent': 2} -> 12.34)."""
        if not isinstance(m, dict):
            return None
        try:
            exp = m.get("exponent", 2)
            return m.get("amount_minor", 0) / (10 ** exp)
        except Exception:
            return None

    # usage credits (extra usage) — spend against a monthly $ limit
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else None
    credit = None
    if spend:
        used, limit = spend.get("used"), spend.get("limit")
        cap_money = (spend.get("cap") or {}).get("money")
        cur = ((used or {}).get("currency")
               or (limit or {}).get("currency")
               or (cap_money or {}).get("currency") or "")
        limit_val = money(limit)
        if limit_val is None:
            limit_val = money(cap_money)
        enabled = bool(spend.get("enabled"))
        credit = {
            "enabled": enabled,
            "spent": money(used),
            "limit": limit_val,
            # enabled with no monetary cap anywhere => spending is uncapped
            "unlimited": enabled and limit_val is None,
            "balance": money(spend.get("balance")),
            "percent": spend.get("percent"),
            "currency": cur,
            "disabled_reason": spend.get("disabled_reason"),
        }

    scoped = []
    for lim in data.get("limits") or []:
        if lim.get("kind") == "weekly_scoped" and lim.get("scope"):
            name = ((lim["scope"].get("model") or {}).get("display_name")) or "scoped"
            scoped.append({
                "name": name,
                "percent": lim.get("percent"),
                "resets_at": lim.get("resets_at"),
            })

    result = {
        "ok": True,
        "error": None,
        "stale": False,
        "session": block(data.get("five_hour")),
        "weekly": block(data.get("seven_day")),
        "weekly_scoped": scoped,
        "credit": credit,
        "plan": oauth.get("subscriptionType"),
    }
    _save_cache(result)
    return result


# --- source 2: token / cost from transcripts -------------------------------

def _parse_ts(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_token_usage() -> dict:
    """Aggregate token counts + estimated cost from local JSONL transcripts.
    Buckets: today, this month, all-time. Deduped by request/message id."""
    now = datetime.now(timezone.utc).astimezone()
    today = now.date()
    month_start = today.replace(day=1)

    buckets = {
        "today":  {"in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "msgs": 0},
        "month":  {"in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "msgs": 0},
        "total":  {"in": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "msgs": 0},
    }
    seen = set()

    for fp in glob.glob(PROJECTS_GLOB):
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    uid = obj.get("requestId") or msg.get("id") or obj.get("uuid")
                    if uid in seen:
                        continue
                    seen.add(uid)

                    ip = usage.get("input_tokens", 0) or 0
                    op = usage.get("output_tokens", 0) or 0
                    cw = usage.get("cache_creation_input_tokens", 0) or 0
                    cr = usage.get("cache_read_input_tokens", 0) or 0
                    p_in, p_out, p_cw, p_cr = _price_for(msg.get("model"))
                    cost = ip * p_in + op * p_out + cw * p_cw + cr * p_cr

                    dt = _parse_ts(obj.get("timestamp"))
                    day = dt.astimezone().date() if dt else None

                    targets = ["total"]
                    if day == today:
                        targets.append("today")
                    if day and day >= month_start:
                        targets.append("month")
                    for t in targets:
                        b = buckets[t]
                        b["in"] += ip; b["out"] += op
                        b["cw"] += cw; b["cr"] += cr
                        b["cost"] += cost; b["msgs"] += 1
        except Exception:
            continue

    return buckets


def fetch_all(force: bool = False) -> dict:
    return {"limits": fetch_limits(force=force), "tokens": fetch_token_usage()}


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_all())
