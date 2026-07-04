"""Liveness guard + heartbeat for the `csd sweep` launchd timer.

WHY THIS EXISTS
---------------
The sweep runs under launchd (`com.claude-session-db.sweep`, StartInterval 300s).
launchd serializes per label: it will not start a second copy while one is still
running. That prevents overlap, but it has a SILENT failure mode — if a run
*hangs* instead of finishing, launchd keeps deferring every tick to a process
that never exits, and the job still shows a live PID + last-exit 0. Observed once
as an ~9h silent starvation (see lesson launchd-per-label-hang-silent-starvation)
during which an abandoned idle-in-transaction sweep also convoyed the whole DB
(lesson csd-sweep-idle-in-transaction-lock-convoy).

This module provides the OUTER safety net, independent of the DB-side fixes
(idle_in_transaction_session_timeout, gated view DDL):

  * A LIVENESS GUARD — a pidfile carrying {pid, started_at}. A new sweep refuses
    to pile on while a *genuinely live, not-yet-stale* prior run holds the file;
    but a stale lock (dead PID, or alive-but-older-than max_age) is reclaimed so
    a crashed/wedged predecessor can never become a permanent block. This is the
    explicit fail-fast that launchd's per-label serialization lacks.

  * A HEARTBEAT / last-success marker — written on every successful sweep. A cheap
    watcher (`csd sweep-health`, or any mtime check) flags a heartbeat older than
    N intervals as a stall, so a hang is surfaced in seconds, not found by hand
    hours later.

Deliberately dependency-free and DB-free: the guard must work even when the
archive DB is the thing that is wedged.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

# Default state dir. Kept OUT of the repo and out of the DB so the guard survives
# a wedged archive. CSD_STATE_DIR (if set) is the EXACT dir; otherwise honor XDG /
# ~/.local/state and append the app subdir.
def _default_state_dir() -> Path:
    explicit = os.environ.get("CSD_STATE_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "claude-session-db"


_DEFAULT_STATE_DIR = _default_state_dir()

LOCK_NAME = "sweep.lock"
HEARTBEAT_NAME = "sweep.heartbeat"

# A sweep older than this is treated as wedged: its lock is reclaimable even if the
# PID is still alive. Comfortably longer than a healthy sweep (seconds to low
# minutes on this corpus) yet far below the multi-hour starvation we are guarding
# against. Tunable via env for slow/large backfills.
DEFAULT_MAX_AGE_S = int(os.environ.get("CSD_SWEEP_MAX_AGE_S", "900"))  # 15 min


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists and we may signal it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive (conservative).
        return True
    return True


@dataclass
class GuardResult:
    """Outcome of attempting to acquire the sweep guard."""

    acquired: bool
    reason: str  # human-readable; logged by the caller
    prior_pid: int | None = None
    prior_age_s: float | None = None
    reclaimed_stale: bool = False


class SweepGuard:
    """Pidfile-based liveness guard with an age/staleness check.

    Usage::

        guard = SweepGuard()
        res = guard.acquire()
        if not res.acquired:
            # a live, fresh prior run holds the lock — self-abort, do not pile on
            ...
            return
        try:
            ...do the sweep...
            guard.heartbeat(ok=True)
        finally:
            guard.release()
    """

    def __init__(self, state_dir: Path | None = None, max_age_s: int = DEFAULT_MAX_AGE_S,
                 lock_name: str = LOCK_NAME, heartbeat_name: str = HEARTBEAT_NAME):
        self.state_dir = Path(state_dir) if state_dir else _DEFAULT_STATE_DIR
        self.max_age_s = max_age_s
        self.lock_path = self.state_dir / lock_name
        self.heartbeat_path = self.state_dir / heartbeat_name
        self._held = False

    # -- lock ---------------------------------------------------------------

    def _read_lock(self) -> dict | None:
        try:
            return json.loads(self.lock_path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _write_lock(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "started_at": time.time()}
        # Write-then-rename so a reader never sees a half-written lock.
        tmp = self.lock_path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.lock_path)

    def acquire(self) -> GuardResult:
        """Try to claim the sweep lock.

        Refuses (acquired=False) only when a prior run is BOTH alive AND younger
        than max_age_s — the one case where piling on would create overlap. A
        missing, dead, or stale-but-alive lock is reclaimed so a crashed or wedged
        predecessor never becomes a permanent block (the failure mode called out
        in the task: "Do not introduce a lockfile without an age/staleness
        check").
        """
        prior = self._read_lock()
        if prior:
            pid = int(prior.get("pid", 0) or 0)
            started = float(prior.get("started_at", 0) or 0)
            age = time.time() - started if started else None
            alive = _pid_alive(pid)
            stale = age is not None and age > self.max_age_s
            if alive and not stale:
                return GuardResult(
                    acquired=False,
                    reason=(f"prior sweep still live (pid={pid}, "
                            f"age={age:.0f}s < max {self.max_age_s}s) — self-aborting"),
                    prior_pid=pid,
                    prior_age_s=age,
                )
            # Reclaim: dead PID, or alive-but-overrun (wedged). The DB-side
            # idle_in_transaction_session_timeout reaps any txn the wedged run
            # held; here we just stop it from blocking the schedule forever.
            self._write_lock()
            self._held = True
            why = "dead pid" if not alive else f"overrun (age={age:.0f}s > {self.max_age_s}s)"
            return GuardResult(
                acquired=True,
                reason=f"reclaimed stale lock from pid={pid} ({why})",
                prior_pid=pid,
                prior_age_s=age,
                reclaimed_stale=True,
            )
        self._write_lock()
        self._held = True
        return GuardResult(acquired=True, reason="acquired (no prior lock)")

    def release(self) -> None:
        """Release the lock iff we hold it (never delete a successor's lock)."""
        if not self._held:
            return
        try:
            cur = self._read_lock()
            if cur and int(cur.get("pid", -1)) == os.getpid():
                self.lock_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        self._held = False

    # -- heartbeat ----------------------------------------------------------

    def heartbeat(self, ok: bool, detail: str = "") -> None:
        """Record a run outcome. mtime is the staleness signal; `ok` separates a
        successful sweep from a run that completed-but-errored."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "pid": os.getpid(),
            "ok": bool(ok),
            "detail": detail[:500],
        }
        tmp = self.heartbeat_path.with_suffix(".heartbeat.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self.heartbeat_path)

    def read_heartbeat(self) -> dict | None:
        try:
            return json.loads(self.heartbeat_path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def staleness(self) -> tuple[float | None, dict | None]:
        """(age_seconds_since_last_heartbeat, heartbeat_payload). age is None if
        no heartbeat has ever been written."""
        hb = self.read_heartbeat()
        if not hb or "ts" not in hb:
            return None, hb
        return time.time() - float(hb["ts"]), hb
