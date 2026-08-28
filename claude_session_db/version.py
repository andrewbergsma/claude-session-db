"""csd version identity — one canonical version, plus running-vs-disk staleness.

Source of truth
---------------
`claude_session_db.__version__` (in `__init__.py`) is THE version. pyproject
declares `dynamic = ["version"]` and hatch reads that same attribute at build
time, so there is no second number to keep in sync and no dependency on
`importlib.metadata` (an editable install caches its metadata at install time
and would happily report a version the code on disk no longer has).

Why a running-vs-disk comparison
--------------------------------
The console is long-lived and usually launchd-respawned: it keeps executing the
bytes it was started with, while `git pull` / an edit moves the code on disk
underneath it. That gap has bitten repeatedly ("the fix is in but the console
doesn't have it"). So the process captures its identity ONCE (`capture_running`,
at server start), and `disk_state()` re-reads the repo HEAD and the on-disk
`__version__` (cached ~60s) — the two together let the UI say
"restart to update: running abc1234, disk def5678".

Everything here degrades: no git, no repo, a git timeout → the sha is simply
None and staleness is unknown. Nothing raises.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from . import __version__

VERSION = __version__

# repo root = the parent of the package directory (…/claude-session-db)
REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

GIT_TIMEOUT_S = 3.0
DISK_CACHE_S = 60.0             # how long a HEAD read is reused


def _git(*args: str) -> str | None:
    """Read-only git in the repo root; None on any failure (never raises)."""
    try:
        p = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip() or None


def head_sha() -> str | None:
    """Short sha of the repo's current HEAD (what is on DISK right now)."""
    return _git("rev-parse", "--short", "HEAD")


def head_dirty() -> bool | None:
    """True if the working tree has uncommitted changes; None if unknown."""
    out = _git("status", "--porcelain", "--untracked-files=no")
    if out is None:
        return None
    return bool(out)


def file_version() -> str | None:
    """`__version__` as it reads ON DISK right now.

    Deliberately re-parsed from the source file rather than taken from the
    imported module: the whole point is to see a version the running process
    has NOT loaded.
    """
    try:
        src = (REPO_ROOT / "claude_session_db" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
    return m.group(1) if m else None


def _snapshot(from_disk: bool = False) -> dict:
    """Identity triple. `from_disk` re-reads the version off the source file;
    otherwise it is the version this process actually imported."""
    return {"version": (file_version() or VERSION) if from_disk else VERSION,
            "sha": head_sha(), "dirty": head_dirty()}


# The identity of the code THIS process is running. Captured once, on the
# first call, and never re-read — re-reading would make the staleness check a
# tautology. Lazy rather than import-time so a plain `csd ingest` never pays
# for a git subprocess it doesn't use; the console calls it at startup so the
# snapshot is genuinely "what we booted with".
RUNNING: dict = {}


def capture_running() -> dict:
    """Snapshot the running code's identity (idempotent — first call wins)."""
    if not RUNNING:
        RUNNING.update(_snapshot(), started_at=time.time())
    return RUNNING


_disk_cache: dict = {"at": 0.0, "val": None}


def disk_state(max_age_s: float = DISK_CACHE_S) -> dict:
    """What is on disk NOW (cached ~60s so a poll can call this freely)."""
    now = time.time()
    if _disk_cache["val"] is not None and now - _disk_cache["at"] < max_age_s:
        return _disk_cache["val"]
    val = _snapshot(from_disk=True)
    _disk_cache.update(at=now, val=val)
    return val


def version_report(max_age_s: float = DISK_CACHE_S) -> dict:
    """Running identity + disk identity + a verdict, for /api/version.

    `stale` is True only when both shas are known AND differ — an unknown sha
    (no git, not a checkout) reports False, never a false alarm.
    """
    run = capture_running()
    disk = disk_state(max_age_s)
    run_sha, disk_sha = run.get("sha"), disk.get("sha")
    stale = bool(run_sha and disk_sha and run_sha != disk_sha)
    return {
        "version": run["version"],
        "sha": run_sha,
        "dirty": run.get("dirty"),
        "started_at": run["started_at"],
        "uptime_s": time.time() - run["started_at"],
        "disk_version": disk.get("version"),
        "disk_sha": disk_sha,
        "disk_dirty": disk.get("dirty"),
        "stale": stale,
    }


def changelog_text() -> str | None:
    """CHANGELOG.md as text, or None when it isn't there / isn't readable."""
    try:
        return CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        return None
