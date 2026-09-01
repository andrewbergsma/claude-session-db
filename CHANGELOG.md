# Changelog

All notable changes to `csd` (claude-session-db). Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[Semantic Versioning](https://semver.org/).

**Bump discipline.** The version lives in exactly one place —
`claude_session_db/__init__.py:__version__` — and everything else reads it
(pyproject via hatch `dynamic`, `csd --version`, the console's version chip).
Bump it **and add the changelog entry in the same commit as the change**: a
**minor** for a feature batch, a **patch** for fixes/perf/docs, a **major** for
an archive generation or a breaking CLI/schema change. The console shows the
running version *and* the running git sha against the repo's HEAD, so an
un-bumped feature and a stale process are both visible at a glance — click the
chip in the sidebar footer to read this file in the UI.

**Versioning history.** The major tracks the archive generation: Gen1/Gen2 were
the retired SQLite era, and `csd` has been the Postgres (Gen3) front-end since
2026-06-01 — hence the 3.x line. Releases before 3.9.0 are backfilled from git
history and dated by their last commit.

## [3.11.0] - 2026-09-01

### Added
- **Repos overlay — the cross-repo lens.** `⎇ repos` beside `⊞ threads`: the
  same inventory shape on the other axis. Threads answers *which sessions are
  still open*; this answers *which repos are*. One card per repository — trunk
  and how it resolved, unpushed/behind, dirty/untracked/stashed, unmerged
  branches with ahead/behind, live worktrees (stale registrations flagged), and
  the last commit.
- Console `GET /api/repos` — cached-first and **never a fan-out on the request
  path**. One repo snapshot is ~9 git invocations, so a 20-repo grid on a 30s
  poll would be ~180 subprocesses a tick; a single background walker
  (`_repos_refresher`, `CSD_REPOS_REFRESH_S`, default 90s, serial + staggered)
  keeps `$CSD_STATE_DIR/console/repos.json` warm and the endpoint reads it off
  disk. Same seam as tldr/timeline.
- Console `POST /api/repos/refresh` — the ⟳ button; the only forced walk, and
  it re-discovers the registry so a new repo needs no console restart.
- Registry **discovery**: distinct `sessions.cwd` from the archive
  (`CSD_REPOS_WINDOW_DAYS`, 30) resolved through `rev-parse --show-toplevel`,
  with `CSD_REPOS` for pinned roots. No database degrades to the transcript-tail
  cwd derivation the nav already uses, so the lens works DB-free.
- `tests/test_repos_lens.py` — 13 tests over real temp repositories (a git
  reader stubbed out of git tests nothing): worktree folding, `master` trunk
  probing, ahead/behind, stale worktree registrations, per-row degradation, and
  a payload test that asserts `repos_payload()` never invokes git.

### Notes
- **Read-only, and never a fetch.** Every call goes through `_git()`
  (`--no-optional-locks`); ahead/behind is measured against the refs already on
  disk, and the header says so rather than implying live remote truth.
- A linked **worktree folds into its parent** repo rather than standing as its
  own row — un-folded, `controltech` and its `receive-packing-slip-cli` worktree
  both claimed the same 22 branches.
- The **attention band is narrow by design**: tracked modifications, an unpushed
  trunk, or a worktree whose folder is gone. Banding on untracked files and old
  unmerged branches too put 16 of 17 rows in the band.

## [3.10.0] - 2026-08-28

### Added
- Console chat pane renders markdown, like Claude Code: `mdLite` extended with
  fenced code blocks (with language label), ordered lists, blockquotes,
  `~~del~~`, autolinked URLs, and h1-h6 headings; applied to user/assistant/
  queued/optimistic turns via a memoized `mdText`. Escape-first, so raw HTML in
  transcripts stays inert; `__dunder__` / `mcp__*` identifiers deliberately
  never trigger emphasis. Changelog overlay shares the same renderer.

## [3.9.0] - 2026-08-28

### Added
- Versioning system: one canonical `__version__` in the package, consumed by
  pyproject (hatch `dynamic`), `csd --version` / `csd -V`, and the console.
- `CHANGELOG.md` (this file), backfilled from git history, with the bump
  discipline documented in its header.
- Console `GET /api/version` — running version + git sha captured at server
  start, compared against the repo's HEAD on disk (cached ~60s) so the UI can
  say *"restart to update: running abc1234, disk def5678"*. The console is
  usually launchd-respawned and keeps executing the bytes it booted with; that
  gap has bitten repeatedly and is now visible.
- Console `GET /api/changelog` — serves this file's markdown.
- Console version chip in the sidebar footer, with an amber staleness dot; the
  chip opens a changelog overlay (minimal client-side markdown, no libraries).

## [3.8.0] - 2026-08-27

### Added
- Delta summarize, Stage 2: pass-aware Summarize in the console — watermark
  scope, ledger claim, and an idle warning before dispatch.

## [3.7.0] - 2026-08-21

### Added
- Context reduction (CR): the engine (manifest, validated redaction, preamble,
  fork forge), its console endpoints (manifest, two-phase fork, kmcp
  search/compile), and the CR mode UI — manifest verbs on the stream, cart
  rail, two-phase preview.
- Repeatable delta summarization: pass ledger, delta gate, since-mode digest.
- In-process ambient miner — angles/tldr/timeline kept warm for active sessions
  (the console hosts it; `csd angles-watch` is no longer a separate must-run).
- Summarize / summarize+archive / archive actions in the digest reader.
- tl;dr and timeline presence at the sidebar row, with a reader and proposed
  titles; tl;dr for all sessions and from the sidebar.
- Optimistic render of sent messages — a ⏳ sending badge until the transcript
  catches up.
- Copy-path button on Writes-tab file rows.
- Sidebar search matches session ids (with a disk fallback); angles rail
  subtabs plus rail cleanup.
- Side-session envelope translates `harness_hints.model` → `--model`.
- Mobile chat pass: compact header, full-width messages, keyboard-safe
  composer, reliable rail.

### Changed
- Timeline `num_ctx` 4096 → 8192 to match tldr/angles, killing the ~2s Ollama
  model reload on every consumer alternation.

### Fixed
- 24h HH:MM in Timeline rows; tl;dr row added to the Mine menu.
- Reconcile's collision guard no longer poisons summary watermarks.

## [3.6.0] - 2026-08-14

### Added
- Files rail tab: read-only `/api/files` + `/api/file` endpoints
  (root-confined), a lazy tree with git badges, and a preview overlay.
- Side-session permission envelopes resolved from kmcp — a declared skill
  translated into `--allowedTools` / `--add-dir` / `--max-turns` /
  `--append-system-prompt`, degrading to zero flags rather than blocking.
- Batch ops over many sessions, plus visible and restart-safe timeline
  generation.
- Flat most-recent-first sidebar mode.
- Mine-angles menu — per-angle mining.
- Two-line sidebar session rows with readable titles.

### Fixed
- Folder taps keep the mobile drawer open.

## [3.5.0] - 2026-07-25

### Added
- Reply queue: Answer never refuses on the two-writer guard — it queues and
  auto-dispatches (persisted FIFO, restart-safe).
- Sidebar UX rework: project accordion, sortable columns, content search
  (prototyped as mocks first: priority/idle columns, collapsible sidebar,
  inline title edit, your-move vs waiting split).
- CLAUDE.md memory files on the Context rail tab.
- Deployed/served URLs group in the right rail.
- Whole-session tl;dr timeline, promoted into the right rail as a Timeline tab.
- 4-state activity classifier (working / waiting / idle / stale).
- Titled sessions, sortable sidebar columns, and a topic taxonomy.
- Expandable full command + live running status on Bash rows.
- PR membership on commits, full PR status in the Git tab, and an ≈#N
  equivalence badge for commits whose change shipped via a cherry-picked PR.

### Changed
- Mobile + UX overhaul across every console surface.
- Removed the two-writer warning banner (the queue replaced it).

### Fixed
- The console mints a fork's session id instead of losing it.
- "Summarize + archive" reports its outcome.
- Answer no longer fails with a masking `JSON.parse SyntaxError`.
- `Cache-Control: no-cache` so a reload never serves stale JS.

## [3.4.0] - 2026-07-17

### Added
- Subagent (sidechain) visibility: child session rows + agent_id index +
  one-shot `csd backfill-subagents`, the `v_agent_children` spawn ledger, an
  own-vs-rollup aggregate split, and subagent navigation (agents angle, child
  refs, drill-down).
- Volatile background-task `.output` files swept into the archive at sync time.
- Session-management lens (`csd angles sessions`) — open-thread inventory with
  delta-after-summary classification; head/tail/since windowing in
  `session_digest.render`.
- Console: sidebar UX rework + tabbed angle rail (priority store, nav stats),
  Git tab, tl;dr angle (last-3-turns catch-up), live-run status ticker.
- Session lens and subagent navigation absorbed into the console.

### Removed
- **Breaking:** `angles_web.py` / `csd angles-serve`. The console is the single
  web UI; the watcher (`angles_watch.py`) lives on.

### Fixed
- Answer gating derived from process reality, not transcript shape.
- Batch kmcp reads identify their entries instead of showing `?`.
- Sidebar flattened to one grouping level with real project names.
- The whole transcript is scanned for the first timestamp.
- `v_session_overview` must DROP+CREATE — its column list grew.

## [3.3.0] - 2026-07-11

### Added
- The reply-capable session console (`csd console`): full-screen chat, inline
  kmcp reads, the angles gems folded in, and the stop / archive / summarize
  action-vocabulary.
- Token auth for the console, so it can bind the LAN.
- Turn angles (`csd angles`) — pull-based per-turn capture (P1 spike).
- Phase-4 roll-up (`csd summarize`) — automated digest → Ollama → kmcp session
  entry, with lock, heartbeat, backoff ledger and quiesce gate.
- Dual-account Claude Max quota report (`csd usage`) plus a rotation-safe
  account switcher.

### Fixed
- Summarize+archive runs an independent off-session summary (never a resume).
- Tool-heavy transcripts made readable.

## [3.2.0] - 2026-06-25

### Added
- Sweep hardening: liveness guard, heartbeat / error detection
  (`csd sweep-health`), and prompt read-transaction release.
- `summary_state` pre-LLM gate with `csd reconcile-summaries`,
  `csd unsummarized`, `csd mark-summarized`.
- Project README; env-driven config (internal infra strings scrubbed).
- UAT script for the sweep/reconcile work; `uv.lock` tracked.

### Fixed
- Sweep/reconcile noise cut; the silent lock-hang killed; the gate hardened
  against unreliable `session_id` (colliding ids excluded from the bare-id
  gate).

## [3.1.0] - 2026-06-08

### Added
- Token-cost views (the caching lens) and the statusline relocated into the
  repo.
- Error-class taxonomy + a heuristic tl;dr A/B harness.
- `tldr` derived at ingest, plus the `csd sweep` observability head (phases
  1-3).

### Fixed
- Closed the sweep lock-convoy root: idle-transaction reaper + gate view DDL.
- Analytic reads hardened — timeout, count estimates, single-pass recompute.

## [3.0.0] - 2026-06-01

### Added
- **Gen3:** the Postgres archive backend — `csd` becomes the front-end of the
  lossless `claude_sessions` archive (schema DDL, JSONB escape hatches, batched
  idempotent upserts, analytic views, glob+mtime incremental sync).
- `recompact` and `session_digest` transcript tools; `transcript_analyzer`
  relocated from the knowledge repo.

### Removed
- The Gen2 SQLite era (`database.py`, `sessions_index.py`, the SQLite/VisiData
  analyst surface) is superseded and retired.

## [0.1.0] - 2026-02-22

### Added
- Project scaffold.
