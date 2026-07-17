"""Incremental sync of Claude Code session JSONL into the Postgres archive.

Phase 3 of claudecode:design/claude-session-db-postgres-archive.

- Discovery is glob-based: every `*.jsonl` under ~/.claude/projects (main
  sessions + subagent sidechains). sessions-index.json is NOT consulted.
- Sync signal is filesystem mtime (st_mtime_ns); a file is re-ingested only when
  its mtime changes (or --force).
- Ingest is idempotent: messages/attachments/system_events upsert by uuid; all
  child rows are cleared per source_file before re-insert.
- No truncation: content blocks and tool results are stored verbatim; the
  largest tool results are pulled from the tool-results/ overflow files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .jsonl_records import (
    JSONLParser,
    UserMessage,
    AssistantMessage,
    SystemMessage,
    ThinkingBlock,
    TextBlock,
    ToolUseBlock,
)
from .postgres import SessionArchive, resolve_dsn
from .subagent import load_external_tool_results, read_agent_meta
from .tool_tldr import tldr_result
from .transcript_analyzer import classify_error


# Volatile background-task outputs live at
# /private/tmp/claude-<uid>/<proj-encoded>/<session-uuid>/tasks/<task>.output
# and are wiped on reboot — the sweep below is their only durable copy.
TASKS_TMP_BASE = Path("/private/tmp")
TASK_OUTPUT_MAX_BYTES = 5 * 1024 * 1024


def decode_project_path(encoded: str) -> str:
    """Decode a project dir name: -Users-me-GitHub-x -> /Users/me/GitHub/x."""
    if encoded.startswith("-"):
        encoded = encoded[1:]
    return "/" + encoded.replace("-", "/")


@dataclass
class SyncStats:
    files_found: int = 0
    files_synced: int = 0
    files_skipped: int = 0
    messages: int = 0
    content_blocks: int = 0
    tool_results: int = 0
    attachments: int = 0
    system_events: int = 0
    queue_operations: int = 0
    file_snapshots: int = 0
    pr_links: int = 0
    agent_tasks: int = 0
    overflow_results: int = 0
    task_outputs: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            "Sync complete:\n"
            f"  Files: {self.files_found} found, {self.files_synced} synced, "
            f"{self.files_skipped} skipped\n"
            f"  Messages: {self.messages:,}\n"
            f"  Content blocks: {self.content_blocks:,}\n"
            f"  Tool results: {self.tool_results:,} ({self.overflow_results:,} from overflow)\n"
            f"  Attachments: {self.attachments:,}\n"
            f"  System events: {self.system_events:,}\n"
            f"  Queue ops: {self.queue_operations:,}  File snapshots: {self.file_snapshots:,}\n"
            f"  PR links: {self.pr_links}  Agent tasks: {self.agent_tasks}\n"
            f"  Task outputs: {self.task_outputs}\n"
            f"  Errors: {self.errors}"
        )

    def oneline(self) -> str:
        """Compact single-line summary — only non-zero record counts (errors always)."""
        counts = [
            ("msg", self.messages), ("blocks", self.content_blocks),
            ("results", self.tool_results), ("attach", self.attachments),
            ("sysevents", self.system_events), ("queueops", self.queue_operations),
            ("snapshots", self.file_snapshots), ("prs", self.pr_links),
            ("agents", self.agent_tasks), ("taskout", self.task_outputs),
        ]
        parts = [f"{n:,} {label}" for label, n in counts if n]
        body = ", ".join(parts) if parts else "no new records"
        return (f"Synced {self.files_synced}/{self.files_found} files · "
                f"{body} ({self.errors} errors)")


class SessionSync:
    """Sync Claude Code session JSONL into the Postgres archive."""

    def __init__(self, dsn: Optional[str] = None, claude_dir: Optional[Path] = None,
                 verbose: bool = False):
        self.dsn = resolve_dsn(dsn)
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.projects_dir = self.claude_dir / "projects"
        self.verbose = verbose
        self.parser = JSONLParser(self.claude_dir)
        self.archive = SessionArchive(self.dsn)
        self._project_cache: dict[str, int] = {}

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # -- discovery ----------------------------------------------------------

    def enumerate_files(self) -> list[tuple[Path, bool]]:
        """Return [(path, is_subagent)] for all session + subagent JSONL files.

        Main session files are flat at the project level (`<proj>/<uuid>.jsonl`).
        Subagent sidechains live anywhere under a `subagents/` directory — both
        the flat `subagents/agent-*.jsonl` and the nested workflow form
        `subagents/workflows/wf_*/agent-*.jsonl`.
        """
        files: list[tuple[Path, bool]] = []
        if not self.projects_dir.exists():
            return files
        mains = set(self.projects_dir.glob("*/*.jsonl"))
        for main in mains:
            files.append((main, False))
        for p in self.projects_dir.rglob("*.jsonl"):
            if p not in mains and "subagents" in p.parts:
                files.append((p, True))
        return files

    # -- entry point --------------------------------------------------------

    def sync_all(self, force: bool = False, rebuild: bool = False) -> SyncStats:
        stats = SyncStats()
        with self.archive:
            if rebuild:
                self.log("Rebuilding schema (DROP + CREATE)...")
                self.archive.drop_all()
            self.archive.initialize()

            files = self.enumerate_files()
            stats.files_found = len(files)
            self.log(f"Found {len(files)} JSONL files")

            for path, is_sub in files:
                try:
                    if self._sync_file(path, is_sub, stats, force):
                        stats.files_synced += 1
                    else:
                        stats.files_skipped += 1
                    if not is_sub:
                        # Task outputs are swept even for mtime-skipped sessions:
                        # a background task can finish after the JSONL settles.
                        self._sweep_task_outputs(path.stem, path.parent.name, stats)
                except Exception as e:  # noqa: BLE001 — keep going on per-file errors
                    # Roll back the aborted transaction so the connection recovers
                    # and subsequent files aren't poisoned by InFailedSqlTransaction.
                    try:
                        if self.archive.conn is not None:
                            self.archive.conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    self.log(f"  ERROR syncing {path}: {type(e).__name__}: {e}")
                    stats.errors += 1

            self.log("Recomputing session aggregates...")
            self.archive.recompute_session_aggregates()
        return stats

    # -- per-file -----------------------------------------------------------

    def _project_id_for(self, project_encoded: str) -> int:
        if project_encoded not in self._project_cache:
            decoded = decode_project_path(project_encoded)
            self._project_cache[project_encoded] = self.archive.get_or_create_project(
                project_encoded, decoded
            )
        return self._project_cache[project_encoded]

    def _sync_file(self, path: Path, is_subagent: bool, stats: SyncStats, force: bool) -> bool:
        source_file = str(path)
        st = path.stat()
        mtime_ns = st.st_mtime_ns

        if not force and not self.archive.needs_sync(source_file, mtime_ns):
            return False

        # Owning session id + project + session dir (for overflow + file-history).
        # Subagents may nest arbitrarily under `subagents/` (e.g. workflow agents
        # at <uuid>/subagents/workflows/wf_*/agent-*.jsonl), so locate the
        # `subagents` path component rather than assuming a fixed depth.
        if is_subagent:
            parts = path.parts
            idx = parts.index("subagents")
            owning_session_id = parts[idx - 1]       # <uuid> dir before subagents/
            project_encoded = parts[idx - 2]
            session_dir = Path(*parts[:idx])         # <proj>/<uuid>/
        else:
            session_dir = path.parent / path.stem    # <proj>/<uuid>/
            owning_session_id = path.stem
            project_encoded = path.parent.name

        self.log(f"  syncing {'[sub] ' if is_subagent else ''}{path.name}")
        self.archive.clear_file_data(source_file)
        records = self.parser.parse_file(path)

        # Overflow tool results (full content for the largest results)
        overflow = load_external_tool_results(session_dir)

        self._insert_records(records, source_file, owning_session_id, overflow, stats)

        # A main file defines a session row (subagent messages roll up into it);
        # a subagent file additionally defines a CHILD session row keyed
        # "<parent>:<agent_id>" so the sidechain is a navigable identity.
        if not is_subagent:
            project_id = self._project_id_for(project_encoded)
            self._upsert_session(records, owning_session_id, project_id, source_file, st)
        else:
            self._upsert_subagent_session(records, path, owning_session_id,
                                          project_encoded, source_file, st)

        self.archive.commit()

        record_count = sum(len(v) for v in records.values())
        self.archive.update_sync_state(source_file, mtime_ns, record_count, st.st_size)
        return True

    def _sweep_task_outputs(self, session_id: str, project_encoded: str,
                            stats: SyncStats) -> None:
        """Sweep /private/tmp/claude-*/<proj>/<sid>/tasks/*.output into the
        archive — the existing external-overflow capture pattern applied to the
        VOLATILE task-output files (wiped on reboot; this is the only durable
        copy). Verbatim, keyed (session_id, task filename), idempotent by
        mtime, bounded at TASK_OUTPUT_MAX_BYTES with a truncation note.
        Symlinks resolving into ~/.claude/projects are skipped: their target IS
        a subagent transcript the archive already holds losslessly."""
        dirs = list(TASKS_TMP_BASE.glob(
            f"claude-*/{project_encoded}/{session_id}/tasks"))
        if not dirs:
            return
        known = self.archive.get_task_output_mtimes(session_id)
        for d in dirs:
            for f in sorted(d.glob("*.output")):
                try:
                    if f.is_symlink() and self.projects_dir in f.resolve().parents:
                        continue
                    st = f.stat()
                except OSError:
                    continue
                if known.get(f.name) == st.st_mtime_ns:
                    continue
                truncated = st.st_size > TASK_OUTPUT_MAX_BYTES
                try:
                    with open(f, "rb") as fh:
                        data = fh.read(TASK_OUTPUT_MAX_BYTES)
                except OSError:
                    continue
                content = data.decode("utf-8", errors="replace")
                if truncated:
                    content += (f"\n\n[csd: truncated at {TASK_OUTPUT_MAX_BYTES}"
                                f" of {st.st_size} bytes]")
                self.log(f"  task output {session_id[:8]}/{f.name}"
                         f" ({st.st_size:,}b{' truncated' if truncated else ''})")
                self.archive.upsert_task_output({
                    "session_id": session_id,
                    "task_name": f.name,
                    "content": content,
                    "char_count": len(content),
                    "truncated": truncated,
                    "file_size": st.st_size,
                    "file_mtime_ns": st.st_mtime_ns,
                    "source_path": str(f),
                })
                stats.task_outputs += 1

    # -- record -> row builders --------------------------------------------

    def _insert_records(self, records: dict, source_file: str, owning_session_id: str,
                        overflow: dict[str, str], stats: SyncStats) -> None:
        msg_rows: list[dict] = []
        cb_rows: list[dict] = []
        tr_rows: list[dict] = []
        att_rows: list[dict] = []
        sys_rows: list[dict] = []
        qo_rows: list[dict] = []
        pr_rows: list[dict] = []
        agent_rows: list[dict] = []

        # tool_use_id -> tool_name, so error results can be classified with the
        # correct tool (classify_error needs the name to suppress the phantom_tool
        # false-positive on Bash output).
        tool_name_by_id: dict[str, str] = {}
        for msg in records.get("assistant", []):
            for blk in msg.content_blocks:
                if isinstance(blk, ToolUseBlock):
                    tool_name_by_id[blk.id] = blk.name

        for msg in records.get("user", []):
            msg_rows.append(self._user_row(msg, owning_session_id, source_file))
            for blk in msg.tool_result_blocks:
                tr_rows.append(self._tool_result_row(msg, blk, owning_session_id,
                                                     source_file, overflow, stats,
                                                     tool_name_by_id))

        for msg in records.get("assistant", []):
            msg_rows.append(self._assistant_row(msg, owning_session_id, source_file))
            sid = msg.session_id or owning_session_id
            for idx, blk in enumerate(msg.content_blocks):
                cb_rows.append(self._content_block_row(msg.uuid, sid, idx, blk, source_file))

        for att in records.get("attachment", []):
            att_rows.append({
                "uuid": att.uuid,
                "session_id": att.session_id or owning_session_id,
                "parent_uuid": att.parent_uuid,
                "ts": att.timestamp,
                "attachment_type": att.attachment_type,
                "attachment": att.attachment,
                "is_sidechain": att.is_sidechain,
                "source_file": source_file,
                "source_line": None,
            })

        for ev in records.get("system", []):
            sys_rows.append(self._system_row(ev, owning_session_id, source_file))

        for op in records.get("queue_operation", []):
            qo_rows.append({
                "session_id": op.session_id or owning_session_id,
                "ts": op.timestamp,
                "operation": op.operation,
                "content": op.content,
                "source_file": source_file,
                "source_line": None,
            })

        for pr in records.get("pr_link", []):
            pr_rows.append({
                "session_id": pr.session_id or owning_session_id,
                "pr_number": pr.pr_number,
                "pr_url": pr.pr_url,
                "pr_repository": pr.pr_repository,
                "ts": pr.timestamp,
                "source_file": source_file,
                "source_line": None,
            })

        for al in records.get("agent_lifecycle", []):
            agent_rows.append({
                "key": al.key,
                "agent_id": al.agent_id,
                "started": al.kind == "started",
                "result": al.result,
                "source_file": source_file,
            })

        # Batched inserts
        self.archive.insert_messages(msg_rows)
        self.archive.insert_content_blocks(cb_rows)
        self.archive.insert_tool_results(tr_rows)
        self.archive.insert_attachments(att_rows)
        self.archive.insert_system_events(sys_rows)
        self.archive.insert_queue_operations(qo_rows)
        self.archive.insert_pr_links(pr_rows)
        self.archive.insert_agent_tasks(agent_rows)

        # File-history snapshots need per-row generated ids
        for snap in records.get("file_history", []):
            self._insert_snapshot(snap, owning_session_id, source_file)
            stats.file_snapshots += 1

        stats.messages += len(msg_rows)
        stats.content_blocks += len(cb_rows)
        stats.tool_results += len(tr_rows)
        stats.attachments += len(att_rows)
        stats.system_events += len(sys_rows)
        stats.queue_operations += len(qo_rows)
        stats.pr_links += len(pr_rows)
        stats.agent_tasks += len(agent_rows)

    def _user_row(self, msg: UserMessage, owning_session_id: str, source_file: str) -> dict:
        return {
            "uuid": msg.uuid,
            "session_id": msg.session_id or owning_session_id,
            "parent_uuid": msg.parent_uuid,
            "ts": msg.timestamp,
            "role": "user",
            "message_type": "prompt" if msg.is_direct_prompt else "tool_result",
            "prompt_text": msg.prompt_text,
            "prompt_id": msg.prompt_id,
            "permission_mode": msg.permission_mode,
            "is_meta": msg.is_meta,
            "is_compact_summary": msg.is_compact_summary,
            "source_tool_assistant_uuid": msg.source_tool_assistant_uuid,
            "source_tool_use_id": msg.source_tool_use_id,
            "is_sidechain": msg.is_sidechain,
            "agent_id": msg.agent_id,
            "slug": msg.slug,
            "cwd": msg.cwd,
            "git_branch": msg.git_branch,
            "cc_version": msg.version,
            "entrypoint": msg.entrypoint,
            "forked_from": msg.forked_from,
            "source_file": source_file,
            "source_line": None,
            "raw": msg.raw,
        }

    def _assistant_row(self, msg: AssistantMessage, owning_session_id: str, source_file: str) -> dict:
        u = msg.usage
        return {
            "uuid": msg.uuid,
            "session_id": msg.session_id or owning_session_id,
            "parent_uuid": msg.parent_uuid,
            "ts": msg.timestamp,
            "role": "assistant",
            "message_type": "response",
            "model": msg.model,
            "api_message_id": msg.message_id,
            "request_id": msg.request_id,
            "stop_reason": msg.stop_reason,
            "stop_details": msg.stop_details,
            "is_api_error": msg.is_api_error_message,
            "api_error_status": msg.api_error_status,
            "error_text": msg.error,
            "diagnostics": msg.diagnostics,
            "attribution_agent": msg.attribution_agent,
            "attribution_skill": msg.attribution_skill,
            "attribution_mcp_server": msg.attribution_mcp_server,
            "attribution_mcp_tool": msg.attribution_mcp_tool,
            "attribution_plugin": msg.attribution_plugin,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_input_tokens,
            "cache_creation_tokens": u.cache_creation_input_tokens,
            "ephemeral_5m_tokens": u.ephemeral_5m_tokens,
            "ephemeral_1h_tokens": u.ephemeral_1h_tokens,
            "service_tier": u.service_tier,
            "inference_geo": u.inference_geo,
            "speed": u.speed,
            "usage": u.raw or None,
            "is_sidechain": msg.is_sidechain,
            "agent_id": msg.agent_id,
            "slug": msg.slug,
            "cwd": msg.cwd,
            "git_branch": msg.git_branch,
            "cc_version": msg.version,
            "entrypoint": msg.entrypoint,
            "forked_from": msg.forked_from,
            "source_file": source_file,
            "source_line": None,
            "raw": msg.raw,
        }

    def _content_block_row(self, message_uuid: str, session_id: str, idx: int,
                          blk, source_file: str) -> dict:
        row = {
            "message_uuid": message_uuid, "session_id": session_id, "block_index": idx,
            "block_type": "", "content": None, "char_count": None, "signature": None,
            "tool_use_id": None, "tool_name": None, "tool_input": None,
            "tool_type": None, "mcp_server": None,
            "source_file": source_file, "source_line": None,
        }
        if isinstance(blk, ThinkingBlock):
            row["block_type"] = "thinking"
            row["content"] = blk.thinking
            row["char_count"] = blk.char_count
            row["signature"] = blk.signature
        elif isinstance(blk, TextBlock):
            row["block_type"] = "text"
            row["content"] = blk.text
            row["char_count"] = blk.char_count
        elif isinstance(blk, ToolUseBlock):
            row["block_type"] = "tool_use"
            row["tool_use_id"] = blk.id
            row["tool_name"] = blk.name
            row["tool_input"] = blk.input
            row["tool_type"] = blk.tool_type
            row["mcp_server"] = blk.mcp_server
        return row

    def _tool_result_row(self, msg: UserMessage, blk, owning_session_id: str,
                        source_file: str, overflow: dict[str, str], stats: SyncStats,
                        tool_name_by_id: dict[str, str]) -> dict:
        content = blk.content_text
        from_overflow = False
        # Prefer the verbatim overflow file when present (the largest results)
        full = overflow.get(blk.tool_use_id)
        if full is not None and len(full) > len(content):
            content = full
            from_overflow = True
            stats.overflow_results += 1
        is_error = bool(blk.is_error)
        error_class = (
            classify_error(tool_name_by_id.get(blk.tool_use_id, ""), content)
            if is_error else None
        )
        # Phase-2 derive-at-ingest: the free heuristic tldr (zero model calls).
        # The hybrid Haiku route for structured/multi-line-error bodies is a
        # later layer; see claudecode:design/session-archive-and-recompact.
        return {
            "message_uuid": msg.uuid,
            "session_id": msg.session_id or owning_session_id,
            "tool_use_id": blk.tool_use_id,
            "content_text": content,
            "tldr": tldr_result(content, is_error=is_error, error_class=error_class),
            "char_count": len(content),
            "is_error": is_error,
            "error_class": error_class,
            "block_count": len(blk.content_blocks),
            "tool_use_result": msg.tool_use_result,
            "from_overflow_file": from_overflow,
            "source_file": source_file,
            "source_line": None,
        }

    def _system_row(self, ev: SystemMessage, owning_session_id: str, source_file: str) -> dict:
        row = {
            "uuid": ev.uuid,
            "session_id": ev.session_id or owning_session_id,
            "parent_uuid": ev.parent_uuid,
            "ts": ev.timestamp,
            "subtype": ev.subtype,
            "level": ev.level,
            "content": ev.content,
            "duration_ms": ev.duration_ms,
            "message_count": ev.message_count,
            "url": ev.url,
            "compact_trigger": None,
            "compact_pre_tokens": None,
            "logical_parent_uuid": ev.logical_parent_uuid,
            "error_status": None,
            "error_type": None,
            "error_message": None,
            "retry_in_ms": ev.retry_in_ms,
            "retry_attempt": ev.retry_attempt,
            "max_retries": ev.max_retries,
            "is_sidechain": ev.is_sidechain,
            "slug": ev.slug,
            "source_file": source_file,
            "source_line": None,
            "raw": ev.raw,
        }
        if ev.compact_metadata:
            row["compact_trigger"] = ev.compact_metadata.trigger
            row["compact_pre_tokens"] = ev.compact_metadata.pre_tokens
        if ev.error_info:
            row["error_status"] = ev.error_info.status
            row["error_type"] = ev.error_info.error_type
            row["error_message"] = ev.error_info.error_message
        return row

    def _insert_snapshot(self, snap, owning_session_id: str, source_file: str) -> None:
        snapshot_row = {
            "session_id": owning_session_id,
            "message_id": snap.message_id,
            "snapshot_message_id": snap.snapshot_message_id,
            "ts": snap.timestamp,
            "file_count": snap.file_count,
            "has_backups": snap.has_backups,
            "is_snapshot_update": snap.is_snapshot_update,
            "source_file": source_file,
            "source_line": None,
        }
        backups = [{
            "file_path": b.file_path,
            "backup_file_name": b.backup_file_name,
            "content_hash": b.content_hash,
            "version": b.version,
            "backup_time": b.backup_time,
        } for b in snap.tracked_file_backups]
        self.archive.insert_file_history(snapshot_row, backups)

    # -- session metadata derivation ---------------------------------------

    def _upsert_subagent_session(self, records: dict, path: Path,
                                 parent_session_id: str, project_encoded: str,
                                 source_file: str, st) -> None:
        """Upsert the CHILD session row for one sidechain file.

        Keyed "<parent_session_id>:<agent_id>" — sidechain MESSAGES stay under
        the parent session_id exactly as before (source never re-shaped); the
        child row is the navigable identity. agentType/description come from the
        adjacent meta.json sidecar (tolerated absent on older sessions);
        aggregates are filled by recompute_session_aggregates.
        """
        from datetime import datetime, timezone

        if not path.stem.startswith("agent-"):
            return  # workflows journal.jsonl etc. — lifecycle records, no identity
        agent_id = path.stem[len("agent-"):]
        meta = read_agent_meta(path)

        users = records.get("user", [])
        assts = records.get("assistant", [])
        # The sidechain seed prompt (the Agent dispatch prompt) is the child's
        # first_prompt — the same shape as a main session's first real prompt.
        first_prompt = next((u.prompt_text for u in users
                             if u.is_direct_prompt and not u.is_meta and u.prompt_text),
                            None)
        ctx = next((m for m in users + assts), None)
        ts_list = [m.timestamp for m in users + assts if getattr(m, "timestamp", None)]

        self.archive.upsert_session({
            "session_id": f"{parent_session_id}:{agent_id}",
            "project_id": self._project_id_for(project_encoded),
            "file_path": source_file,
            "is_subagent": True,
            "parent_session_id": parent_session_id,
            "agent_id": agent_id,
            "agent_name": meta.get("agentType"),
            "custom_title": meta.get("description"),
            "first_prompt": first_prompt,
            "git_branch": ctx.git_branch if ctx else None,
            "cwd": ctx.cwd if ctx else None,
            "cc_version": ctx.version if ctx else None,
            "entrypoint": getattr(ctx, "entrypoint", None) if ctx else None,
            "created_at": min(ts_list) if ts_list else None,
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            "message_count": len(users) + len(assts),
        })

    def _upsert_session(self, records: dict, session_id: str, project_id: int,
                       source_file: str, st) -> None:
        from datetime import datetime, timezone

        users = records.get("user", [])
        assts = records.get("assistant", [])

        # First real user prompt
        first_prompt = None
        for u in users:
            if u.is_direct_prompt and not u.is_meta and u.prompt_text:
                first_prompt = u.prompt_text
                break

        # Context fields from any conversation record
        ctx = next((m for m in users + assts), None)

        # Session-scoped metadata (latest-wins)
        meta: dict[str, Optional[str]] = {}
        last_prompt_leaf = None
        for rec in records.get("session_meta", []):
            meta[rec.kind] = rec.value
            if rec.kind == "last-prompt":
                last_prompt_leaf = rec.raw.get("leafUuid")

        # Timestamps
        ts_list = [m.timestamp for m in users + assts if getattr(m, "timestamp", None)]
        created_at = min(ts_list) if ts_list else None
        modified_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

        self.archive.upsert_session({
            "session_id": session_id,
            "project_id": project_id,
            "file_path": source_file,
            "is_subagent": False,
            "parent_session_id": None,
            "agent_id": None,
            "ai_title": meta.get("ai-title"),
            "custom_title": meta.get("custom-title"),
            "first_prompt": first_prompt,
            "last_prompt": meta.get("last-prompt"),
            "last_prompt_leaf_uuid": last_prompt_leaf,
            "permission_mode": meta.get("permission-mode"),
            "mode": meta.get("mode"),
            "bridge_session_id": meta.get("bridge-session"),
            "agent_name": meta.get("agent-name"),
            "git_branch": ctx.git_branch if ctx else None,
            "cwd": ctx.cwd if ctx else None,
            "cc_version": ctx.version if ctx else None,
            "entrypoint": getattr(ctx, "entrypoint", None) if ctx else None,
            "created_at": created_at,
            "modified_at": modified_at,
            "message_count": len(users) + len(assts),
        })


# --- one-shot backfill -------------------------------------------------------

_BF_CHILDREN_SQL = """
    SELECT m.session_id AS parent, m.agent_id,
           max(m.source_file) FILTER (WHERE m.source_file LIKE '%%/subagents/%%')
               AS sub_file,
           max(m.source_file) AS any_file,
           min(m.ts) AS created_at, max(m.ts) AS modified_at,
           count(*) AS message_count
    FROM messages m
    WHERE m.is_sidechain AND m.agent_id IS NOT NULL
    GROUP BY 1, 2
"""

_BF_FIRST_PROMPT_SQL = """
    SELECT DISTINCT ON (session_id, agent_id)
           session_id AS parent, agent_id, prompt_text,
           cwd, git_branch, cc_version, entrypoint
    FROM messages
    WHERE is_sidechain AND agent_id IS NOT NULL
      AND message_type = 'prompt' AND NOT is_meta AND prompt_text IS NOT NULL
    ORDER BY session_id, agent_id, ts
"""

# Parent -> child linkage from the ledger: the Agent tool_use joined to its
# tool_result, whose tool_use_result JSONB carries {agentId, agentType, ...}.
_BF_AGENT_RESULTS_SQL = """
    SELECT cb.session_id AS parent,
           tr.tool_use_result->>'agentId' AS agent_id,
           coalesce(tr.tool_use_result->>'agentType',
                    cb.tool_input->>'subagent_type') AS agent_type,
           cb.tool_input->>'description' AS description
    FROM content_blocks cb
    JOIN tool_results tr ON tr.tool_use_id = cb.tool_use_id
    WHERE cb.block_type = 'tool_use' AND cb.tool_name = 'Agent'
      AND tr.tool_use_result ? 'agentId'
"""


def backfill_subagent_sessions(archive: SessionArchive, log=None) -> dict:
    """Materialize child session rows for ALL already-ingested sidechain files.

    One-shot, idempotent (`csd backfill-subagents`). No re-ingest: everything is
    derived from rows already in the archive — messages define the child set,
    the adjacent meta.json sidecar (where still on disk) supplies
    agentType/description, else the Agent tool_result join does. Ends with a
    recompute so aggregates land on the new rows.
    """
    emit = log if callable(log) else (lambda _m: None)

    emit("scanning archived sidechain messages…")
    children = archive.query(_BF_CHILDREN_SQL)
    emit(f"  {len(children)} distinct (parent, agent_id) pairs")

    seeds = {(r["parent"], r["agent_id"]): r
             for r in archive.query(_BF_FIRST_PROMPT_SQL)}
    agent_results = {(r["parent"], r["agent_id"]): r
                     for r in archive.query(_BF_AGENT_RESULTS_SQL)
                     if r["agent_id"]}
    parents = {r["session_id"]: r["project_id"] for r in archive.query(
        "SELECT session_id, project_id FROM sessions WHERE NOT is_subagent")}

    rows: list[dict] = []
    meta_hits = result_hits = 0
    for c in children:
        parent, agent_id = c["parent"], c["agent_id"]
        source_file = c["sub_file"] or c["any_file"]
        meta = read_agent_meta(Path(source_file)) if source_file else {}
        agent_name = meta.get("agentType")
        description = meta.get("description")
        if agent_name or description:
            meta_hits += 1
        else:
            hit = agent_results.get((parent, agent_id))
            if hit:
                agent_name = hit["agent_type"]
                description = hit["description"]
                result_hits += 1
        seed = seeds.get((parent, agent_id), {})
        rows.append({
            "session_id": f"{parent}:{agent_id}",
            "project_id": parents.get(parent),
            "file_path": source_file,
            "is_subagent": True,
            "parent_session_id": parent,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "custom_title": description,
            "first_prompt": seed.get("prompt_text"),
            "cwd": seed.get("cwd"),
            "git_branch": seed.get("git_branch"),
            "cc_version": seed.get("cc_version"),
            "entrypoint": seed.get("entrypoint"),
            "created_at": c["created_at"],
            "modified_at": c["modified_at"],
            "message_count": c["message_count"],
        })

    emit(f"upserting {len(rows)} child session rows "
         f"({meta_hits} via meta.json, {result_hits} via Agent-result join)…")
    archive.upsert_sessions(rows)
    emit("recomputing session aggregates…")
    archive.recompute_session_aggregates()
    return {"children": len(rows), "meta_hits": meta_hits,
            "result_hits": result_hits}
