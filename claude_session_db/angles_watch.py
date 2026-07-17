"""angles_watch — headless miner that keeps the angles state dir warm.

A daemon that tails every live Claude Code transcript under ~/.claude/projects
and re-mines a session's latest turn (via angles.run_angles) whenever its JSONL
settles. Nothing is served; the only output is the angles state dir
(`$CSD_STATE_DIR/angles/<session_id>.json`), which any reader — `csd angles
show`, the session console — picks up off disk.

Two mechanisms carry the weight:

  settle-detection  a transcript is mined only once its (mtime_ns, size)
                    signature stops changing AND it has been quiet for
                    DEBOUNCE_S, so a turn is never mined mid-write.
  single worker     one mining thread drains the job queue, so N live sessions
                    can never stampede the local Ollama.

Doctrine (claudecode:design/turn-angles-context-cockpit): pull-not-push governs
the CONVERSATION surface. This daemon is the ambient exception — it costs zero
context tokens and interrupts nobody; it only makes the pull instant when the
operator asks for it.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from . import angles as A

SCAN_INTERVAL_S = 5           # transcript poll cadence
DEBOUNCE_S = 8                # file must be quiet this long before mining
DEFAULT_LIVE_WINDOW_S = 1800  # transcript mtime within this = live session


class AngleWatcher(threading.Thread):
    """Polls live transcripts; queues one mining job per settled change."""

    def __init__(self, window_s: int, model: str, base_url: str,
                 kmcp_dsn: Optional[str], no_probes: bool):
        super().__init__(daemon=True, name="angle-watcher")
        self.window_s = window_s
        self.model = model
        self.base_url = base_url
        self.kmcp_dsn = kmcp_dsn
        self.no_probes = no_probes
        self.mined_sig: dict[str, tuple[int, int]] = {}
        self.status: dict[str, str] = {}   # sid -> "mining" | "ok" | error text
        self.jobs: "queue.Queue[tuple[str, tuple[int, int]]]" = queue.Queue()
        self.queued: set[str] = set()
        self._worker = threading.Thread(target=self._work, daemon=True,
                                        name="angle-worker")

    # -- scan ------------------------------------------------------------
    def run(self) -> None:
        self._worker.start()
        while True:
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001 — watcher must survive
                self.status["_scan"] = f"{type(exc).__name__}: {exc}"
            time.sleep(SCAN_INTERVAL_S)

    def _scan_once(self) -> None:
        now = time.time()
        # Main transcripts + live sidechains (a running background child gets
        # its own mined store under the child key '<parent>:<agent_id>' — the
        # same address the archive and the console use).
        candidates = ((p, p.stem) for p in A.PROJECTS_DIR.glob("*/*.jsonl"))
        sub = ((p, A.subagent_key(p)) for p in A.PROJECTS_DIR.glob(
            "*/*/subagents/**/agent-*.jsonl"))
        for p, sid in (*candidates, *sub):
            if not sid:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if now - st.st_mtime > self.window_s:
                continue
            if now - st.st_mtime < DEBOUNCE_S:
                continue  # still being written; next scan will see it settled
            sig = (st.st_mtime_ns, st.st_size)
            if self.mined_sig.get(sid) == sig or sid in self.queued:
                continue
            self.queued.add(sid)
            self.jobs.put((sid, sig))

    # -- mine ------------------------------------------------------------
    def _work(self) -> None:
        while True:
            sid, sig = self.jobs.get()
            self.status[sid] = "mining"
            try:
                A.run_angles(cwd="", session_id=sid, turn=-1,
                             model=self.model, base_url=self.base_url,
                             kmcp_dsn=self.kmcp_dsn, no_probes=self.no_probes)
                self.mined_sig[sid] = sig
                self.status[sid] = "ok"
            except Exception as exc:  # noqa: BLE001 — one bad session ≠ dead worker
                self.status[sid] = f"{type(exc).__name__}: {exc}"
            finally:
                self.queued.discard(sid)


def run_watch(window_s: int, model: str, base_url: str,
              kmcp_dsn: Optional[str], no_probes: bool,
              echo: bool = True) -> None:
    """Start the watcher and block, echoing each mining outcome once."""
    watcher = AngleWatcher(window_s, model, base_url, kmcp_dsn, no_probes)
    watcher.start()
    seen: dict[str, str] = {}
    while True:
        if echo:
            for sid, status in list(watcher.status.items()):
                if seen.get(sid) != status:
                    seen[sid] = status
                    stamp = time.strftime("%H:%M:%S")
                    print(f"{stamp} {sid[:8]} {status}", flush=True)
        time.sleep(1)
