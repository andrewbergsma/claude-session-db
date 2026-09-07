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

## [3.24.5] - 2026-09-07

### Added

- **Console sidebar — the session id's first section on every row.** The
  8-hex handle (`f682a93d`) sits at the top-right of the title line, in the
  muted mono the meta line uses; the title ellipsizes before the id is ever
  pushed off. Hover shows the full id, a click copies it. (3.24.4 is the CR
  injection-label fix, on its own branch at the time of this cut.)
- **Console — a status line under the composer**, the two rows Claude Code
  prints under its own prompt, from the transcript: model · directory ·
  ⎇ branch, then `105k (104k cached · exp 07:59 · 96% hit) / 1000k high
  (session id)`. `/api/session` now carries `effort`, the last turn's `usage`
  split and a `cache` block (TTL tier from `usage.cache_creation`, last-usage
  timestamp, per-message hit tally). The window is inferred from the model id
  (the transcript never records it) and says so in the tooltip.

### Fixed

- **Console — chat text matches the rest of the app.** Turn prose was 13.5px
  against 12.5px sidebar titles and rail body; it is 12.5px now (inline and
  fenced code 11.5px).

## [3.24.3] - 2026-09-07

### Fixed

- **Context tab — a batch import's one refused entry no longer fails every
  row.** `import_entries` reports a validation error per entry
  (`{entry, path, error}`); the extractor now keeps that attribution
  (`_parse_write_result` → `failed`, marked on the ref in `_write_meta`) and
  both consumers — the Context tab's written list and the Summary tab's
  `_run_writes` — charge an attributed error to its entry alone. An error no
  ref owns (a whole-call refusal, a transport error) is still every ref's.
  A five-document import whose dry run refused entry 4 over a bad `scope`
  section used to read *1 failed* on all five rows, including the four that
  landed.
- **Context tab — nested `path:` lines are citations, not entries.** The
  parser-free YAML scrape read every `path:` at any depth, so a task's
  `references[]` and a charter's `see_also` list became phantom *created*
  rows (`docingest:charter`, `orchestration:pmo/registry`, …) wearing the
  LAST `entity_type` in the file. `_yaml_docs` now splits on `---`, fixes the
  key indent from each document's first own key, ignores deeper keys, yields
  one document per top-level list item, and reads `entity_type` per
  document. Five documents now yield five refs, not twenty.

## [3.24.2] - 2026-09-07

### Added

- **Console sidebar — the branch on every row.** Line 3 of a session row now
  opens with `⎇ <branch>` at the lower left (the transcript's `gitBranch`,
  which `/api/sessions` already carried), ahead of the project label and the
  action strip. A tap opens the repos overlay for that session's repo focused
  on the branch — the same jump as the chat header's `⎇` chip — without
  loading the chat (`openRepoFor` takes an optional session id).

## [3.24.1] - 2026-09-07

### Fixed

- **Console sidebar — a search ignores the repo pill.** Typing in the search
  box now suspends the selected project pill: the list, the per-pill hit
  counts and the "all" highlight behave as if no pill were selected, and the
  persisted choice returns the moment the query is cleared. Pasting a session
  id with another repo's pill selected used to answer *no sessions match … in
  this repo* (`activePill`).

### Changed

- **Console sidebar — repo pills lay out as a grid.** The pill strip is a
  CSS grid (auto-fill columns at ~150px, aligned rows) instead of a ragged
  wrap; labels ellipsize inside their cell and the count sits flush right.

## [3.24.0] - 2026-09-02

The **code-defect batch** — schema **v10**. An adversarial review of the live
archive on 2026-09-02 found eight defects: duplicated child rows, a predicate
the v9 relabel had already rejected, two never-populated columns, three fields
dropped on ingest, a frozen project path and a silently incomplete cost view.
All schema changes are additive and idempotent; no existing row is deleted or
rewritten outside a documented resumable backfill. Full schema reference:
[`DATA_MODEL.md`](DATA_MODEL.md).

### Fixed
- **`content_blocks` / `tool_results` no longer duplicate across source files.**
  `messages` inserts `ON CONFLICT (uuid) DO NOTHING`, so a record present in two
  transcripts keeps ONE message row — but the child tables have no uniqueness
  and `clear_file_data` deletes only by `source_file`, so the second file
  appended a SECOND set of blocks and results beside the first (2.7% of recent
  assistant messages; 3,339 duplicated `(message_uuid, tool_use_id)` pairs
  across 300 recent sessions). Ingest now **skips** block/result rows for a
  message whose row is owned by a different file — skip, not delete, because
  deleting by `message_uuid` would destroy rows another file's per-file
  clear/insert cycle owns and they would not come back until that file's mtime
  changed. `SyncStats.duplicate_rows_skipped` reports it.
- **`sessions.first_prompt` uses the v9 prompt rule.** `sync` still picked it
  with `u.is_direct_prompt` — the STRING-ONLY predicate the v9 relabel had
  already rejected — so a session whose first prompt carried an image, a
  document or any list-shaped content stored the *second* prompt, or none: 132
  of 2,684 main sessions. Both derivation sites (main session and sidechain
  child) now share `SessionSync._first_prompt`, which classifies on the
  `tool_result`-block rule and takes `prompt_text` (every text block, in order).
  New resumable backfill **`v10_first_prompt`** recomputes the history from
  `messages` — per-session, `IS DISTINCT FROM`-guarded, main sessions only
  (refresh child rows with `csd backfill-subagents`).
- **`sessions.session_kind` is populated.** v9 added the column to both
  `messages` and `sessions`, backfilled only `messages`, and left the sessions
  column NULL on every row in the archive. New resumable backfill
  **`v10_session_kind`** sets it from the constant `messages.session_kind`
  where the session column IS NULL (never overwriting a value ingest derived).
  `sync._derive_session_kind`'s docstring — which claimed there was
  deliberately no per-message column — is corrected.
- **`sessions.worktree_active` (new column) — the worktree EXIT is recordable.**
  `worktree-state` signals leaving a worktree with `worktreeSession: null` (38%
  of the records in the live archive) and the session upsert COALESCEs, so the
  null could never clear `worktree_session`: a session that had ever entered a
  worktree read as still inside it forever. The state now lives in an explicit
  boolean — NULL = no `worktree-state` record ever seen, `true` = the last one
  carried a session object, `false` = the last one was null — written
  last-observation-wins via the new `_SESSION_LAST_WINS_COLS` rule rather than
  COALESCE. `worktree_session` is unchanged and keeps its "last binding ever
  seen" meaning. The second payload shape (`enteredExisting: true`, with no
  `originalBranch`/`originalHeadCommit`) is accepted as a binding like any
  other. Both columns are exposed on `v_session_overview`.
- **`projects.decoded_path` no longer freezes at the first insert.**
  `get_or_create_project`'s conflict path updated only `last_seen_at`, so a
  project first seen without a usable `cwd` hint kept the naive (and for every
  dot-directory and worktree project, wrong) decode forever. A new nullable
  **`projects.decoded_from`** ('cwd' | 'encoded' | NULL = pre-v10) records the
  provenance; a `cwd`-derived path now upgrades a stored guess, and a guess
  never overwrites anything — the upgrade is strictly one-way. The per-run
  project cache remembers the provenance too, so a later file with a real hint
  can upgrade within the same sync. `encoded_path` remains the unique key.
- **`sessions.tool_use_count` / `error_count` count identities, not rows.**
  `recompute_session_aggregates` now counts `DISTINCT tool_use_id` (with a
  `block_id` fallback so an id-less block is not silently dropped by
  `count(DISTINCT)`) and `DISTINCT (message_uuid, tool_use_id)` for errors, on
  both the main-session and the child-session statement. The aggregates are
  therefore correct despite the historical duplicates.

### Added
- **`attachments.raw` (new column).** Every other conversation-flow table keeps
  the whole record in a `raw` JSONB escape hatch; `attachments` kept only the
  promoted columns plus the `attachment` sub-object, so the record's own
  top-level fields (`cwd`, `gitBranch`, `version`, `userType`, `entrypoint`,
  `sessionKind`, …) were parsed and dropped. Nullable, filled on ingest and by
  a re-sync. **No backfill is possible** — the data was never written.
- **Promoted-but-lossy record types are archived verbatim too.** A record type
  with a dedicated destination could still lose data: `bridge-session` kept only
  `bridgeSessionId` (dropping `lastSequenceNum`, `ownerAccountUuid`,
  `ownerOrganizationUuid`, `noHistoryBackfill`), `queue-operation.reason` and
  `last-prompt.explicit` had no column, and the seven latest-wins
  session-metadata types (`ai-title`, `custom-title`, `last-prompt`, `mode`,
  `permission-mode`, `bridge-session`, `agent-name`) collapse onto one
  `sessions` column so every earlier value existed only in the JSONL. All eight
  types (`SESSION_RECORD_ALSO_ARCHIVED`) now ALSO land in `session_records`,
  verbatim, `is_modelled = true` — an addition to the promoted columns, never a
  replacement, and invisible to the unmodelled census. Still exactly one
  `session_records` row per JSONL line, so `(source_file, source_line)` remains
  the key.
- **`content_blocks.caller` (new column).** `ToolUseBlock` has always parsed
  `tool_use.caller`, and `sync._content_block_row` never wrote it — dropped on
  100% of tool_use blocks. Now stored verbatim as JSONB (NULL when the block
  carried no `caller` at all, so "the transcript said direct" stays
  distinguishable from "the transcript said nothing"), deliberately unindexed.
  New resumable backfill **`v10_content_block_caller`** recovers the history
  from `messages.raw`, matching on `tool_use_id` rather than `block_index` —
  the pre-v9 dropped-block bug shifted historical indexes, so the index does
  not address the raw array.
- **`v_token_cost_daily` reports what it could not price.** New `messages`,
  `priced_messages` and `unpriced_messages` columns, matching
  `v_token_cost_by_model`. An unpriced row (no `model_pricing` pattern) yields
  NULL cost terms that `sum()` silently skips, so the daily rollup — the one
  `csd usage` reports from — read as a complete total while omitting spend.
  **No cost arithmetic changed.** The `v_message_cost` DDL comment now names
  `write_untiered_tokens`, the column the lump-`cache_creation` paragraph was
  about.
- **`v_duplicate_blocks`** — the historical duplication made visible:
  `(message_uuid, session_id, kind, source_files, row_count)` for every message
  whose blocks or results span more than one `source_file`. Historical
  duplicates are **not** deleted automatically; the operator cleanup recipe
  (batched, `LIMIT 20000`, delete only rows whose `source_file` differs from the
  owning message's) is documented beside the view definition in `postgres.py`.

### Changed
- **"eleven" record types reconciled to ten** in `sync.py`, the tripwire tests
  and `CLAUDE.md` — `SESSION_RECORD_TYPES` has always had ten members, and the
  3.23.0 entry above is corrected to match.
- **The `fallback` content block is dated to v2.1.215**, its first observation
  in the corpus, not v2.1.247 (the release csd happened to notice it in).
- **Status line reads context and cache from the payload.**
  `statusline/statusline-command.sh` now renders the context bar and the cache
  indicator from the status-line payload's `context_window` and `prompt_cache`
  blocks instead of scanning the session transcript, and sizes the bar
  correctly for a 1M context window (it previously assumed 200K). Dropping the
  transcript scan takes a 37 MB session from **687 ms to 133 ms** — the status
  line runs on every render, so that latency was paid constantly. New
  `statusline/test_statusline.sh` replays 11 recorded payload samples through
  the script (**55 assertions**), including first-turn nulls, post-compact,
  a resumed session with no `prompt_cache`, the legacy transcript fallback and
  a malformed payload. `statusline/README.md` rewritten to match.
- **DATA_MODEL.md reconciled against the merged v10 code** — the review rewrite
  (1,159 → 1,812 lines) was written against the v10 *plan*, so the "commit
  pending" markers, the two-instead-of-three `v_token_cost_daily` columns, the
  backfill driving keys and the predicted-vs-measured backfill figures are
  corrected against the shipped DDL. `CLAUDE.md` and `README.md` follow.

### Migration note
- v10 is applied by `initialize()` like every other version: guarded
  `ALTER … IF NOT EXISTS`-shaped DDL, then the resumable `BACKFILLS`. Nothing
  is dropped, truncated or deleted; the only permitted deletes remain the
  per-file `clear_file_data` path.
- **Running v10 DDL against a database a v9 process still sweeps**: park
  `metadata.views_version` at `9` until the v10 code is merged. v9's
  `CREATE OR REPLACE VIEW v_token_cost_daily` cannot drop the three columns
  v10 adds ("cannot drop columns from view"), so a v9 `initialize()` that
  decides to re-run its view DDL will raise and fail the sweep. With
  `views_version = 9` the v9 sweep skips view DDL entirely, and the merged v10
  code recreates the views on its next run (9 ≠ 10).

## [3.23.0] - 2026-09-02

The **Claude Code v2.1.161-258 impact release** — schema **v9**. Ten record
types, six model families, a fork mechanism and a `/cd` had arrived in the
transcripts since the last audit, and the archive was silently poorer for all of
them. Full schema reference: [`DATA_MODEL.md`](DATA_MODEL.md).

### Added
- **`session_records` — the catch-all that ends silent record loss.** The parser
  has always collected `records["unknown"]`, and nothing has ever read it: every
  session-scoped record type Claude Code added between v2.1.161 and v2.1.258 was
  parsed and then dropped on the floor. Every such record is now stored
  **verbatim** — ten modelled types (`atis-latch`, `worktree-state`,
  `relocated`, `file-history-delta`, `history-suppression`, `frame-link`,
  `cost-state`, `artifact-autoreact-ledger`, `artifact-comment-monitor`,
  `fork-context-ref`) plus anything csd has never seen, flagged
  `is_modelled = false`. Keyed `(source_file, source_line)` — these records carry
  no uuid and a transcript is append-only — and in `PER_FILE_TABLES`, so a
  re-sync clears before re-inserting.
- **The UNMODELLED tripwire**, on all four surfaces a new record type could
  reach. `SyncStats` carries a census by type; `csd ingest` prints
  `UNMODELLED record types: N records — type×n, …` under the sync summary and on
  the one-line sweep form; the sweep heartbeat carries it so **`csd sweep-health`
  reports it as a `notice:`** despite being deliberately DB-free; and `csd stats`
  prints the whole-archive census from the table itself, so a type that arrived
  weeks ago and never recurred is still visible. It is a **signal, never a
  failure** — an unmodelled type never sets `ok=false` and never changes an exit
  code. The convention for the next one: give it a modelled route, or let it land
  in `session_records`. Never drop it.
- **Fork lineage, relocation and worktree binding on `sessions`** —
  `forked_from_session_id`, `forked_from_uuid`, `fork_context_length`,
  `fork_agent_id` (from `fork-context-ref`), `current_cwd` (from `relocated`, the
  v2.1.169 `/cd`), and `worktree_session`. A fork, a relocated session and an
  ordinary one were indistinguishable before.
- **Claude Code's own cost ledger on `sessions`** — `cost_state`,
  `reported_cost_usd`, `reported_{total,api,tool}_duration_ms`,
  `reported_lines_{added,removed}`, `has_unknown_model_cost`, from the
  `cost-state` record — plus **`session_kind`** (`bg` marks a background
  session).
- **`v_session_cost_drift`** — csd's computed cost against the harness's own
  reported cost, carrying what is needed to *attribute* a gap rather than just
  display one: `drift_usd` / `drift_pct`, `sidechain_messages`,
  `fallback_messages`, `unpriced_messages`, `reported_model_usage`. A session
  with no `cost-state` yet has a NULL reported side, never a fake zero.
- **`v_message_cost.api_message_ratio`** — `priced_messages / distinct
  api_message_id`. One API response can land as several `messages` rows, and the
  ratio measures it: **1.808-2.443** on four real sessions, tracking the observed
  cost drift closely. See *Notes* for why the dedup is deferred.
- **`messages` usage sub-fields** — `thinking_tokens`, `server_tool_use`,
  `iterations` (JSONB) + `iteration_count`, so a model **fallback** is at least
  visible; plus `messages.effort` and `messages.session_kind`.
- **`csd angles sessions` FLAGS column** — `bg` (background session), `mv` (the
  session relocated; the PROJECT column is derived from where it was *filed*,
  not where it now is), `fk` (a fork, inheriting another session's context),
  `—` for an ordinary row.
- **Undecodable-project reporting.** Claude Code's project-slug encoding maps
  both `/` and `.` to `-` and is **not invertible**, so the naive decode is wrong
  for every dot-directory (`-Users-andrew--claude`) and every worktree project.
  Sync now uses the transcript's own `cwd` as ground truth when it re-encodes to
  the directory name, and otherwise **flags** the project and reports the count
  rather than recording a garbage `decoded_path`. `projects.encoded_path` was and
  remains the exact key.
- **`tool_labels.py`** — one label table for the tools the v2.1.161-258 window
  added (the `TaskCreate` / `TaskUpdate` / `TaskOutput` / `ListAgents`
  background-task family, and friends), shared by the console renderers and
  `csd angles`, so the two cannot drift.

### Changed
- **Model pricing: the Claude 5 family, solved from the harness's own ledger.**
  `cost-state.totalCostUSD` is a second independent number, so the rates were
  *derived* rather than assumed: **opus-5 $5/$25**, sonnet-5 $2/$10 (exact on
  16/16 rows), fable-5 and fable-5-1 $10/$50, mythos-5 / mythos-5-1 at the same
  tier. **`claude-fable-5-1` alone carries a 0.025 cache-read multiplier**
  ($0.25/MTok) — exact on 7/7 rows, and a 5.1-only change; every other family
  member keeps 0.10.
- **Opus 4.6 / 4.7 / 4.8 corrected to $5/$25.** The generic `claude-opus-4`
  pattern priced them at 15/75 — 3× over — and longest-pattern-wins now routes
  them to their own rows. Together with the 5 family, **unpriced assistant
  messages went from 338,515 to 0.**
- **`csd angles` sees the whole orchestration family.** `angle_agents` covered
  `Agent` / `SendMessage` / `TaskStop` only, so a turn that created three
  background tasks and updated two of them produced one "stop" headline or
  nothing at all.

### Fixed
- **User records are classified on evidence, not on content shape.** A prompt
  whose content happened to be a *list* was labelled `tool_result`; the
  classifier now looks for an actual `tool_result` block. **+7,179 rows** moved
  to `prompt`, with `prompt_text` backfilled for them.
- **Unknown content blocks are kept, not dropped.** A block type the parser does
  not model is preserved verbatim in `content_blocks.block_payload` instead of
  vanishing between JSONL and Postgres.
- **Structured tool-result overflow is archived.** Subagent overflow discovery
  matched `tool-results/*.txt` only, so every `.json` overflow file — the
  structured ones — was skipped.
- Undecodable project names no longer crash or mis-file the sync (see above),
  and the console/renderer surfaces label the new tools instead of showing a
  bare tool name.

### Notes
- **Schema v9 migration is additive, idempotent and automatic** — it applies on
  the next `initialize()` (any `csd ingest`, or the launchd sweep) with no
  manual step. Column additions sit behind an `information_schema` guard keyed on
  the first new column, so the ACCESS EXCLUSIVE `ALTER` fires once and not on
  every 5-minute tick; `ADD COLUMN` without a default is O(1) in PG11+. No column
  is dropped, no column retyped, no row deleted: `messages.forked_from` is
  retained as **legacy**.
- **Backfills are bounded and resumable** (`postgres.BACKFILLS`,
  `SessionArchive.run_backfills`). Each walks the `messages` primary key in 20K
  committed batches with a cursor in `metadata`, spends at most 20s per
  `initialize()`, resumes on the next sweep, is `IS DISTINCT FROM`-guarded so a
  re-run writes nothing, and isolates its own failures so a broken backfill can
  never stop ingest. A single long `UPDATE` is exactly the *idle in transaction*
  shape that once convoyed this database for ~9h.
- **The `api_message_ratio` dedup is deliberately deferred.** Deduplicating
  `v_message_cost` by `api_message_id` is the right fix, and it changes every
  historical cost number in the archive — it gets its own change with its own
  verification, not a footnote in this one. Until then `v_session_cost_drift`
  *measures* the over-count instead of hiding it.
- No new CLI commands or flags; every surface above is an addition to an existing
  one.

## [3.22.1] - 2026-09-02

### Fixed
- **Summary tab verdicts: the write that LANDED owns the verdict.** The
  entries list deduped by app:path with "latest write wins", so a session
  that tried a path three times — a create refused by validation, the
  corrected create that landed, a retry answered *already exists* — showed
  the entry as `error` while it sat right there in kmcp (a real controltech
  session rendered 9 of 14 entries as errors that way). Ranking is now by
  evidence strength: a landed write > a refusal > a dry-run, latest among
  equals. An *already exists* refusal is classified `exists`, not `error` —
  it is proof the entry is there, not a failed write — and the verdict chip
  carries the refusal text in its tooltip. A `create_relationship` link
  (`related`) proves the entry exists but is not a write of it, so it ranks
  with `exists` and never outranks the create it decorates.

## [3.22.0] - 2026-09-02

### Added
- **Summary rail tab** (`GET /api/summaries`). A seventh right-rail tab that
  answers, for one session: *has this been captured, by what, and what landed
  in the corpus* — plus the controls to do it. Three bands.
  - **Controls** — **Summarize** (auto scope; the pass-aware label the chat
    header already uses: "Summarize NEW work since ‹date› (pass N)", disabled
    with the reason when the grade is `none`), **Full re-capture**
    (`delta:"off"`, always available), **Capture events**, and ⟳. Buttons
    disable with their reason while a run is in flight. The current grade is
    stated in words underneath (watermark, source, pass, prior entry).
  - **Runs** — one row per summary run, newest first, merged from three
    independent sources: the `summary_passes` ledger (the only one that sees
    launchd's phase-4 local-Ollama passes, marked `phase-4`), the console-minted
    child runs on `meta.json` (durable across a restart), and the in-process
    `SUMMARY_RUNS` tracker (rc + in-flight). A row links into the child run's
    own transcript, exactly as the summarizing chip does.
  - **Entries written** — grouped by entity type, each `app:path` a **link into
    /browse**. Deduped across the session's own transcript and every run's,
    extracted by the *Context tab's own* write extractor (so the two can never
    disagree about a created/updated/dry-run/error verdict), and joined to the
    knowledge DB in ONE query so a written ref is confirmed present in the
    corpus — or flagged `not in corpus` when it is not.
  On-demand only (it grades the transcript tail and re-parses each run's
  JSONL); it re-polls only while a run is in flight, never on the nav poll.
- **Capture events** — the same off-session dispatch through the same
  `spawn_claude(action="summarize")` permission envelope, with the
  /session-summary skill's **own** `--events` override appended (Step 1 CLI
  overrides → Step 3's thin-changelog path: changelog `event` entries only,
  Steps 5–8 skipped, so no session entry, no lessons, no tasks). It writes no
  session entry, so it deliberately takes **no `summary_passes` claim** — a
  ledger row would advance the pass number and imply a watermark the reconcile
  gate would later stamp — and it never archives. `POST /api/summarize` takes
  `events: true`.
- `summary_pass` / `summary_kind` on a child run's `meta.json` overlay, so a
  session's whole run history survives a console restart.

### Notes
- Degrade doctrine unchanged: `/api/summaries` never raises and never 500s for
  a degraded source. An unreachable archive or knowledge DB costs only the part
  that needed it and lands in `warnings[]`; the transcript-derived half still
  renders. A child (`parent:agent`) key is refused 400, pointing at the parent.

## [3.21.0] - 2026-09-02

### Added
- **Commits grouped by PR** (Git rail, and the repo detail's commit list).
  The by-branch partition answers "part of which branch"; on a squash-merge
  trunk that is one useless group called `main` while the PR cards underneath
  it repeat the very same commits. Every commit now resolves to the pull
  request it landed as — strongest evidence first: a `Merge pull request #N`
  subject (whose side commits, `M^1..M^2`, are part of that PR too), a
  `… (#N)` squash marker, gh's own `mergeCommit.oid`, then membership in a
  PR's commit oids. Resolution is **pure code over data already fetched** — no
  extra git call, no model. A number with no gh row (gh unavailable, or the PR
  older than the fetched 30) still forms a group, headed `#N` with the
  commit's own subject as its title and no invented state or link.
- **Group headers** carry the number (a real hyperlink when `origin` is
  GitHub, plain text otherwise), the PR title, a state chip in the same
  colours the PR cards use, the checks glyph, the head branch, the merge age
  and a commit count. Groups are newest-first; commits belonging to no PR
  trail in a single **`no PR · direct to ‹branch›`** group, and unmerged local
  branches keep their own groups. Collapse state is remembered in
  localStorage, as the branch groups already were.
- **`by PR | by branch` toggle** in the section header, remembered in
  localStorage. The server ships **both** partitions in one payload
  (`groups` + `group_alt`, over the same commit dicts by reference), so the
  swap is a re-render with no fetch and no git. `group_mode` names the
  default: by PR whenever at least one commit resolves, else the by-branch
  view that has always shipped.
- **The two views are one.** With PR grouping active, the *recently merged /
  closed* PR cards that already stand as commit groups above are not repeated
  — the list shows only PRs outside the window, or collapses to
  `N shown above as commit groups`. Open PRs keep their cards.

### Changed
- `group_commits(commits, topo, gh, group_by="branch")` gained the mode
  argument; the default is unchanged, byte-for-byte. Groups now carry
  `kind: "pr" | "branch" | "direct"`, and PR groups a
  `pr: {number, title, state, draft, checks, branch, url, merged_at, known}`.
  `commit_groups_payload()` is the one seam both surfaces call.
- `gh pr list` now also fetches `mergeCommit` (stripped from the wire payload;
  it exists for attribution only).
- Doctrine unchanged: grouping never raises. A failure yields `[]` and a
  `group_note`, and both surfaces fall back to the flat commit list they
  already ship — commits are never withheld because their grouping failed.

## [3.20.0] - 2026-09-02

### Changed
- **CR's BEFORE is now an honest token estimate, and the rows close.** The
  context surface was `len(json.dumps(content))` — the transcript's BYTES, not
  what the API counts. On a real 12-turn session that read ≈337K against a true
  ≈157K: one pasted screenshot's base64 counted as ≈119K "tokens" (an image
  bills at ~1-2K), 88 thinking **signatures** counted as ≈51K (opaque
  provenance strings, not tokens), and the JSON quoting itself was counted.
  Each block is now priced the way the API would: text at chars/4, a
  `tool_use` input at `len(json.dumps(input))/4`, an image at (w×h)/750 capped
  at 1600 (dimensions sniffed from the decoded PNG/JPEG/GIF/WEBP header, with a
  documented flat estimate when the format is unreadable — never base64/4).
  Signatures, image payloads and JSON envelope are **excluded**, and reported
  as `excluded` so the gap between file size and estimate is stated rather than
  swallowed. `manifest.totals.est_tokens` is now **defined as Σ rows** — the
  opaque `fixed_chars` bucket (882K of 1.35M chars on that session, which is
  why the groups could never add up to BEFORE) is gone, pinned by a test.
  Preview and fork report the same measure, so the rail, the preview dialog and
  the forged fork can never disagree.

### Added
- **Every source is a row, and every row is controllable.** New manifest row
  kinds: `tool_use` (assistant tool INPUTS — file bodies in a Write, kmcp
  documents in an import_entries: ≈38K real tokens that were previously
  invisible), `image` (one row per image block, wherever it sits — user paste
  or tool_result sub-block), and `other` (any unrecognised block, locked but
  counted). Both new kinds dedup by digest and default to keep-when-recent.
  `apply_stubs`/`forge_fork` honour them: a stubbed tool_use keeps its `id` and
  `name` and swaps only its input for a breadcrumb (so the tool_use/tool_result
  pair still matches), and a stubbed image becomes
  `[image removed by CR: WxH, ~N tokens]` — the fork stays a valid transcript.
- **Per-source control in the CR rail.** Every group header expands (▸/▾,
  remembered in localStorage) into an itemized list — one line per source,
  heaviest first, with turn number, breadcrumb, size, a duplicate marker and
  per-item keep/stub/ref buttons wired to the same state as the group buttons
  and the stream chips. Capped at 40 with a "show all N" toggle. Group headers
  now read `26/183 · ≈7.7K kept of ≈66.2K`, and the panel shows AFTER's
  arithmetic in full (`kept + stubs N·10 + cart R refs = AFTER`), so the number
  is derivable by eye. Tool rows in the stream grew a second chip for their
  input (`in ☑`) beside the result chip.
## [3.19.1] - 2026-09-02

### Fixed
- **Expanding a tool row now shows its result.** The lazy `/api/tool_result`
  fetch (3.15.1) sent `curSession` — the rendered session *payload* — as the
  `id`, so every expand asked the server for `[object Object]` and rendered
  *result unavailable — session not found*. It now sends `curId`, the session
  id every other lazy endpoint already uses.

## [3.19.0] - 2026-09-02

### Added
- **The Σ chip — summarization state at a glance.** Every sidebar row now
  carries a third presence chip beside `T` (tl;dr) and `⧗` (timeline), and it
  answers the one question the console could never answer without opening
  something: *has this session been captured to kmcp, and is there anything new
  since?* Six states, colored like their siblings — dashed/dim `never
  summarized`, green `summarized — nothing new since <date>`, amber
  `summarized — NEW work since <date>` (the OPEN-delta case: needs
  re-capture), pulsing blue `summary running`, red `summarization failed`
  (a `summarize_attempts` backoff row, or a failed console run), and a solid
  grey `unknown` when the archive could not be asked. The tooltip carries the
  next pass number, the watermark date, the prior kmcp entry ref, the record
  count after the watermark and the grader's own reason. A tap opens the
  digest reader — the popover that already carries the close-out actions.
- The same state, **in words**, in two more places: a `Σ …` chip in the chat
  header beside the summarizing-run chip (state vs. last run — they answer
  different questions), and a line at the digest reader's foot directly above
  Summarize / Sum + archive / Archive, so the reader says whether a session is
  already captured *before* the button is pressed.
- `summary` in the `/api/sessions` row payload (and in `/api/session`), the
  same field family as `tldr` and `timeline`: `{state, pass, since, prior,
  records, source, attempts, reason, checked_at, stale, pending}`.

### Changed
- **One grader, one seam.** Nothing new classifies a delta: the chip is graded
  by `summarize.summary_scope_report()` → `_delta_gate` — the same gate the
  launchd timer queues on, the Summarize button labels itself from and
  `csd summary-scope` prints — so the chip, the button, the timer and the
  `/session-summary` skill can never disagree.
- **The nav poll never grades.** `summary_presence()` is a pure read of a
  memo keyed on the transcript's `(mtime_ns, size)` — the same signature the
  tldr/timeline presence chips memoize on — and never opens a connection. A
  single background refresher (`_summary_refresher`, `CSD_SUMMARY_REFRESH_S`,
  default 120s, serial and staggered) grades the wanted rows newest-first and
  mirrors the memo to `$CSD_STATE_DIR/console/summary.json`, so a restarted
  console does not show an all-grey sidebar. Same seam as tldr/timeline and
  the repos lens: the endpoint reads, the worker writes. A settled summarize
  pass invalidates its row so the next tick re-grades it.
- Degrade, never block: no DSN, an unreachable archive, a raising grader, or a
  row not yet graded all land as `unknown` with the reason in the tooltip —
  never an error, never a slow poll. The grader's own "prior capture
  unresolved" degrade is mapped to `unknown` rather than `never`, because
  "never summarized" is a claim an unanswered archive cannot support.
## [3.18.1] - 2026-09-02

### Fixed
- **Comments are tinted inside fenced code blocks.** A code fence in a chat
  turn rendered as one flat run of `--ink`, so the `# why` beside each line
  read as more code. `mdCode` now wraps comments in a muted italic span
  (`--md-comment`), comments ONLY — a full highlighter is a library, and
  mdLite has none by design. The fence's language tag picks the grammar
  (`#`, `//` + `/* */`, `--`, `<!-- -->`, `;`); an untagged fence gets the
  `#` grammar since shell and python are what lands untagged. `#` counts only
  at line start or after whitespace and never inside a quoted string, so
  `$#`, `url#frag` and `"#1"` stay code; a string resets at end of line so
  one stray apostrophe cannot swallow the block. An unterminated fence (a
  turn cut mid-stream) now keeps its `data-lang` label too.

## [3.18.0] - 2026-09-01

### Added
- **Runs tab.** The sidebar's top tabs are now Projects | Runs | Archive. A
  run is an off-session summary session the console spawned (`summary_of`
  set): it is work *about* a session, not a session of yours, so it lives on
  its own tab with a running count (`n▶`) and never pads the Projects list or
  its repo pills. Repo pills, search, sort and the tl;dr-all batch all
  partition per tab; the Archive tab is the archived set as fetched, runs
  included.

## [3.17.0] - 2026-09-01

### Added
- **The off-session summary run is now a session you can open.** Summarize
  used to spawn `claude -p /session-summary <uuid>` and remember only a
  `running | done | failed` flag keyed by the parent, so the run was invisible
  except as a bare-uuid row appearing in the sidebar. The console now mints
  the child's session id itself (`--session-id`, the same doctrine as Fork),
  registers the process under it — so **Stop in the child view aims at the
  run**, and the parent is no longer held by the two-writer guard for a
  process that never writes to it — titles it `Summary of ‹parent› (pass N)`
  before it exists, and records the link both ways in the meta overlay
  (`summary_of` on the child, `summary_child` on the parent — durable across
  a console restart, unlike the in-memory flag). Surfaces: the parent's
  `summarizing…` / `summary done` chip is a **link into the run** (after a
  restart it still opens the last run as `⧉ last summary run`), the run's
  header carries a `← summarizing ‹parent›` back-link, its sidebar row shows a
  `⧉ summary run` marker, and the parent's `busy →` meta segment opens it.
  `/api/summarize` returns `child_session`; `/api/session` carries
  `summary_child`, `summary_run` (child, pass, started, ended, rc, log) and
  `summary_of`.

## [3.16.0] - 2026-09-01

### Added
- **Console Context tab — SURFACED and WRITTEN, beside CONSUMED.** The tab
  answered only *what did this session open*. It now answers the other two
  thirds of the same ledger:
  - **Surfaced entries** — every ref a search OFFERED the session, grouped
    under the query that surfaced it (collapsible, labelled with the tool:
    `search` / `hybrid_search` / `traverse_graph` / `list_by_tag` …), deduped
    per query, counted `×N` across queries, and each marked **consumed ✓** or
    **○ not** by cross-referencing the read events. The useful signal is the
    hits the session never opened; the group header carries `n unopened` and
    the section header `k/n opened`.
  - **Written / modified entries** — the kmcp writes, with an operation chip
    (created / updated / patched / related / renamed / moved / deleted /
    tagged / **dry-run**), an error chip carrying the failure text, `×N` for
    repeated writes to one entry, and a link into `/browse`. A `dry_run` that
    was never followed by a real write renders **as a dry-run**, not as a
    write.
  - A `✍ written` counter joins consumed/surfaced, with `n dry-run` /
    `n failed` breakdowns.

### Fixed
- **kmcp writes were being DROPPED from `/api/session` entirely.** The
  extraction loop's generic-tool branch is gated on `base is None`, which a
  resolved kmcp base never is — so every `import_entries`, `create_entry`,
  `update_entry`, `patch_content`, `create_relationship`, `rename_entry`,
  `move_entry`, `add_entry_tag`, `delete_entry`, `import_lessons`,
  `stage_template` and `upload_file` a session ever made fell through every
  branch and vanished. They are now a first-class `kind: "write"` event
  (`WRITE_TOOLS`, seeded from the angles `W` extractor's own `_WRITE_TOOLS` so
  `csd angles` and the console cannot drift), carrying tool / op / app / path /
  refs / `dry_run` / `via` / error, with the tool_result's `created[]` /
  `updated[]` as the authority on created-vs-updated. The Writes tab's
  `KMCP_WRITE_BASES` filter had been matching that never-emitted list — it is
  replaced by a one-line cross-link to the Context tab, and the tab stays the
  FILE ledger.
- **A refused write read as a successful one.** `import_entries` answers a
  refusal as `{"error": …, "message": …}` with `is_error` unset — "Missing
  input", "Import path not allowed", "Import failed". Those now surface as
  errors. On one real session this turned 9 silent successes into 9 visible
  failures.
- **Text-rendered search results surfaced nothing.** kmcp returns the compact
  `<query> · N hits · app=…` / `types: …` / indented `etype  app:path  title`
  form at `detail=minimal`, which is not JSON — `_parse_search_result` gave up
  on it. It is parsed now (`_parse_search_text`), so those searches carry refs
  like the JSON ones. `_parse_search_result` also accepts a bare list and the
  `entries` / `nodes` / `items` / `relationships` containers the other
  SURFACE_TOOLS return, and reports `returned` / `shown` against a 40-hit cap
  rather than silently truncating at 12.
- The knowledge-cli **Bash shim** is admitted for writes as it already was for
  reads (`via: "cli"`), including `--dry-run`.

## [3.15.1] - 2026-09-01

### Fixed
- **Tool rows expanded to the command, never the result.** A Bash row's
  caret re-showed the command text; the tool_result — already parsed
  server-side for its byte count — never reached the client. Now every tool
  row with a result expands to it: `GET /api/tool_result?id=&tid=` returns
  one result's text verbatim (no truncation; a result absent from the
  transcript reports "not recorded", distinct from empty output), fetched
  lazily on expand and memoized per tool_use id so the polled session payload
  never carries result bodies. Bash rows show command then result; error
  results carry a red rail.

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
  chips, the priority flag — with the repo name at the line's left edge (shown
  while `all` is in effect; a selected pill already names it). Every action is
  a uniform `.rbtn` in one flex row
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
