"""csd usage — dual-account Claude Max quota reporter + safe account switcher.

Live quota is read from the same OAuth endpoints Claude Code's own `/usage`
command uses:

    refresh:  POST https://platform.claude.com/v1/oauth/token   (grant_type=refresh_token)
    usage:    GET  https://api.anthropic.com/api/oauth/usage     (Bearer)
    profile:  GET  https://api.anthropic.com/api/oauth/profile   (Bearer)

The refresh grant uses Claude Code's public OAuth client_id. The refresh
*response* itself carries the account email + organization, so accounts
self-label — no separate lookup is needed to name them.

**One-account-at-a-time constraint.** Only the currently logged-in account is
authenticated in the macOS keychain (`Claude Code-credentials`, the authoritative
store on macOS) mirrored to `~/.claude/.credentials.json`. To report *both*
accounts we vault each account's refresh token (0600) under the state dir.
Anthropic **rotates the refresh token on every use**, so the vault is rewritten
after each refresh; because the operator re-authenticates on every account swap,
this rotation is harmless. `csd usage use <label>` writes a vaulted account's
freshly-refreshed creds back into the keychain + mirror file, replacing the
interactive `/login` swap ritual.

Local per-account token/cost is *not* attributable — transcripts carry no account
identity — so the Postgres cost figure is reported as a commingled aggregate.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── OAuth constants (extracted from the Claude Code 2.1.202 binary) ────────────
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code public OAuth client
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-cli/2.1.202 (external, cli)"

# ── credential stores ─────────────────────────────────────────────────────────
KEYCHAIN_SERVICE = "Claude Code-credentials"
LIVE_CRED_FILE = Path.home() / ".claude" / ".credentials.json"

# Friendly plan labels for the org rate_limit_tier string.
TIER_LABELS = {
    "default_claude_max_20x": "Max 20×",
    "default_claude_max_5x": "Max 5×",
    "default_claude_pro": "Pro",
}


class UsageError(RuntimeError):
    """Any recoverable failure in the usage flow (bad grant, HTTP error, no vault)."""


# ── state dir / vault ─────────────────────────────────────────────────────────
def _state_dir() -> Path:
    """CSD_STATE_DIR is the exact dir; else XDG_STATE_HOME/~/.local/state + app subdir.

    Mirrors the convention in sweepguard/summarize so the whole tool shares one
    state root.
    """
    explicit = os.environ.get("CSD_STATE_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "claude-session-db"


def _vault_path() -> Path:
    return _state_dir() / "usage-accounts.json"


def load_vault() -> dict:
    """Return the vault dict ``{"accounts": [ {...}, ... ]}`` (empty if absent)."""
    p = _vault_path()
    if not p.is_file():
        return {"accounts": []}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise UsageError(f"vault unreadable ({p}): {e}") from e
    data.setdefault("accounts", [])
    return data


def save_vault(vault: dict) -> None:
    p = _vault_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(vault, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _find_account(vault: dict, label: str) -> dict | None:
    for acct in vault["accounts"]:
        if acct.get("label") == label or acct.get("email") == label:
            return acct
    return None


# ── HTTP (stdlib only, matching the repo's no-dep ethos) ──────────────────────
def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, access_token: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def refresh(refresh_token: str) -> dict:
    """Exchange a refresh token for fresh creds.

    Returns the raw token response (``access_token``, rotated ``refresh_token``,
    ``expires_in``, ``account``, ``organization`` …). Raises UsageError on an
    invalid/expired grant — the caller should prompt for re-capture.
    """
    try:
        return _post_json(TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code in (400, 401):
            raise UsageError(
                f"refresh rejected ({e.code}) — the stored refresh token is stale "
                f"(rotated by a Claude Code login?). Re-capture with "
                f"`csd usage add-account`. Server said: {body}"
            ) from e
        raise UsageError(f"token endpoint HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise UsageError(f"cannot reach {TOKEN_URL}: {e.reason}") from e


def fetch_usage(access_token: str) -> dict:
    try:
        return _get_json(USAGE_URL, access_token)
    except urllib.error.HTTPError as e:
        raise UsageError(f"usage endpoint HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from e


def fetch_profile(access_token: str) -> dict:
    try:
        return _get_json(PROFILE_URL, access_token)
    except urllib.error.HTTPError as e:
        raise UsageError(f"profile endpoint HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from e


# ── live credential store (keychain authoritative, file mirror) ───────────────
def read_live_oauth() -> dict | None:
    """Return the currently-active account's ``claudeAiOauth`` blob, or None.

    Keychain wins (authoritative on macOS); falls back to the mirror file.
    """
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode()
        blob = json.loads(raw)
        oauth = blob.get("claudeAiOauth")
        if oauth:
            return oauth
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        pass
    if LIVE_CRED_FILE.is_file():
        try:
            return json.loads(LIVE_CRED_FILE.read_text()).get("claudeAiOauth")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _keychain_account() -> str | None:
    """The keychain generic-password account (the `-a` value), or None."""
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('"acct"'):
            # e.g.  "acct"<blob>="andrew"
            _, _, val = line.partition("=")
            return val.strip().strip('"')
    return None


def write_live_oauth(oauth: dict) -> list[str]:
    """Write a ``claudeAiOauth`` blob into both stores. Returns which stores changed.

    Keychain: replace ``claudeAiOauth`` in the existing payload, preserving any
    ``mcpOAuth`` sibling. File: write the mirror. Best-effort per store.
    """
    changed: list[str] = []
    acct = _keychain_account()
    if acct:
        try:
            existing_raw = subprocess.check_output(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", acct, "-w"],
                stderr=subprocess.DEVNULL,
            ).decode()
            blob = json.loads(existing_raw)
        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
            blob = {}
        blob["claudeAiOauth"] = oauth
        try:
            subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-s", KEYCHAIN_SERVICE, "-a", acct, "-w", json.dumps(blob)],
                check=True, stderr=subprocess.DEVNULL,
            )
            changed.append("keychain")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    if LIVE_CRED_FILE.is_file() or not changed:
        try:
            LIVE_CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
            LIVE_CRED_FILE.write_text(json.dumps({"claudeAiOauth": oauth}))
            os.chmod(LIVE_CRED_FILE, 0o600)
            changed.append("file")
        except OSError:
            pass
    return changed


# ── vault upsert from a refresh response ──────────────────────────────────────
def _oauth_from_token_response(tok: dict, prev: dict | None = None) -> dict:
    """Build a ``claudeAiOauth``-shaped blob from a token endpoint response."""
    prev = prev or {}
    return {
        "accessToken": tok["access_token"],
        "refreshToken": tok["refresh_token"],
        "expiresAt": int((time.time() + tok.get("expires_in", 28800)) * 1000),
        "scopes": (tok.get("scope") or "").split() or prev.get("scopes", []),
        "subscriptionType": prev.get("subscriptionType", "max"),
        "rateLimitTier": prev.get("rateLimitTier", ""),
    }


def upsert_from_refresh(vault: dict, tok: dict, label: str | None = None,
                        tier: str | None = None) -> dict:
    """Insert/update the vault account described by a refresh response. Returns it."""
    email = (tok.get("account") or {}).get("email_address")
    acct = None
    if email:
        acct = next((a for a in vault["accounts"] if a.get("email") == email), None)
    if acct is None and label:
        acct = _find_account(vault, label)
    if acct is None:
        acct = {}
        vault["accounts"].append(acct)
    acct["label"] = label or acct.get("label") or (email.split("@")[0] if email else "account")
    acct["email"] = email or acct.get("email")
    acct["account_uuid"] = (tok.get("account") or {}).get("uuid", acct.get("account_uuid"))
    org = tok.get("organization") or {}
    acct["org_uuid"] = org.get("uuid", acct.get("org_uuid"))
    acct["org_name"] = org.get("name", acct.get("org_name"))
    acct["refresh_token"] = tok["refresh_token"]
    acct["expires_at"] = int((time.time() + tok.get("expires_in", 28800)) * 1000)
    acct["last_refresh"] = int(time.time())
    if tier:
        acct["rate_limit_tier"] = tier
    return acct


# ── rendering helpers ─────────────────────────────────────────────────────────
def _fmt_reset(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    when = dt.astimezone().strftime("%a %H:%M")
    return f"{when} (in {h}h{m:02d}m)" if h else f"{when} (in {m}m)"


def _bar(pct: float, width: int = 16) -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


_SEVERITY_MARK = {"normal": "", "warning": " ⚠", "critical": " ‼", "exceeded": " ⛔"}


def summarize_usage(usage: dict) -> dict:
    """Reduce the raw /usage payload to the fields we display.

    Prefers the structured ``limits[]`` list; falls back to the flat
    ``five_hour`` / ``seven_day`` objects.
    """
    out: dict = {"five_hour": None, "weekly": None, "scoped": [], "severity": "normal"}
    limits = usage.get("limits") or []
    worst = "normal"
    sev_rank = {"normal": 0, "warning": 1, "critical": 2, "exceeded": 3}
    for lim in limits:
        entry = {
            "percent": lim.get("percent"),
            "resets_at": lim.get("resets_at"),
            "severity": lim.get("severity", "normal"),
        }
        if sev_rank.get(lim.get("severity", "normal"), 0) > sev_rank.get(worst, 0):
            worst = lim.get("severity", "normal")
        kind = lim.get("kind")
        if kind == "session":
            out["five_hour"] = entry
        elif kind == "weekly_all":
            out["weekly"] = entry
        elif kind == "weekly_scoped":
            scope = lim.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            entry["name"] = model or "scoped"
            out["scoped"].append(entry)
    # Fallbacks from the flat objects.
    if out["five_hour"] is None and usage.get("five_hour"):
        fh = usage["five_hour"]
        out["five_hour"] = {"percent": fh.get("utilization"), "resets_at": fh.get("resets_at"),
                            "severity": "normal"}
    if out["weekly"] is None and usage.get("seven_day"):
        sd = usage["seven_day"]
        out["weekly"] = {"percent": sd.get("utilization"), "resets_at": sd.get("resets_at"),
                         "severity": "normal"}
    out["severity"] = worst
    return out


def poll(refresh_token: str, vault: dict, *, is_active: bool,
         label: str | None = None, known_tier: str | None = None) -> dict:
    """Refresh one account, fetch its usage, persist the rotated token to the vault.

    Identity (email/org/tier) is derived from the refresh response, so the caller
    need not know it in advance. For the active account the rotated creds are also
    written back into the live keychain/file so those never desync. Returns a
    report row (carries ``error`` on failure, still identifying the account when
    it can).
    """
    row: dict = {"label": label, "active": is_active, "tier": known_tier}
    try:
        tok = refresh(refresh_token)
    except UsageError as e:
        row["error"] = str(e)
        return row
    row["email"] = (tok.get("account") or {}).get("email_address")
    row["org_name"] = (tok.get("organization") or {}).get("name")
    tier = known_tier
    if not tier:
        try:
            tier = ((fetch_profile(tok["access_token"]).get("organization") or {})
                    .get("rate_limit_tier"))
        except UsageError:
            tier = None
    acct = upsert_from_refresh(vault, tok, label=label, tier=tier)
    row["label"] = acct.get("label")
    row["tier"] = tier
    if is_active:
        prev = read_live_oauth() or {}
        write_live_oauth(_oauth_from_token_response(tok, prev))
    try:
        row["usage"] = summarize_usage(fetch_usage(tok["access_token"]))
    except UsageError as e:
        row["error"] = str(e)
    return row


def local_aggregate_cost(dsn: str) -> dict | None:
    """Commingled (all-accounts) local token cost from the Postgres archive.

    Returns 7-day and today USD totals, or None if the archive is unreachable.
    Per-account attribution is impossible — transcripts carry no account id.
    """
    try:
        from .postgres import SessionArchive
        with SessionArchive(dsn) as a:
            rows = a.query(
                "SELECT "
                "  coalesce(sum(total_cost) FILTER (WHERE day >= now()::date - 6), 0) AS wk, "
                "  coalesce(sum(total_cost) FILTER (WHERE day = now()::date), 0) AS today "
                "FROM v_token_cost_daily"
            )
        if rows:
            return {"week": float(rows[0]["wk"] or 0), "today": float(rows[0]["today"] or 0)}
    except Exception:  # DB down / view missing — the live report must still render
        return None
    return None
