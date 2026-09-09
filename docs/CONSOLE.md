# The csd console, angles miner and CR

Extracted from `CLAUDE.md` on 2026-09-09 to keep the always-loaded memory file
under budget. This is the full doctrine for the web console (`csd console`),
the turn-angles miner, context reduction (CR), the repos lens, the side-session
permission envelope and the curation vocabulary. Nothing here was rewritten —
it is the CLAUDE.md text verbatim.

## Turn angles (`csd angles`) — pull-based per-turn mining

P1 spike of `claudecode:design/turn-angles-context-cockpit`. The operator fires
`! csd angles` right after an agent response lands; the latest turn is read
straight from the live session JSONL (no DB round-trip) and mined by ANGLES:
deterministic extractors (files F, commands X, git G, kmcp writes W, errors R,
metrics M — pure code, instant) plus small-model probes (direction D, events E
on `CSD_ANGLES_MODEL`, default qwen2.5vl:7b) and retrieval (knowledge K via
hybrid_search). Output is one-line ID-addressable headlines (~1-2K tokens);
detail persists under the state dir (`csd angles show F1`). Curation is the
operator's next message ("track E1, load K1, task D1") — nothing is written to
kmcp by the command itself. Doctrine: pull not push; extraction is code, models
only judge. A failed probe degrades to `(unavailable)`, never blocks the pull.

### One engine, one surface

Three commands, one seam — **the state dir**. Nothing serves what it mines.

- `csd angles` — the operator's synchronous pull for one turn.
- `csd angles-watch` (`angles_watch.py`) — the same miner, headless and
  ambient. The console now hosts this watcher **in-process** by default
  (ambient miner: settle-detected active sessions get angles mined, then
  tldr.ensure + timeline.ensure chained; archived / run-in-flight sessions
  vetoed; `--no-ambient` / `CSD_CONSOLE_AMBIENT=0` to disable) — run the
  standalone command only when no console is up, and never both against the
  same Ollama. Watches every live transcript; mines a session's latest turn once
  its JSONL **settles** (`(mtime_ns, size)` signature unchanged and quiet for
  `DEBOUNCE_S`), so a turn is never mined mid-write. A **single worker** drains
  the job queue, so N live sessions cannot stampede the local Ollama. It writes
  to `$CSD_STATE_DIR/angles/<sid>.json` and serves nothing.
- `csd console` (`console/`) — the reply-capable surface, and the only web UI.

The console is **Direction A**: it renders a session's own transcript (chat
turns, kmcp reads joined to their `tool_result` by `tool_use_id`) plus the angle
headlines it reads *off disk*. Answer resumes the session (`claude -p --resume`);
while the session can't accept a write (a console-spawned run in flight, or the
transcript wrote within 15s — the two-writer guard) the message is **queued, not
refused**: a per-session FIFO at `$CSD_STATE_DIR/console/queue.json` (atomic
replace, restart-safe) auto-dispatches head-of-queue through the same spawn path
once the guard clears, strictly in order, one in flight per session. Queued
turns render inline with a `⏸ queued` badge and are cancellable until dispatch
begins; a failed dispatch (3 attempts, backoff) blocks its queue visibly until
dismissed. Fork branches the session, and a point fork writes a **new** session
file rather than mutating the original.

**The console always mints a fork's session id itself** — a point fork writes
the file under a `uuid4()` it chose, and an end fork passes that uuid to
`claude --fork-session --session-id` (the CLI accepts `--session-id` on a
resume *only* alongside `--fork-session`). Never let claude assign an id we
then have to infer: both fork routes return `new_session`, and the spawned run
registers under the **fork's** id, so Stop aims at the fork instead of its
parent and the branch is addressable the moment it is created. Correlating a
process start time against transcript birth times would be inference — racy
when two runs share a project dir, and blind between spawn and first write.

### Console actions

- **Stop** — SIGINT → SIGTERM → SIGKILL to the process group of a run *the
  console spawned*. It cannot reach anything else, and the button is disabled
  with that reason. Claude Code opens a transcript, appends, and closes (no
  process holds it open), and an interactive `claude` carries no session id in
  argv — so an arbitrary live session **cannot be mapped to a pid**.
  `claude -p --resume` never attaches to a running session either; it spawns a
  new process that appends to the same file. That is what the two-writer guard
  is guarding.
- **Archive** — an index entry in `$CSD_STATE_DIR/console/archived.json`
  (atomic replace), never a mutation of `~/.claude/projects`. Archived sessions
  drop out of the sidebar, stay retrievable by id, ignore the 72h cutoff, and
  return on unarchive. Nothing is destructive.
- **Summarize + archive** — runs `/session-summary` **independently, off-session**:
  a throwaway `claude -p` process (no `--resume`) is handed the session UUID as
  the skill argument, so the skill digests the transcript from disk
  (`session_digest.py`) and writes the changelog + lessons to kmcp **without ever
  resuming or appending to the session**. Because nothing writes back, the 15s
  two-writer guard is gone and the archive is decoupled — the session is archived
  the moment the summary is dispatched (its outcome is tracked in `SUMMARIZING`
  for visibility, not as an archive gate). **The run is itself a session the
  console minted the id for** (`--session-id`, as with Fork): registered under
  the child id so Stop in the child view reaches it, titled `Summary of
  ‹parent› (pass N)` at dispatch, and linked both ways in the meta overlay
  (`summary_of` / `summary_child`) — the parent's summarizing chip opens the
  run, the run's header links back, and the link survives a restart.
  Summarize is the **first action dispatched through the side-session permission
  envelope** (below); every other spawn is still ambient.
  It is also **repeatable**: `resolve_summary_scope()` grades the session through
  the *same* `summarize._delta_gate` the launchd timer uses, so a second press
  captures only the work after the prior pass's watermark — the button reads
  "Summarize NEW work since ‹date› (pass N)", and the window, the literal
  `session_digest.py --since` command and the prior entry ref travel to the child
  in the envelope's appended system prompt. `delta` in the POST body picks the
  scope (`auto` default / `force` / `off`), the pass is claimed and recorded in
  `summary_passes`, and a lost claim refuses. Same doctrine as `resolve_envelope`:
  an unreachable archive degrades to full scope with the reason surfaced, never a
  block. The console does not quiesce (a manual close-out is deliberate) — a
  transcript written inside phase-4's idle window comes back as a `warning` the
  UI shows, because a live session is digested short, silently.
- **Summary tab** (`GET /api/summaries`) — the rail tab that answers *has this
  session been captured, by what, and what landed in the corpus*. Controls:
  **Summarize** (auto scope, wearing the same pass-aware label as the header
  button; disabled with the reason when the grade is `none`), **Full
  re-capture** (`delta:"off"`), **Capture events**, ⟳ — with the grade stated
  in words (watermark, source, pass, prior ref). **Runs** merges three
  independent records of the same history: the `summary_passes` ledger (the
  only one that sees launchd's phase-4 local-Ollama passes — marked `phase-4`),
  the console-minted child runs on `meta.json` (durable across a restart;
  `summary_pass` / `summary_kind` ride there), and the in-process
  `SUMMARY_RUNS` tracker (rc + in-flight). **Entries written** groups by entity
  type and links each `app:path` into /browse, deduped across this session's
  transcript and every run's — extracted by the **Context tab's own** write
  extractor so the two can never disagree about a created/updated/dry-run/error
  verdict, and joined to the knowledge DB in **one** query so a written ref is
  confirmed present (or flagged `not in corpus`). On-demand ONLY: it grades the
  tail and re-parses each run's JSONL, so it re-polls only while a run is in
  flight and never rides the nav poll. Degrade doctrine as everywhere on this
  seam — an unreachable archive or knowledge DB costs only its own half and
  lands in `warnings[]`, never a 500; a child key is refused 400.
  **Capture events** is the same off-session dispatch through the same
  envelope with the /session-summary skill's **own** `--events` override
  appended (its thin-changelog path: changelog `event` entries only, no session
  entry, no lessons, no tasks). Because it writes no session entry it
  deliberately takes **no `summary_passes` claim** — a ledger row would advance
  the pass number and imply a watermark `csd reconcile-summaries` would later
  stamp — and it never archives.
- **Mine angles** — `csd angles --session <sid>` on demand, so the rail is
  usable without `csd angles-watch` running.
- **tl;dr timeline** — the first *pre-determined angle button*: a whole-session,
  time-stamped catch-up (one line per user-prompt turn, **tool results omitted**),
  rendered as the **Timeline tab of the right rail** (beside Angles/Context/
  Writes/Git). Distinct from the last-3-turns `tldr` headline — this walks the
  ENTIRE conversation. Engine is `session_timeline.py` (`POST /api/timeline`
  force-generates, `GET /api/timeline` serves cached, never generates). Segment +
  map: one small local-Ollama call per turn (so it scales to any length and never
  overflows a 7B/8K-ctx model), completed turns memoized by prompt uuid, the tail
  turn always recomputed. **Cached-first, pull not push**: opening the tab only
  ever serves the cached store off disk — it never auto-mines. Generate/⟳ are the
  only things that force a run, and the tab polls only while one is in flight.
  Bounded by `CSD_TIMELINE_MAX_TURNS` (150; older turns omitted, surfaced in the
  footer). State: `$CSD_STATE_DIR/timeline/<sid>.json`.
- **Digests at the session row** — every sidebar row carries two presence chips
  (`T` tl;dr, `⧗` timeline; absent/stale/fresh/error/generating by color). A tap
  opens the **digest reader** popover (read the tldr or timeline without loading
  the chat) and, when absent/stale, also fires the ensure POST. Run-if-needed is
  server-side — `tldr.ensure()` / `session_timeline.ensure()` (fresh = no-op,
  error stores not retried unless forced) — the seam a future ambient runner
  calls. Row presence ships in `/api/sessions` signature-memoized (store files
  re-read only when their `(mtime_ns, size)` changes), so the nav poll costs
  stats, not reads. The tldr's single model call also yields a **proposed
  session title** (`title_proposal` in the store): surfaced as a suggestion in
  the row (ghost placeholder when the session has only a raw fallback title),
  the reader, and the chat header — one-press ✓ accept writes through
  `/api/title`; ✕ dismiss is remembered by value (`tp_dismissed` in meta.json).
  A manual title is never auto-overwritten.
  The reader's foot carries **close-out actions** so a session can be triaged
  without opening the chat: plain **Summarize** (the same off-session dispatch,
  `/api/summarize` with `archive:false` — the session stays in the sidebar),
  **Sum + archive** (the historical coupled action), and **Archive/Unarchive**
  (index-entry flip only). Confirm-free; post-click state lands inline
  (`summary dispatched ✓` / `archived ✓`), and a summarize already running for
  the session disables both summarize buttons with the reason in the title.
  A **third chip, `Σ`**, answers what the row could never say without opening
  something: *is this session captured, and is there anything new since?* Six
  states — `never` (dashed/dim), `captured` (green, nothing substantive since),
  `delta` (amber — the OPEN-delta case, real new work after the watermark;
  a capture with no resolvable watermark reads amber too, since its next pass
  is a full re-capture), `running` (pulsing blue), `failed` (red — a
  `summarize_attempts` backoff row or a failed console run), `unknown` (solid
  grey — the archive could not be asked; never a false "never"). The tooltip
  carries pass N, the watermark date, the prior kmcp ref, the post-watermark
  record count and the grader's reason; a tap opens the same reader, whose foot
  now states the grade in words above the Summarize buttons, as does a `Σ …`
  chip in the chat header beside the summarizing-run chip (state vs. last run).
  **One grader, and the poll never grades**: the state comes from
  `summarize.summary_scope_report` → `_delta_gate` — the button's, the timer's
  and `csd summary-scope`'s gate — run by ONE background refresher
  (`_summary_refresher`, `CSD_SUMMARY_REFRESH_S` 120s, serial, mirrored to
  `$CSD_STATE_DIR/console/summary.json`), while `summary_presence()` on the
  request path is a pure `(mtime_ns, size)` memo read that opens no connection.
  It ships as `summary` in the same `/api/sessions` field family as
  `tldr`/`timeline`; every failure degrades to `unknown`, never an error.

### CR — context reduction (`cr.py`, `/api/cr*`)

A fork with curation: the operator picks what survives, CR writes a NEW reduced
session file (the original is never touched). Two things make the panel
trustworthy — **an honest measure and no hidden bucket**.

**The accounting model.** BEFORE is what the API would count, not the file's
bytes: text/prompt/narration/thinking-TEXT/tool_result at chars/4, a `tool_use`
input at `len(json.dumps(input))/4` (the API receives the input as JSON), an
image at (w×h)/750 capped at 1600 (dimensions sniffed from the decoded image
header; an unreadable format degrades to a documented flat estimate, never to
base64/4). Thinking **signatures**, image base64 payloads and the JSON envelope
are excluded — they are in the file but they are not tokens — and reported in
`excluded` so the gap between file size and estimate is stated, never swallowed.
Byte-counting the JSON read one real session at ≈337K against a true ≈157K: a
single screenshot was ≈119K of it and 88 signatures another ≈51K.

**`totals.est_tokens` IS Σ rows.** Every token-bearing block is a row —
including the `tool_use` inputs and images that used to vanish into an opaque
`fixed_chars` bucket, which is precisely why the group sizes could never add up
to BEFORE. `residual_tokens` exists only so a future drift would be *shown*
instead of absorbed. Locked kinds (thinking, unrecognised blocks) are counted
but never stubbed. Preview, fork and manifest all report this one measure.

**Σ rows is the REDUCIBLE context, not the context.** The API bills more than
any file count can see: the scaffolding floor, the model's thinking when the
transcript stores it as a signature only, and the chars/4 shortfall on JSON
tool traffic. `cr.billed_context` reads `usage` — the last call's context (the
header's ctx chip), the floor as turn-1 context (≈32K on a real session; the
70–100K band is only the usage-less fallback, labelled `source: band`), and
`last_turn_output`, an upper bound on the thinking still in context — and the
cart/preview print the reconciliation signed: `BILLED ctx ≈230K = floor ≈32K
+ reducible ≈98K + last-turn thinking ≤42K + estimate error ≈58K`. What a
fork will bill on resume is `floor + AFTER` (prior-turn thinking is dropped),
and that figure is printed beside AFTER.

**The fork behaves like the reduced session it claims to be.** With CR mode
on, the composer sends INTO the fork: one `POST /api/cr {confirm:true, text}`
writes the fork and starts the headless turn in the NEW session (registered
under the fork's id; the same direct spawn `point_fork` uses — a file the
console just wrote is not a second writer), then the console opens it. The
fork is stamped on meta.json (`cr_source/before/after/floor/billed/at`), and
because it copies its source's last usage block verbatim, `_cr_overlay` shows
`≈floor+AFTER` as a labelled *CR estimate* on the row, header and status line
until an assistant usage record postdates the stamp. `claude --resume ID`
resolves inside the CURRENT cwd's project dir and the fork lives beside its
source, so the preview, the post-write dialog and `resume_cmd` print
`cd ‹source cwd› && claude --resume ‹id›`.

**Per-source control.** Each group header expands into an itemized list (one
line per source, heaviest first or by turn, turn number + what-it-is + the
row's opening words (`head`) + size + dup marker; tapping a line opens the
row's FULL content in place via `GET /api/cr/row` — text as the API sees it,
an image rendered — with a jump to the message in the chat),
with per-item keep/stub/ref buttons writing the same `crAct` state as the group
buttons and the stream chips — the three surfaces cannot disagree about a row.
Capped at 40 items with a "show all N" toggle; open/closed remembered in
localStorage. AFTER is computed client-side from that same per-row state and
its arithmetic is printed in full (`kept + stubs N·10 + cart R refs`).
Stubbing stays the validated edit class: a stubbed `tool_use` keeps its `id`
and `name` and swaps only the input for a breadcrumb (the tool_use/tool_result
pair must still match), a stubbed image becomes a `[image removed by CR: …]`
text block, and a stubbed result is swapped in BOTH copies.

### Repos overlay — the cross-REPO lens (`/api/repos`)

The other axis to the threads overlay: threads answers *which sessions are still
open*, this answers *which repos are*. `⎇ repos` beside `⊞ threads`; one card per
repository with trunk, unpushed/behind, working tree, unmerged branches and live
worktrees.

**The registry is discovered, not configured.** Distinct `sessions.cwd` from the
archive (30d, `CSD_REPOS_WINDOW_DAYS`), each resolved through `rev-parse
--show-toplevel`; a cwd that is not a repo never appears. A linked worktree
folds into its PARENT (`--git-common-dir`) — a worktree is a second checkout,
not a second repository, and un-folded it stands as its own row carrying the
parent's branch and worktree counts. `CSD_REPOS` pins extra roots; the archive
being unreachable degrades to the transcript-tail cwd derivation the nav already
uses, so the lens works with no database at all.

**Cached-first, and the request path NEVER fans out.** One repo snapshot is ~9
git invocations; 20 repos on a 30s poll would be ~180 subprocesses a tick.
A single background walker (`_repos_refresher`, `CSD_REPOS_REFRESH_S`, default
90s, staggered and serial) writes `$CSD_STATE_DIR/console/repos.json`; `GET
/api/repos` is a disk read. Same seam as tldr/timeline — the endpoint reads, the
worker writes. `POST /api/repos/refresh` is the only forced walk (the ⟳ button),
and it re-discovers the registry so a new repo needs no console restart.

**Read-only, and never a fetch.** Every call goes through `_git()`
(`--no-optional-locks`), so observing a repo cannot write into it; ahead/behind
is measured against the remote-tracking refs already on disk. The header says so
— *"behind" = behind your last fetch* — rather than implying live remote truth.
Trunk is `origin/HEAD`, then a `main`/`master`/`trunk` probe: guessing `main` is
how a lens reports every branch in an older repo as unmerged.

**The 📁 and ⎇ chips in the chat header are links into it.** Folder opens that
session's repo in full; branch opens it focused on that branch. The detail view
(also reached by clicking any grid card) carries every branch with ahead/behind
vs the trunk, every worktree, **commits across all refs** (`log --all` with ref
decoration — HEAD's log hides the parallel branches a repo view exists to show),
and the PR listing with checks. When `origin` is GitHub, hashes / branches / PRs
are **real hyperlinks** out; a non-GitHub remote renders plain text rather than a
guessed URL. `GET /api/repo?id=<sid>` derives the root server-side from the
transcript; `?root=` is admitted only for a root the registry already knows —
a repo root IS a git command's cwd, so an unvalidated one is path injection.

**Commits group by PULL REQUEST, not just by branch.** The Git rail's "commits
in session window" and the detail's commit list share one grouper
(`commit_groups_payload`), which resolves every commit to the PR it landed as —
a `Merge pull request #N` subject (its `M^1..M^2` side commits included), a
`… (#N)` squash marker, gh's `mergeCommit.oid`, then PR-oid membership — in
**pure code over data already fetched**, never an extra git call. On a
squash-merge trunk this is the difference between one group called `main` and
one group per PR; commits belonging to no PR trail in `no PR · direct to
‹branch›`, and the PR cards below stop repeating what the groups already show.
Both partitions ship in one payload (`groups` + `group_alt`), so the header's
`by PR | by branch` toggle is a re-render, and a grouping failure still degrades
to the flat list plus a `group_note`.

**The attention band is deliberately narrow**: uncommitted *tracked* edits, an
unpushed trunk, or a worktree whose folder is gone. Untracked files and
months-old unmerged branches are a working repo's normal resting state — banding
on them put 16 of 17 rows in the band, which is the same as having no band.
Failure isolates per ROW (a deleted directory, a git timeout, an
`ahead-behind` atom an older git lacks); the lens never raises.

### Side-session permission envelope (`spawn_claude` as resolver/translator)

Pilot of `claude_session_db:design/task-driven-side-sessions`. Side-sessions used
to pass **no scope or permission flag** and inherited whatever ambient settings
their `cwd` resolved to — so a Summarize spawned with a git-worktree cwd could not
read `~/.claude/projects`, where the transcript it digests lives. Measured A/B on
the same cwd/target/prompt: without the envelope the child returns *"you haven't
granted it yet"* + *"This command requires approval"*; with it, it reads the
transcript and runs `session_digest.py` clean.

The envelope is **declared data, not code**: a versioned kmcp skill
(`claude_session_db:skill/console-summarize`) binding an agent
(`agent:tools/session-summarizer`). `spawn_claude(…, action=…)` only *translates*
it — `harness_hints.required_tools` → `--allowedTools` (comma-joined; the flag is
variadic and would otherwise eat a bare prompt), `fs_read`+`fs_write` →
`--add-dir`, `constraints.max_turns` → `--max-turns`, and `guardrails` **plus the
already-resolved transcript path** → `--append-system-prompt`. That last part is
load-bearing, not decoration: the skill otherwise locates its transcript with a
command-substituted shell command that a headless run can never get approved, so
filesystem scope alone would not have fixed it.

`fs_read`/`fs_write`/`bash_allow` are first-class keys under `harness_hints`
(decision: `event/2026-08-01/decide-harness-scope-keys-on-harness-hints`) — the
skill schema sets no `additionalProperties` bar, so they validate today with no
migration.

**Doctrine — it cannot break the console.** `resolve_envelope()` never raises and
never blocks: kmcp unreachable, skill missing, entry malformed all degrade to
**zero flags**, byte-for-byte the previous behaviour, with the reason surfaced
(spawn log + the `/api/summarize` `envelope` field) instead of swallowed. There is
deliberately **no hardcoded fallback envelope** — duplicating a declared scope in
Python is the drift this displaces, and a silent fallback would mask a broken
resolver. Actions with no bound skill (answer, fork, the queue dispatcher) resolve
to nothing and are untouched. Least privilege only; `bypassPermissions` is never
emitted. Flags are **prepended**, which is safe only because every call site's
args begin with `-p` — `spawn_claude` checks that contract and drops the envelope
(never the prompt) if a caller breaks it.

### Curation — the span action-vocabulary

`track → event`, `record → lesson`, `task → task` deposit kmcp writes;
`load` / `drop` are context ops that compose the operator's *next message*
(the design's "the operator's next message IS the curation") and write nothing.

Writes are **two-phase**: compose a draft, validate with `import_entries`
`dry_run`, show it, write only on explicit confirm. A small model's headline
never reaches the corpus unreviewed. Two further guards:

- The entry document is passed as **JSON**, which is valid YAML 1.2 — sidestepping
  the `import_entries` YAML footguns wholesale (unquoted `#` truncation, bare
  timestamps coerced to datetime, angle-bracket placeholder rejection).
- Application inference **proposes, never decides**. The cwd basename is a guess
  (`final-taglists` is not a kmcp app); it is validated against the live
  `list_applications`. A confirmed write is *refused* when the app was a
  fallback or when kmcp is unreachable — otherwise the entry lands silently in
  the wrong corpus, or invents a junk application out of a directory name.

The kmcp reads-rail counts the `knowledge-cli call <tool>` Bash shim as a read,
not just `mcp__*__<tool>` — a session that took the fallback loaded just as much
context and must not vanish from the rail.

The Context tab lists three kmcp populations, all extracted by code from the
transcript: **consumed** (get_entry/get_section/get_entries), **surfaced**
(search-family results, per query, each ref marked opened or not — the
unopened ones are the signal), and **written** (`WRITE_TOOLS`, seeded from
`angles._WRITE_TOOLS` so the miner and the console cannot drift). A write's
created-vs-updated verdict comes from the tool_result, a refusal returned as
`{"error": …}` without `is_error` is still an error, and a dry-run that was
never followed by a real write renders as a dry-run, never as a write.

Superseded and REMOVED (2026-07-17): `angles-serve` / `angles_web.py`, a
read-only LAN dashboard that duplicated the session list and reads-rail beside
the console. Its watcher lives on as `angles_watch.py`; its gems — the
session-management "sessions" tab and the subagent drill-down — were ported
into the console (threads overlay, Agent-row child links); its UI is gone.
The console is the single web surface: `csd console` binds 127.0.0.1:4462 by
default, and any non-loopback bind (e.g. `--host 0.0.0.0 --port 8791` for the
LAN) requires token auth (`CSD_CONSOLE_TOKEN`, auto-generated if unset;
`?token=` once, then a cookie).

