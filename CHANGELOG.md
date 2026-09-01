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

## [3.15.0] - 2026-09-01

### Changed
- **The Git rail groups commits by the branch they are part of.** Both commit
  lists — the session rail's *commits in session window* and the repo detail's
  *latest N across all branches* — were flat and interleaved: they said WHAT
  happened but never *as part of what*, so five commits from three different
  feature branches read as one undifferentiated run. Each list is now one
  collapsible group per owning branch, with the same commit rows underneath.
  - **Derived in code, never by a model** — git already records the answer.
    Trunk is **resolved** (`origin/HEAD`, then a `main`/`master`/`trunk` probe,
    the repos lens's rule), `log --first-parent` IS the current branch's own
    line and every commit on it belongs to that branch, each merge M on the
    line names a side branch whose commits `rev-list M^1..M^2` enumerates
    exactly, and an unclaimed commit reachable from a live local branch
    (`trunk..branch`) belongs to the branch whose tip is nearest — listed once,
    never duplicated. Everything left stays with the current branch.
  - A merged group is named from the PR whose `oids` cover its commits when
    there is one — **headRefName is the only name that survives
    `--delete-branch`** — then from the merge subject (`Merge branch 'x'`,
    `Merge pull request #N from owner/x`), then `(merged branch)`.
  - **The request path never fans out.** The git side is a per-root TOPOLOGY
    (~25 bounded read-only calls, ~480ms on this repo) cached at `TOPO_TTL_S`
    = 90s, well above the snapshot's 12s because the branch shape is the
    slowest-changing thing in a repo; grouping a commit list against it is
    0.2ms of pure set arithmetic with no git at all. ⟳ refresh busts it.
  - **Failure isolates.** `group_commits()` cannot raise: an unreadable
    first-parent line, a missing branch list, a garbage topology all return
    `[]` plus a `group_note`, and both surfaces fall back to the flat list they
    always shipped. The commits are never withheld because their grouping
    failed.
  - Payload is **additive**: the flat `commits` arrays are unchanged, and
    `groups: [{branch, merged, merge_hash, pr, worktree, remote_name, ahead,
    behind, current, count, commits:[…]}]` is new — on `session_window` for
    `/api/git`, top-level for `/api/repo`, ordered by each group's newest
    commit with the current branch first. Group commits are the very same dicts
    the flat list holds, so a `pr` stamp rides along by reference and the two
    can never disagree.
  - The header carries the branch name, a `merged ✓ via <hash>` or
    `unmerged · ahead N` chip, the PR chip when attributed, and a ⌥ glyph when
    the branch has a worktree. The name links to GitHub **only when a remote
    actually carries the branch** (`remote_name`) — the 3.12.1 rule; a merged
    branch is usually deleted, and linking it is a 404 wearing a hyperlink.
  - Current branch open by default, merged groups collapsed; the choice is
    remembered per repo in `localStorage` (the rail is polled, and a group that
    sprang back open every few seconds would be worse than no grouping).
  - The session window's tooltip keeps its honesty: *timestamp* membership is
    still best-effort, but the branch a commit is grouped under is not — it
    comes from git's own first-parent line and merge records.
  - 19 unit tests over real throwaway repos (`tests/test_commit_groups.py`).

## [3.14.1] - 2026-09-01

### Added
- **GFM pipe tables in the console's markdown renderer.** A `| a | b |` header
  over a `|---|---|` delimiter now renders as a real `<table>` — header row,
  cell borders, zebra body rows — in chat turns and the changelog overlay
  alike (`mdLite` is one renderer, shared). Leading/trailing pipes are
  optional, `\|` is a literal pipe inside a cell, `:---` / `:---:` / `---:`
  set column alignment, cell content goes through the same inline pass (code,
  bold, links), and a table under an open list item lands *inside* that item.
- Detection is a **two-line contract** (a row carrying an unescaped pipe plus a
  matching delimiter row), evaluated only outside a code fence — so a fenced
  block full of pipes, a bare `---`, and `foo |` over `---` all stay what they
  were. Ragged rows follow GFM: short rows pad, long rows truncate.
- The **wrapper**, never the table, is the scroller (`.mdtw` is
  `overflow-x:auto`): a 12-column table scrolls inside its own box exactly like
  a wide code fence rather than widening the chat column — verified at 418px
  wrapper / 1542px table with the pane unmoved.

### Changed
- **Rendered markdown has colour.** New `--md-*` tokens on `:root` —
  `--md-head`, `--md-subhead`, `--md-code`, `--md-quote`, `--md-strong`,
  `--md-marker`, `--md-thead`, `--md-zebra` — tint headings, inline code and
  fence-language labels, blockquotes (accented left border + a 5% wash), bold,
  list markers and the table header. Hue carries the block *type*, never
  emphasis; the console is dark-only, so there is no light pairing to keep.
## [3.14.0] - 2026-09-01

### Added
- **`csd digest <SESSION_REF>` — the digest, addressable by session id.** The
  `/session-summary` skill used to locate a transcript with
  `find ~/.claude/projects -name "$SID.jsonl" | head -1` and then run
  `python3 /Users/andrew/GitHub/claude-session-db/claude_session_db/session_digest.py`
  — a command substitution a headless run can never get approved, plus a
  hardcoded absolute interpreter path. Both are gone: `csd digest` resolves the
  ref **worktree-aware** (archive `sessions.file_path` first, then a glob over
  `~/.claude/projects/*/<id>.jsonl`), accepts a unique prefix, and calls
  `session_digest.render` — so its output is byte-for-byte the old one, same
  `SESSION DIGEST · …` header and `span:` / `delta span:` lines. It is NOT a
  second renderer.
  - Works with **no database at all**: the glob alone resolves a full id, so
    the skill still digests when the archive is wedged.
  - Default scope is the WHOLE transcript (session_digest's own default), not
    the head/tail window `csd angles digest` applies — a silently elided middle
    is a silently short summary. `--head/--tail` window it on demand.
  - `--since TS` renders only the post-watermark tail (the continuation-pass
    window), with no watermark lookup — the DB-free half of `--delta`.
  - An unresolvable ref exits 1 with `NO TRANSCRIPT FOUND for <ref>` on stderr,
    the string the skill branches on. A `<parent>:<agent_id>` child key is
    refused pointing at the parent, because session_digest renders main-chain
    records only — digesting the parent under a child's name would return work
    that is not the child's.
- **`csd summary-scope <SESSION_REF>` — is this a continuation pass?** Reports
  whether a kmcp session summary already exists and what a NEXT pass would
  cover, so an **in-session** `/session-summary` can detect pass N without the
  console's dispatcher telling it. Three verdicts: `full` (no prior capture, or
  one with no resolvable watermark — the honest scope is the whole transcript),
  `delta` (a window opens at `since`; the exact `csd digest … --since …`
  command is printed), `none` (captured already and the tail is not
  substantive — nothing new to write). `--json` emits the same facts;
  `--mode auto|force|off` picks the grading, matching the console's `delta`
  body field.

### Changed
- **One grader, three surfaces.** `resolve_summary_scope` and `_prior_capture`
  moved out of `console/server.py` into `summarize.py` (beside the
  `_delta_gate` they wrap). The console keeps a thin wrapper binding its own
  module-level DSNs — behaviour byte-for-byte identical, tests unchanged — and
  the CLI (and through it the skill) now grades a pass through the *same* code
  the Summarize button and the launchd timer use. Doctrine travels with it:
  it never raises, and an unreachable archive degrades to `pass 1 / full` with
  the reason printed, exit 0.
- `csd` no longer needs a DSN to start for `digest` / `summary-scope`; every
  other command still fails loudly at the group level when the archive is
  unconfigured.

## [3.13.0] - 2026-09-01

_Most of this batch's `index.html` code reached `main` inside the 3.12.3
tooltips commit (two sessions sharing one worktree); this entry labels it._

### Added
- **Repo pills in the sidebar.** The project accordion (and its ▤ flat/grouped
  toggle) is replaced by a pill strip under the sort bar: an `all` pill plus
  one per repo, each carrying its session count and an amber `●n` waiting-
  for-you count. Selecting a pill filters the list to that repo; selecting it
  again (or `all`) clears. The choice persists (`csd.navOpenProj`, the key the
  accordion used, so the collapsed rail's project icons still land on it),
  applies to both the Projects and Archive tabs, and falls back to `all`
  without forgetting itself when the current tab has no sessions in that
  repo. Under a search the counts become per-repo hits and empty repos dim.
  The tl;dr-all batch queues exactly what the pill + search show.

### Changed
- **Three-line session rows.** Line 1 is the title on its own, full width;
  line 2 the meta line (project only while `all` is in effect); line 3 a
  right-aligned action strip — accept-proposal ✓, rename ✎, the T/⧗ digest
  chips, the priority flag — every action a uniform `.rbtn` in one flex row
  that wraps before truncating, so further buttons are one span appended in
  `sessRow()`. The rename glyph is always visible now rather than hover-only.
- **Sidebar scrollbar gutter is reserved** (`scrollbar-gutter: stable` on
  the sidebar and the pill strip), so switching from a long list to a short
  one no longer shifts the contents sideways.

## [3.12.3] - 2026-09-01

### Added
- **Tooltips throughout the console** — 101 new `title=` attributes across 69
  sites, weighted toward git: every ahead/behind arrow, `unmerged`, `trunk`,
  `no upstream`, worktree row, merge marker and unpushed `↑` now explains in
  plain English what it means, not just what it is called. e.g. `↑4` reads
  *"4 commits on this branch that main does not have yet — unmerged work, not
  a problem in itself"*. Bare-glyph controls, state/verdict indicators and the
  threads-overlay column headers are covered too.

### Fixed
- Two tooltips that stated something untrue, caught on review:
  - "N modified" claimed tracked edits were *"the only edits here you could
    actually lose"*. Untracked files are **more** losable, not less — git has
    never recorded them and `git clean` deletes them outright. Now: *"tracked
    file(s) changed since the last commit — edits git is watching but has not
    saved yet"*.
  - The ctx chip named a *"roughly 200k"* ceiling. The window is per-model and
    this very console is often driven by a 1M-context session, so the chip
    cannot know the number; it now says the ceiling depends on the model.

## [3.12.2] - 2026-09-01

### Fixed
- **Git rail said "gh CLI not installed" on a machine where it is.** The
  console runs under launchd (`app.csd.console`), which hands the process the
  bare default PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), so `shutil.which("gh")`
  never saw Homebrew's `/opt/homebrew/bin/gh`. Two-layer fix: `_gh_bin()`
  resolves `$CSD_GH_BIN`, then PATH, then the well-known install dirs (the same
  fallback the `claude` resolver in `spawn_claude` already has), and the
  reason string now reports what the process could *see* ("gh not found on the
  console's PATH (…)") instead of a claim about the machine. The launcher
  (`~/.local/bin/csd-console-lan`, outside the repo) now prepends
  `~/.local/bin:/opt/homebrew/bin:/usr/local/bin` so every other shell-out
  gets the same fix at the source.

## [3.12.1] - 2026-09-01

### Fixed
- **GitHub links produced 404s.** Two causes, both now gone:
  - `encodeURIComponent` escaped the separator in a branch name, so
    `feat/console-repos-lens` became `/tree/feat%2Fconsole-repos-lens` and
    GitHub errored. A branch name is a *path*, not a path segment; `ghPath()`
    encodes each segment and keeps the slashes.
  - Refs and commits were linked whether or not they existed on the remote. A
    local-only branch has no GitHub page and an unpushed commit has no
    `/commit/<sha>` — both were 404s wearing a hyperlink.
- Linking is now evidence-based: a **branch** links only when it tracks a
  remote (and to the *upstream's* name, which is not always the local one); a
  **ref badge** links only when it is remote-tracking; a **commit** links only
  when `rev-list --all --not --remotes` says it is reachable from a remote.
  Unpushed commits carry a `↑` marker, and unlinked items explain themselves on
  hover — more useful than a dead link.
- Group headings link at the repo-level pages (`/branches`, `/commits`,
  `/pulls`), which are valid regardless of push state.
- `rev-list` failing degrades push state to **unknown**, never to "pushed" —
  an errored probe must not license a link.
- `test_symbolic_head_refs_are_dropped` recursed into its own stub once
  `_all_commits` gained a second git call; it now captures the real `_git`
  before patching.

## [3.12.0] - 2026-09-01

### Added
- **The chat header's 📁 and ⎇ chips are now links into the repo.** Clicking the
  folder chip opens that session's repository in full; clicking the branch chip
  opens it focused on that branch (the row is highlighted and scrolled to).
  Both are keyboard-reachable (Enter/Space, visible focus ring).
- **Repo detail view** — the drill-down behind a chip or a grid card: every
  branch with ahead/behind vs the trunk and its upstream, every worktree,
  **commits across all refs** (`log --all`, with ref decoration and merge
  commits marked `⑂`), and the repo's **pull requests** with state, checks
  rollup and merge age.
- **Real hyperlinks out to GitHub** when `origin` is GitHub — commit hashes to
  `/commit/<sha>`, branches and ref badges to `/tree/<branch>`, PRs to their own
  URL, and the repo name to its GitHub page. A non-GitHub remote renders plain
  text: a guessed URL is worse than none.
- Console `GET /api/repo?id=<sid>` / `?root=<root>` — the detail payload.

### Notes
- **The caller never names a path.** `id` derives the root server-side from the
  transcript (the `/api/git` derivation); `root` is admitted only when the
  registry already knows it, and an unknown root is refused with a 404 rather
  than handed to git as a cwd. A repo root *is* a git command's working
  directory, so an unvalidated one is a path-injection surface.
- `log --all` rather than HEAD's log: HEAD's line hides exactly what a repo view
  is for — the other branches moving in parallel.
- `origin/HEAD` / `upstream/HEAD` are dropped from ref decorations. They are
  symbolic aliases for the default branch, which is already in the list beside
  them, and kept they render a badge linking at `/tree/HEAD` — a URL that means
  nothing.

### Fixed
- `_branch_inventory` / `_worktree_inventory` resolve their caps at CALL time
  rather than binding `REPO_BRANCH_CAP` / `REPO_WORKTREE_CAP` as def-time
  defaults, which had quietly made both module constants decorative. Caught by
  the test that asserts the detail view lifts the card's caps.

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
