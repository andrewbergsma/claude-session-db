"""Parsers for JSONL session transcript records.

JSONL files contain multiple record types:
- user: User messages (prompts) and tool results
- assistant: Assistant responses with content blocks
- progress: Progress events (MCP, bash, hooks, agents, search)
- system: System messages (turn_duration, compact_boundary, local_command,
          api_error, stop_hook_summary)
- summary: Session title/summary text
- queue-operation: Message queue operations (enqueue/remove)
- file-history-snapshot: File state snapshots
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from enum import Enum
import json


class UserType(Enum):
    """Type of user message source."""

    EXTERNAL = "external"  # Direct user input
    TOOL_RESULT = "tool_result"  # Response to tool call (inferred)


class PermissionMode(Enum):
    """Permission mode for the session."""

    DEFAULT = "default"
    BYPASS = "bypassPermissions"
    PLAN = "plan"


@dataclass
class ThinkingMetadata:
    """Metadata about thinking mode configuration."""

    level: str  # "high", "low", etc.
    disabled: bool
    triggers: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ThinkingMetadata":
        return cls(
            level=data.get("level", "high"),
            disabled=data.get("disabled", False),
            triggers=data.get("triggers", []),
        )


# =============================================================================
# Content Block Types (for assistant messages)
# =============================================================================


@dataclass
class ThinkingBlock:
    """A thinking content block from assistant message."""

    thinking: str
    signature: str = ""  # Cryptographic signature

    @classmethod
    def from_dict(cls, data: dict) -> "ThinkingBlock":
        return cls(
            thinking=data.get("thinking", ""),
            signature=data.get("signature", ""),
        )

    @property
    def char_count(self) -> int:
        return len(self.thinking)


@dataclass
class TextBlock:
    """A text content block from assistant message."""

    text: str

    @classmethod
    def from_dict(cls, data: dict) -> "TextBlock":
        return cls(text=data.get("text", ""))

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class ToolUseCaller:
    """Caller information for tool use."""

    type: str  # "direct", etc.

    @classmethod
    def from_dict(cls, data: dict) -> "ToolUseCaller":
        return cls(type=data.get("type", "direct"))


@dataclass
class ToolUseBlock:
    """A tool_use content block from assistant message."""

    id: str  # Tool use ID (e.g., "toolu_01...")
    name: str  # Tool name (e.g., "mcp__knowledge__get_entry")
    input: dict  # Tool input parameters
    caller: ToolUseCaller

    @classmethod
    def from_dict(cls, data: dict) -> "ToolUseBlock":
        caller_data = data.get("caller", {"type": "direct"})
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            input=data.get("input", {}),
            caller=ToolUseCaller.from_dict(caller_data),
        )

    @property
    def tool_type(self) -> str:
        """Extract tool type from name (mcp, builtin, etc)."""
        if self.name.startswith("mcp__"):
            return "mcp"
        return "builtin"

    @property
    def mcp_server(self) -> Optional[str]:
        """Extract MCP server name if MCP tool."""
        if self.name.startswith("mcp__"):
            parts = self.name.split("__")
            if len(parts) >= 2:
                return parts[1]
        return None


# Type alias for assistant content blocks
ContentBlock = ThinkingBlock | TextBlock | ToolUseBlock


def parse_content_block(data: dict) -> Optional[ContentBlock]:
    """Parse a content block based on its type."""
    block_type = data.get("type")
    if block_type == "thinking":
        return ThinkingBlock.from_dict(data)
    elif block_type == "text":
        return TextBlock.from_dict(data)
    elif block_type == "tool_use":
        return ToolUseBlock.from_dict(data)
    return None


# =============================================================================
# Tool Result Block Types (for user messages containing tool results)
# =============================================================================


@dataclass
class ToolResultContentBlock:
    """A content block within a tool_result (typically text).

    Tool results can contain multiple content blocks, usually text.
    This wraps the inner content blocks.
    """

    type: str  # Usually "text"
    text: str

    @classmethod
    def from_dict(cls, data: dict) -> "ToolResultContentBlock":
        return cls(
            type=data.get("type", "text"),
            text=data.get("text", ""),
        )

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
        }


@dataclass
class ToolResultBlock:
    """A tool_result content block from a user message.

    Tool results are returned in user messages as responses to tool_use blocks.
    The content can be either:
    - A string (simple text result)
    - An array of content blocks (typically text blocks)

    Fields:
        tool_use_id: The ID of the tool_use block this responds to
        content: Either a string or list of ToolResultContentBlock
        is_error: Whether the tool execution resulted in an error (optional)
    """

    tool_use_id: str
    content: str | list[ToolResultContentBlock]
    is_error: Optional[bool] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ToolResultBlock":
        raw_content = data.get("content", "")

        # Parse content - can be string or array of content blocks
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = [
                ToolResultContentBlock.from_dict(item)
                for item in raw_content
                if isinstance(item, dict)
            ]
        else:
            content = str(raw_content) if raw_content else ""

        return cls(
            tool_use_id=data.get("tool_use_id", ""),
            content=content,
            is_error=data.get("is_error"),  # Can be None, True, or False
        )

    @property
    def content_text(self) -> str:
        """Extract full text content regardless of format."""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(block.text for block in self.content)

    @property
    def content_blocks(self) -> list[ToolResultContentBlock]:
        """Get content as list of blocks (wraps string content if needed)."""
        if isinstance(self.content, str):
            return [ToolResultContentBlock(type="text", text=self.content)]
        return self.content

    @property
    def is_success(self) -> bool:
        """True if not an error result."""
        return self.is_error is not True

    @property
    def char_count(self) -> int:
        """Total character count of content."""
        return len(self.content_text)

    def to_dict(self) -> dict:
        """Convert to dict for database/export."""
        return {
            "tool_use_id": self.tool_use_id,
            "content_text": self.content_text,
            "char_count": self.char_count,
            "is_error": self.is_error,
            "is_success": self.is_success,
            "content_block_count": len(self.content_blocks),
        }


# =============================================================================
# Usage Statistics
# =============================================================================


@dataclass
class CacheCreation:
    """Cache creation details."""

    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "CacheCreation":
        return cls(
            ephemeral_5m_input_tokens=data.get("ephemeral_5m_input_tokens", 0),
            ephemeral_1h_input_tokens=data.get("ephemeral_1h_input_tokens", 0),
        )


@dataclass
class Usage:
    """Token usage statistics for an API response."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation: Optional[CacheCreation] = None
    service_tier: str = "standard"
    inference_geo: str = "not_available"
    speed: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Usage":
        cache_data = data.get("cache_creation")
        return cls(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_creation_input_tokens=data.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=data.get("cache_read_input_tokens", 0),
            cache_creation=CacheCreation.from_dict(cache_data) if cache_data else None,
            service_tier=data.get("service_tier", "standard"),
            inference_geo=data.get("inference_geo", "not_available"),
            speed=data.get("speed"),
            raw=data,
        )

    @property
    def ephemeral_5m_tokens(self) -> int:
        return self.cache_creation.ephemeral_5m_input_tokens if self.cache_creation else 0

    @property
    def ephemeral_1h_tokens(self) -> int:
        return self.cache_creation.ephemeral_1h_input_tokens if self.cache_creation else 0

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens including cache."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Percentage of input tokens from cache."""
        total = self.total_input_tokens
        if total == 0:
            return 0.0
        return self.cache_read_input_tokens / total


# =============================================================================
# Assistant Message
# =============================================================================


@dataclass
class AssistantMessage:
    """An assistant message record from JSONL.

    Contains the model response with content blocks (thinking, text, tool_use),
    usage statistics, and metadata.
    """

    # Core identifiers
    uuid: str
    session_id: str
    parent_uuid: Optional[str]
    timestamp: datetime

    # API response metadata
    message_id: str  # API message ID
    model: str  # Model name (e.g., "claude-sonnet-4-20250514")
    stop_reason: Optional[str]  # "end_turn", "tool_use", etc.
    stop_sequence: Optional[str]

    # Content blocks
    content_blocks: list[ContentBlock]

    # Usage statistics
    usage: Usage

    # Context
    cwd: str
    git_branch: str
    version: str

    # Flags
    is_sidechain: bool
    user_type: str

    # Optional fields
    slug: Optional[str] = None
    request_id: Optional[str] = None
    is_api_error_message: bool = False
    error: Optional[str] = None

    # Context / threading (added 2026-06 re-audit)
    entrypoint: Optional[str] = None
    agent_id: Optional[str] = None

    # Attribution system (NEW — which agent/skill/mcp/plugin produced this message)
    attribution_agent: Optional[str] = None
    attribution_skill: Optional[str] = None
    attribution_mcp_server: Optional[str] = None
    attribution_mcp_tool: Optional[str] = None
    attribution_plugin: Optional[str] = None

    # Variable-shape / escape-hatch payloads
    stop_details: Optional[Any] = None
    diagnostics: Optional[Any] = None
    forked_from: Optional[dict] = None
    api_error_status: Optional[int] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def thinking_blocks(self) -> list[ThinkingBlock]:
        """Get all thinking blocks."""
        return [b for b in self.content_blocks if isinstance(b, ThinkingBlock)]

    @property
    def text_blocks(self) -> list[TextBlock]:
        """Get all text blocks."""
        return [b for b in self.content_blocks if isinstance(b, TextBlock)]

    @property
    def tool_use_blocks(self) -> list[ToolUseBlock]:
        """Get all tool use blocks."""
        return [b for b in self.content_blocks if isinstance(b, ToolUseBlock)]

    @property
    def full_text(self) -> str:
        """Concatenate all text blocks."""
        return "\n".join(b.text for b in self.text_blocks)

    @property
    def full_thinking(self) -> str:
        """Concatenate all thinking blocks."""
        return "\n".join(b.thinking for b in self.thinking_blocks)

    @property
    def tool_names(self) -> list[str]:
        """Get list of tool names used."""
        return [b.name for b in self.tool_use_blocks]

    @property
    def has_thinking(self) -> bool:
        return len(self.thinking_blocks) > 0

    @property
    def has_tool_use(self) -> bool:
        return len(self.tool_use_blocks) > 0

    @classmethod
    def from_dict(cls, data: dict) -> "AssistantMessage":
        """Parse from JSON dict."""
        message = data.get("message", {})

        # Parse content blocks
        content_blocks = []
        for block_data in message.get("content", []):
            block = parse_content_block(block_data)
            if block:
                content_blocks.append(block)

        # Parse usage
        usage_data = message.get("usage", {})
        usage = Usage.from_dict(usage_data)

        return cls(
            uuid=data["uuid"],
            session_id=data["sessionId"],
            parent_uuid=data.get("parentUuid"),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            message_id=message.get("id", ""),
            model=message.get("model", ""),
            stop_reason=message.get("stop_reason"),
            stop_sequence=message.get("stop_sequence"),
            content_blocks=content_blocks,
            usage=usage,
            cwd=data.get("cwd", ""),
            git_branch=data.get("gitBranch", ""),
            version=data.get("version", ""),
            is_sidechain=data.get("isSidechain", False),
            user_type=data.get("userType", "external"),
            slug=data.get("slug"),
            request_id=data.get("requestId"),
            is_api_error_message=data.get("isApiErrorMessage", False),
            error=data.get("error"),
            entrypoint=data.get("entrypoint"),
            agent_id=data.get("agentId"),
            attribution_agent=data.get("attributionAgent"),
            attribution_skill=data.get("attributionSkill"),
            attribution_mcp_server=data.get("attributionMcpServer"),
            attribution_mcp_tool=data.get("attributionMcpTool"),
            attribution_plugin=data.get("attributionPlugin"),
            stop_details=message.get("stop_details"),
            diagnostics=message.get("diagnostics"),
            forked_from=data.get("forkedFrom"),
            api_error_status=data.get("apiErrorStatus"),
            raw=data,
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/export."""
        return {
            "uuid": self.uuid,
            "session_id": self.session_id,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp.isoformat(),
            "message_id": self.message_id,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "thinking_count": len(self.thinking_blocks),
            "text_count": len(self.text_blocks),
            "tool_use_count": len(self.tool_use_blocks),
            "tool_names": self.tool_names,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_tokens": self.usage.cache_creation_input_tokens,
            "total_input_tokens": self.usage.total_input_tokens,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "is_sidechain": self.is_sidechain,
            "slug": self.slug,
            "is_api_error": self.is_api_error_message,
            "error": self.error,
        }


@dataclass
class UserMessage:
    """A user message record from JSONL.

    User messages come in two forms:
    1. Direct prompts: message.content is a string
    2. Tool results: message.content is an array with tool_result blocks
    """

    # Core identifiers
    uuid: str
    session_id: str
    parent_uuid: Optional[str]
    timestamp: datetime

    # Message content
    role: str  # Always "user"
    content: str | list[dict]  # String for prompts, array for tool results

    # Context
    cwd: str
    git_branch: str
    version: str

    # Flags
    is_sidechain: bool
    user_type: str  # "external"

    # Optional fields
    slug: Optional[str] = None
    permission_mode: Optional[str] = None
    thinking_metadata: Optional[ThinkingMetadata] = None
    todos: list[dict] = field(default_factory=list)

    # Tool result specific
    source_tool_assistant_uuid: Optional[str] = None
    tool_use_result: Optional[Any] = None

    # Special flags
    is_meta: bool = False
    is_compact_summary: bool = False
    is_visible_in_transcript_only: bool = False
    source_tool_use_id: Optional[str] = None

    # Context / threading (added 2026-06 re-audit)
    entrypoint: Optional[str] = None
    prompt_id: Optional[str] = None
    agent_id: Optional[str] = None
    origin: Optional[Any] = None
    forked_from: Optional[dict] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_direct_prompt(self) -> bool:
        """True if this is a direct user prompt (not a tool result)."""
        return isinstance(self.content, str)

    @property
    def is_tool_result(self) -> bool:
        """True if this contains tool results."""
        if not isinstance(self.content, list):
            return False
        return any(
            isinstance(item, dict) and item.get("type") == "tool_result" for item in self.content
        )

    @property
    def prompt_text(self) -> Optional[str]:
        """Extract the prompt text if this is a direct prompt."""
        if isinstance(self.content, str):
            return self.content
        # Check for text blocks in array content
        for item in self.content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text")
        return None

    @property
    def tool_result_ids(self) -> list[str]:
        """Extract tool_use_ids from tool_result blocks."""
        if not isinstance(self.content, list):
            return []
        return [
            item.get("tool_use_id")
            for item in self.content
            if isinstance(item, dict)
            and item.get("type") == "tool_result"
            and item.get("tool_use_id")
        ]

    @property
    def tool_result_blocks(self) -> list["ToolResultBlock"]:
        """Parse and return tool_result blocks as ToolResultBlock objects."""
        if not isinstance(self.content, list):
            return []
        return [
            ToolResultBlock.from_dict(item)
            for item in self.content
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ]

    @property
    def has_error_results(self) -> bool:
        """True if any tool result is an error."""
        return any(block.is_error for block in self.tool_result_blocks)

    @property
    def error_results(self) -> list["ToolResultBlock"]:
        """Get only error results."""
        return [block for block in self.tool_result_blocks if block.is_error]

    @property
    def success_results(self) -> list["ToolResultBlock"]:
        """Get only successful results."""
        return [block for block in self.tool_result_blocks if block.is_success]

    @property
    def tool_result_count(self) -> int:
        """Number of tool results in this message."""
        return len(self.tool_result_blocks)

    @property
    def total_result_chars(self) -> int:
        """Total character count of all tool results."""
        return sum(block.char_count for block in self.tool_result_blocks)

    @classmethod
    def from_dict(cls, data: dict) -> "UserMessage":
        """Parse from JSON dict."""
        message = data.get("message", {})

        # Parse thinking metadata if present
        thinking_meta = None
        if "thinkingMetadata" in data:
            thinking_meta = ThinkingMetadata.from_dict(data["thinkingMetadata"])

        return cls(
            uuid=data["uuid"],
            session_id=data["sessionId"],
            parent_uuid=data.get("parentUuid"),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            role=message.get("role", "user"),
            content=message.get("content", ""),
            cwd=data.get("cwd", ""),
            git_branch=data.get("gitBranch", ""),
            version=data.get("version", ""),
            is_sidechain=data.get("isSidechain", False),
            user_type=data.get("userType", "external"),
            slug=data.get("slug"),
            permission_mode=data.get("permissionMode"),
            thinking_metadata=thinking_meta,
            todos=data.get("todos", []),
            source_tool_assistant_uuid=data.get("sourceToolAssistantUUID"),
            tool_use_result=data.get("toolUseResult"),
            is_meta=data.get("isMeta", False),
            is_compact_summary=data.get("isCompactSummary", False),
            is_visible_in_transcript_only=data.get("isVisibleInTranscriptOnly", False),
            source_tool_use_id=data.get("sourceToolUseID"),
            entrypoint=data.get("entrypoint"),
            prompt_id=data.get("promptId"),
            agent_id=data.get("agentId"),
            origin=data.get("origin"),
            forked_from=data.get("forkedFrom"),
            raw=data,
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/export."""
        result = {
            "uuid": self.uuid,
            "session_id": self.session_id,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp.isoformat(),
            "role": self.role,
            "content_type": "prompt" if self.is_direct_prompt else "tool_result",
            "prompt_text": self.prompt_text,
            "tool_result_ids": self.tool_result_ids,
            "tool_result_count": self.tool_result_count,
            "total_result_chars": self.total_result_chars,
            "has_error_results": self.has_error_results,
            "error_count": len(self.error_results),
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "is_sidechain": self.is_sidechain,
            "user_type": self.user_type,
            "slug": self.slug,
            "permission_mode": self.permission_mode,
            "thinking_level": (self.thinking_metadata.level if self.thinking_metadata else None),
            "thinking_disabled": (
                self.thinking_metadata.disabled if self.thinking_metadata else None
            ),
            "is_meta": self.is_meta,
            "is_compact_summary": self.is_compact_summary,
        }
        return result

    def tool_results_to_dicts(self) -> list[dict]:
        """Export all tool results as list of dicts for detailed analysis."""
        return [block.to_dict() for block in self.tool_result_blocks]


# =============================================================================
# File History Snapshot
# =============================================================================


@dataclass
class FileBackup:
    """Backup information for a single tracked file."""

    file_path: str  # Path to the tracked file (key in trackedFileBackups)
    backup_file_name: Optional[str]  # Backup file name (e.g., "aac4901a4109ef10@v1")
    version: int  # Backup version number
    backup_time: datetime  # When the backup was created

    @classmethod
    def from_dict(cls, file_path: str, data: dict) -> "FileBackup":
        backup_time_str = data.get("backupTime", "")
        backup_time = (
            datetime.fromisoformat(backup_time_str.replace("Z", "+00:00"))
            if backup_time_str
            else datetime.min
        )

        return cls(
            file_path=file_path,
            backup_file_name=data.get("backupFileName"),
            version=data.get("version", 1),
            backup_time=backup_time,
        )

    def to_dict(self) -> dict:
        """Convert to dict for database/export."""
        return {
            "file_path": self.file_path,
            "backup_file_name": self.backup_file_name,
            "version": self.version,
            "backup_time": self.backup_time.isoformat(),
        }

    @property
    def content_hash(self) -> Optional[str]:
        """Extract content hash from backup file name.

        Example: 'aac4901a4109ef10' from 'aac4901a4109ef10@v1'.
        """
        if not self.backup_file_name:
            return None
        if "@" in self.backup_file_name:
            return self.backup_file_name.split("@")[0]
        return self.backup_file_name


@dataclass
class FileHistorySnapshot:
    """A file-history-snapshot record from JSONL.

    Captures the state of tracked files at a specific point in time.
    Used for file versioning and rollback capabilities.
    """

    # Core identifiers
    message_id: str  # Message this snapshot is associated with
    snapshot_message_id: str  # Message ID within the snapshot
    timestamp: datetime  # When the snapshot was taken

    # Tracked files
    tracked_file_backups: list[FileBackup]

    # Flags
    is_snapshot_update: bool  # True if this is an incremental update

    @property
    def file_paths(self) -> list[str]:
        """Get list of tracked file paths."""
        return [fb.file_path for fb in self.tracked_file_backups]

    @property
    def file_count(self) -> int:
        """Number of tracked files."""
        return len(self.tracked_file_backups)

    @property
    def has_backups(self) -> bool:
        """True if any files have actual backups (non-null backup_file_name)."""
        return any(fb.backup_file_name for fb in self.tracked_file_backups)

    def get_backup(self, file_path: str) -> Optional[FileBackup]:
        """Get backup info for a specific file path."""
        for fb in self.tracked_file_backups:
            if fb.file_path == file_path:
                return fb
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "FileHistorySnapshot":
        """Parse from JSON dict."""
        snapshot = data.get("snapshot", {})
        tracked_backups_data = snapshot.get("trackedFileBackups", {})

        # Parse each tracked file backup
        tracked_file_backups = [
            FileBackup.from_dict(file_path, backup_data)
            for file_path, backup_data in tracked_backups_data.items()
        ]

        # Sort by file path for consistent ordering
        tracked_file_backups.sort(key=lambda fb: fb.file_path)

        timestamp_str = snapshot.get("timestamp", "")
        timestamp = (
            datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if timestamp_str
            else datetime.min
        )

        return cls(
            message_id=data.get("messageId", ""),
            snapshot_message_id=snapshot.get("messageId", ""),
            timestamp=timestamp,
            tracked_file_backups=tracked_file_backups,
            is_snapshot_update=data.get("isSnapshotUpdate", False),
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/export."""
        return {
            "message_id": self.message_id,
            "snapshot_message_id": self.snapshot_message_id,
            "timestamp": self.timestamp.isoformat(),
            "file_count": self.file_count,
            "file_paths": self.file_paths,
            "has_backups": self.has_backups,
            "is_snapshot_update": self.is_snapshot_update,
            "tracked_file_backups": [fb.to_dict() for fb in self.tracked_file_backups],
        }


# =============================================================================
# System Message Types
# =============================================================================


class SystemSubtype(Enum):
    """System message subtypes."""

    TURN_DURATION = "turn_duration"
    COMPACT_BOUNDARY = "compact_boundary"
    MICROCOMPACT_BOUNDARY = "microcompact_boundary"
    LOCAL_COMMAND = "local_command"
    API_ERROR = "api_error"
    STOP_HOOK_SUMMARY = "stop_hook_summary"
    # Added 2026-06 re-audit
    AWAY_SUMMARY = "away_summary"
    BRIDGE_STATUS = "bridge_status"
    SCHEDULED_TASK_FIRE = "scheduled_task_fire"
    INFORMATIONAL = "informational"


@dataclass
class CompactMetadata:
    """Metadata for compact_boundary system messages."""

    trigger: str  # "manual" or "auto"
    pre_tokens: int  # Token count before compaction

    @classmethod
    def from_dict(cls, data: dict) -> "CompactMetadata":
        return cls(
            trigger=data.get("trigger", "unknown"),
            pre_tokens=data.get("preTokens", 0),
        )

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "pre_tokens": self.pre_tokens,
        }


@dataclass
class HookInfo:
    """Information about a hook that ran."""

    command: str

    @classmethod
    def from_dict(cls, data: dict) -> "HookInfo":
        return cls(command=data.get("command", ""))

    def to_dict(self) -> dict:
        return {"command": self.command}


@dataclass
class ApiErrorInfo:
    """Information about an API error."""

    status: int
    request_id: str
    error_type: str
    error_message: str

    @classmethod
    def from_dict(cls, data: dict) -> "ApiErrorInfo":
        error_obj = data.get("error", {})
        inner_error = error_obj.get("error", {})
        return cls(
            status=data.get("status", 0),
            request_id=data.get("requestID", ""),
            error_type=inner_error.get("type", ""),
            error_message=inner_error.get("message", ""),
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "request_id": self.request_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass
class SystemMessage:
    """A system message record from JSONL.

    System messages track internal events like:
    - Turn duration timing
    - Conversation compaction
    - Local slash commands
    - API errors and retries
    - Stop hook execution summaries
    """

    # Core identifiers
    uuid: str
    session_id: str
    parent_uuid: Optional[str]
    timestamp: datetime

    # Message type
    subtype: str  # One of SystemSubtype values

    # Context
    cwd: str
    git_branch: str
    version: str

    # Flags
    is_sidechain: bool
    is_meta: bool
    user_type: str

    # Optional fields
    slug: Optional[str] = None
    level: Optional[str] = None  # "info", "error", "suggestion", etc.
    content: Optional[str] = None  # For compact_boundary, local_command

    # Subtype-specific fields
    duration_ms: Optional[int] = None  # turn_duration
    message_count: Optional[int] = None  # turn_duration (NEW)
    url: Optional[str] = None  # bridge_status (NEW)
    compact_metadata: Optional[CompactMetadata] = None  # compact_boundary
    microcompact_metadata: Optional[dict] = None  # microcompact_boundary (raw)
    logical_parent_uuid: Optional[str] = None  # compact_boundary
    entrypoint: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    # API error fields
    error_info: Optional[ApiErrorInfo] = None
    retry_in_ms: Optional[float] = None
    retry_attempt: Optional[int] = None
    max_retries: Optional[int] = None

    # Stop hook fields
    hook_count: Optional[int] = None
    hook_infos: list[HookInfo] = field(default_factory=list)
    hook_errors: list[str] = field(default_factory=list)
    prevented_continuation: Optional[bool] = None
    stop_reason: Optional[str] = None
    has_output: Optional[bool] = None
    tool_use_id: Optional[str] = None

    @property
    def subtype_enum(self) -> Optional[SystemSubtype]:
        """Get subtype as enum if valid."""
        try:
            return SystemSubtype(self.subtype)
        except ValueError:
            return None

    @property
    def is_turn_duration(self) -> bool:
        return self.subtype == SystemSubtype.TURN_DURATION.value

    @property
    def is_compact_boundary(self) -> bool:
        return self.subtype == SystemSubtype.COMPACT_BOUNDARY.value

    @property
    def is_local_command(self) -> bool:
        return self.subtype == SystemSubtype.LOCAL_COMMAND.value

    @property
    def is_api_error(self) -> bool:
        return self.subtype == SystemSubtype.API_ERROR.value

    @property
    def is_stop_hook_summary(self) -> bool:
        return self.subtype == SystemSubtype.STOP_HOOK_SUMMARY.value

    @property
    def duration_seconds(self) -> Optional[float]:
        """Turn duration in seconds."""
        if self.duration_ms is not None:
            return self.duration_ms / 1000.0
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "SystemMessage":
        """Parse from JSON dict."""
        # Parse compact metadata if present
        compact_meta = None
        if "compactMetadata" in data:
            compact_meta = CompactMetadata.from_dict(data["compactMetadata"])

        # Parse API error info if present
        error_info = None
        if "error" in data and isinstance(data["error"], dict):
            error_info = ApiErrorInfo.from_dict(data["error"])

        # Parse hook infos if present
        hook_infos = []
        for info in data.get("hookInfos", []):
            hook_infos.append(HookInfo.from_dict(info))

        return cls(
            uuid=data["uuid"],
            session_id=data["sessionId"],
            parent_uuid=data.get("parentUuid"),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            subtype=data.get("subtype", "unknown"),
            cwd=data.get("cwd", ""),
            git_branch=data.get("gitBranch", ""),
            version=data.get("version", ""),
            is_sidechain=data.get("isSidechain", False),
            is_meta=data.get("isMeta", False),
            user_type=data.get("userType", "external"),
            slug=data.get("slug"),
            level=data.get("level"),
            content=data.get("content"),
            duration_ms=data.get("durationMs"),
            message_count=data.get("messageCount"),
            url=data.get("url"),
            entrypoint=data.get("entrypoint"),
            raw=data,
            compact_metadata=compact_meta,
            microcompact_metadata=data.get("microcompactMetadata"),
            logical_parent_uuid=data.get("logicalParentUuid"),
            error_info=error_info,
            retry_in_ms=data.get("retryInMs"),
            retry_attempt=data.get("retryAttempt"),
            max_retries=data.get("maxRetries"),
            hook_count=data.get("hookCount"),
            hook_infos=hook_infos,
            hook_errors=data.get("hookErrors", []),
            prevented_continuation=data.get("preventedContinuation"),
            stop_reason=data.get("stopReason"),
            has_output=data.get("hasOutput"),
            tool_use_id=data.get("toolUseID"),
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/export."""
        result = {
            "uuid": self.uuid,
            "session_id": self.session_id,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp.isoformat(),
            "subtype": self.subtype,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "is_sidechain": self.is_sidechain,
            "is_meta": self.is_meta,
            "slug": self.slug,
            "level": self.level,
            "content": self.content,
        }

        # Add subtype-specific fields
        if self.is_turn_duration:
            result["duration_ms"] = self.duration_ms
            result["duration_seconds"] = self.duration_seconds

        if self.is_compact_boundary and self.compact_metadata:
            result["compact_trigger"] = self.compact_metadata.trigger
            result["compact_pre_tokens"] = self.compact_metadata.pre_tokens
            result["logical_parent_uuid"] = self.logical_parent_uuid

        if self.is_api_error and self.error_info:
            result["error_status"] = self.error_info.status
            result["error_request_id"] = self.error_info.request_id
            result["error_type"] = self.error_info.error_type
            result["error_message"] = self.error_info.error_message
            result["retry_in_ms"] = self.retry_in_ms
            result["retry_attempt"] = self.retry_attempt
            result["max_retries"] = self.max_retries

        if self.is_stop_hook_summary:
            result["hook_count"] = self.hook_count
            result["hook_commands"] = [h.command for h in self.hook_infos]
            result["hook_errors"] = self.hook_errors
            result["prevented_continuation"] = self.prevented_continuation
            result["stop_reason"] = self.stop_reason
            result["has_output"] = self.has_output
            result["tool_use_id"] = self.tool_use_id

        return result


# =============================================================================
# Summary Message
# =============================================================================


@dataclass
class SummaryMessage:
    """A summary record from JSONL.

    Summary messages store the session title/summary that appears in the
    Claude Code UI. They link to the leaf message in the conversation tree.
    """

    summary: str  # The session title/summary text
    leaf_uuid: str  # UUID of the leaf message this summary refers to

    @classmethod
    def from_dict(cls, data: dict) -> "SummaryMessage":
        """Parse from JSON dict."""
        return cls(
            summary=data.get("summary", ""),
            leaf_uuid=data.get("leafUuid", ""),
        )

    def to_dict(self) -> dict:
        """Convert to dict for database/export."""
        return {
            "summary": self.summary,
            "leaf_uuid": self.leaf_uuid,
        }


# =============================================================================
# Queue Operation Message
# =============================================================================


@dataclass
class QueueOperationMessage:
    """A queue operation record from JSONL.

    Tracks message queue operations (enqueue/remove) for the session.
    Used for managing pending user messages.
    """

    operation: str  # "enqueue" or "remove"
    timestamp: datetime
    session_id: str
    content: Optional[str] = None  # Message content (for enqueue)

    @classmethod
    def from_dict(cls, data: dict) -> "QueueOperationMessage":
        """Parse from JSON dict."""
        return cls(
            operation=data.get("operation", ""),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            session_id=data.get("sessionId", ""),
            content=data.get("content"),
        )

    def to_dict(self) -> dict:
        """Convert to dict for database/export."""
        return {
            "operation": self.operation,
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "content": self.content,
        }


# =============================================================================
# Progress Events
# =============================================================================


class ProgressType(Enum):
    """Type of progress event."""

    MCP_PROGRESS = "mcp_progress"
    BASH_PROGRESS = "bash_progress"
    HOOK_PROGRESS = "hook_progress"
    AGENT_PROGRESS = "agent_progress"
    QUERY_UPDATE = "query_update"
    SEARCH_RESULTS_RECEIVED = "search_results_received"
    WAITING_FOR_TASK = "waiting_for_task"
    UNKNOWN = "unknown"


@dataclass
class MCPProgressData:
    """Data for MCP tool execution progress."""

    status: str  # "started", "completed"
    server_name: str
    tool_name: str
    elapsed_time_ms: Optional[int] = None  # Only present on completion

    @classmethod
    def from_dict(cls, data: dict) -> "MCPProgressData":
        return cls(
            status=data.get("status", ""),
            server_name=data.get("serverName", ""),
            tool_name=data.get("toolName", ""),
            elapsed_time_ms=data.get("elapsedTimeMs"),
        )

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
        }
        if self.elapsed_time_ms is not None:
            result["elapsed_time_ms"] = self.elapsed_time_ms
        return result


@dataclass
class BashProgressData:
    """Data for bash command execution progress."""

    output: str  # Recent output
    full_output: str  # All output so far
    elapsed_time_seconds: int
    total_lines: int

    @classmethod
    def from_dict(cls, data: dict) -> "BashProgressData":
        return cls(
            output=data.get("output", ""),
            full_output=data.get("fullOutput", ""),
            elapsed_time_seconds=data.get("elapsedTimeSeconds", 0),
            total_lines=data.get("totalLines", 0),
        )

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "full_output": self.full_output,
            "elapsed_time_seconds": self.elapsed_time_seconds,
            "total_lines": self.total_lines,
        }


@dataclass
class HookProgressData:
    """Data for hook execution progress."""

    hook_event: str  # "SessionStart", "PreToolUse", "PostToolUse"
    hook_name: str  # e.g., "SessionStart:startup", "PreToolUse:Task"
    command: str  # The hook command being executed

    @classmethod
    def from_dict(cls, data: dict) -> "HookProgressData":
        return cls(
            hook_event=data.get("hookEvent", ""),
            hook_name=data.get("hookName", ""),
            command=data.get("command", ""),
        )

    def to_dict(self) -> dict:
        return {
            "hook_event": self.hook_event,
            "hook_name": self.hook_name,
            "command": self.command,
        }


@dataclass
class AgentProgressData:
    """Data for agent/Task tool progress."""

    prompt: str  # The agent prompt
    agent_id: str  # Agent identifier
    message: Optional[dict] = None  # Original message structure
    normalized_messages: Optional[list[dict]] = None  # Normalized messages

    @classmethod
    def from_dict(cls, data: dict) -> "AgentProgressData":
        return cls(
            prompt=data.get("prompt", ""),
            agent_id=data.get("agentId", ""),
            message=data.get("message"),
            normalized_messages=data.get("normalizedMessages"),
        )

    def to_dict(self) -> dict:
        result = {
            "prompt": self.prompt,
            "agent_id": self.agent_id,
        }
        if self.message:
            result["has_message"] = True
        if self.normalized_messages:
            result["normalized_message_count"] = len(self.normalized_messages)
        return result


@dataclass
class QueryUpdateData:
    """Data for web search query update."""

    query: str

    @classmethod
    def from_dict(cls, data: dict) -> "QueryUpdateData":
        return cls(query=data.get("query", ""))

    def to_dict(self) -> dict:
        return {"query": self.query}


@dataclass
class SearchResultsData:
    """Data for search results received."""

    result_count: int
    query: str

    @classmethod
    def from_dict(cls, data: dict) -> "SearchResultsData":
        return cls(
            result_count=data.get("resultCount", 0),
            query=data.get("query", ""),
        )

    def to_dict(self) -> dict:
        return {
            "result_count": self.result_count,
            "query": self.query,
        }


@dataclass
class WaitingForTaskData:
    """Data for waiting for background task."""

    task_description: str
    task_type: str  # e.g., "local_bash"

    @classmethod
    def from_dict(cls, data: dict) -> "WaitingForTaskData":
        return cls(
            task_description=data.get("taskDescription", ""),
            task_type=data.get("taskType", ""),
        )

    def to_dict(self) -> dict:
        return {
            "task_description": self.task_description,
            "task_type": self.task_type,
        }


# Type alias for progress data
ProgressData = (
    MCPProgressData
    | BashProgressData
    | HookProgressData
    | AgentProgressData
    | QueryUpdateData
    | SearchResultsData
    | WaitingForTaskData
    | dict
)


def parse_progress_data(data: dict) -> tuple[ProgressType, ProgressData]:
    """Parse progress data based on its type."""
    data_type = data.get("type", "unknown")

    if data_type == "mcp_progress":
        return ProgressType.MCP_PROGRESS, MCPProgressData.from_dict(data)
    elif data_type == "bash_progress":
        return ProgressType.BASH_PROGRESS, BashProgressData.from_dict(data)
    elif data_type == "hook_progress":
        return ProgressType.HOOK_PROGRESS, HookProgressData.from_dict(data)
    elif data_type == "agent_progress":
        return ProgressType.AGENT_PROGRESS, AgentProgressData.from_dict(data)
    elif data_type == "query_update":
        return ProgressType.QUERY_UPDATE, QueryUpdateData.from_dict(data)
    elif data_type == "search_results_received":
        return ProgressType.SEARCH_RESULTS_RECEIVED, SearchResultsData.from_dict(data)
    elif data_type == "waiting_for_task":
        return ProgressType.WAITING_FOR_TASK, WaitingForTaskData.from_dict(data)
    else:
        return ProgressType.UNKNOWN, data


@dataclass
class ProgressEvent:
    """A progress event record from JSONL.

    Progress events track the execution state of tools, hooks, agents, and searches.
    They provide real-time feedback during long-running operations.
    """

    # Core identifiers
    uuid: str
    session_id: str
    parent_uuid: Optional[str]
    timestamp: datetime

    # Progress data
    progress_type: ProgressType
    data: ProgressData

    # Tool context
    tool_use_id: str
    parent_tool_use_id: str

    # Session context
    cwd: str
    git_branch: str
    version: str

    # Flags
    is_sidechain: bool
    user_type: str

    # Optional fields
    slug: Optional[str] = None

    @property
    def is_mcp_progress(self) -> bool:
        return self.progress_type == ProgressType.MCP_PROGRESS

    @property
    def is_bash_progress(self) -> bool:
        return self.progress_type == ProgressType.BASH_PROGRESS

    @property
    def is_hook_progress(self) -> bool:
        return self.progress_type == ProgressType.HOOK_PROGRESS

    @property
    def is_agent_progress(self) -> bool:
        return self.progress_type == ProgressType.AGENT_PROGRESS

    @property
    def is_search_progress(self) -> bool:
        return self.progress_type in (
            ProgressType.QUERY_UPDATE,
            ProgressType.SEARCH_RESULTS_RECEIVED,
        )

    @property
    def mcp_server(self) -> Optional[str]:
        """Get MCP server name if this is an MCP progress event."""
        if isinstance(self.data, MCPProgressData):
            return self.data.server_name
        return None

    @property
    def mcp_tool(self) -> Optional[str]:
        """Get MCP tool name if this is an MCP progress event."""
        if isinstance(self.data, MCPProgressData):
            return self.data.tool_name
        return None

    @property
    def mcp_status(self) -> Optional[str]:
        """Get MCP status if this is an MCP progress event."""
        if isinstance(self.data, MCPProgressData):
            return self.data.status
        return None

    @property
    def hook_event(self) -> Optional[str]:
        """Get hook event type if this is a hook progress event."""
        if isinstance(self.data, HookProgressData):
            return self.data.hook_event
        return None

    @property
    def bash_elapsed_seconds(self) -> Optional[int]:
        """Get bash elapsed time if this is a bash progress event."""
        if isinstance(self.data, BashProgressData):
            return self.data.elapsed_time_seconds
        return None

    @classmethod
    def from_dict(cls, data: dict) -> "ProgressEvent":
        """Parse from JSON dict."""
        # Parse the progress-specific data
        progress_data = data.get("data", {})
        progress_type, parsed_data = parse_progress_data(progress_data)

        return cls(
            uuid=data["uuid"],
            session_id=data["sessionId"],
            parent_uuid=data.get("parentUuid"),
            timestamp=datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")),
            progress_type=progress_type,
            data=parsed_data,
            tool_use_id=data.get("toolUseID", ""),
            parent_tool_use_id=data.get("parentToolUseID", ""),
            cwd=data.get("cwd", ""),
            git_branch=data.get("gitBranch", ""),
            version=data.get("version", ""),
            is_sidechain=data.get("isSidechain", False),
            user_type=data.get("userType", "external"),
            slug=data.get("slug"),
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/export."""
        # Get data as dict
        if hasattr(self.data, "to_dict"):
            data_dict = self.data.to_dict()
        else:
            data_dict = self.data if isinstance(self.data, dict) else {}

        return {
            "uuid": self.uuid,
            "session_id": self.session_id,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp.isoformat(),
            "progress_type": self.progress_type.value,
            "tool_use_id": self.tool_use_id,
            "parent_tool_use_id": self.parent_tool_use_id,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "version": self.version,
            "is_sidechain": self.is_sidechain,
            "user_type": self.user_type,
            "slug": self.slug,
            # Include flattened data fields
            **data_dict,
        }


# =============================================================================
# Attachment records (NEW 2026-06) — injected context attachments
# =============================================================================


@dataclass
class AttachmentRecord:
    """An `attachment` record: a context attachment injected into the thread.

    Conversation-flow record (has uuid/parentUuid/timestamp). The `attachment`
    payload is variable-shape (e.g. `deferred_tools_delta`) — stored as JSONB.
    """

    uuid: str
    session_id: str
    parent_uuid: Optional[str]
    timestamp: Optional[datetime]
    attachment_type: Optional[str]
    attachment: Any
    cwd: str = ""
    git_branch: str = ""
    version: str = ""
    is_sidechain: bool = False
    entrypoint: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AttachmentRecord":
        ts = data.get("timestamp")
        attachment = data.get("attachment")
        att_type = attachment.get("type") if isinstance(attachment, dict) else None
        return cls(
            uuid=data.get("uuid", ""),
            session_id=data.get("sessionId", ""),
            parent_uuid=data.get("parentUuid"),
            timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None,
            attachment_type=att_type,
            attachment=attachment,
            cwd=data.get("cwd", ""),
            git_branch=data.get("gitBranch", ""),
            version=data.get("version", ""),
            is_sidechain=data.get("isSidechain", False),
            entrypoint=data.get("entrypoint"),
            raw=data,
        )


# =============================================================================
# Session-scoped metadata records (NEW 2026-06) — sessionId-keyed, latest-wins
# =============================================================================


@dataclass
class SessionMetaRecord:
    """A lightweight session-scoped metadata record.

    Covers ai-title, custom-title, last-prompt, permission-mode, mode,
    bridge-session, agent-name. These are not part of the conversation thread;
    they set attributes on the session (latest-wins). `kind` is the record type,
    `value` is the primary scalar, and `raw` keeps the full payload.
    """

    kind: str  # record `type`
    session_id: str
    value: Optional[str]  # primary scalar (title text, mode, etc.)
    raw: dict = field(default_factory=dict, repr=False)

    # Field that holds the primary scalar per record type
    _VALUE_FIELDS = {
        "ai-title": "aiTitle",
        "custom-title": "customTitle",
        "last-prompt": "lastPrompt",
        "permission-mode": "permissionMode",
        "mode": "mode",
        "bridge-session": "bridgeSessionId",
        "agent-name": "agentName",
    }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMetaRecord":
        kind = data.get("type", "")
        value_field = cls._VALUE_FIELDS.get(kind)
        value = data.get(value_field) if value_field else None
        return cls(
            kind=kind,
            session_id=data.get("sessionId", ""),
            value=value,
            raw=data,
        )


@dataclass
class PrLinkRecord:
    """A `pr-link` record: a PR opened during the session."""

    session_id: str
    pr_number: Optional[int]
    pr_url: Optional[str]
    pr_repository: Optional[str]
    timestamp: Optional[datetime]
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "PrLinkRecord":
        ts = data.get("timestamp")
        return cls(
            session_id=data.get("sessionId", ""),
            pr_number=data.get("prNumber"),
            pr_url=data.get("prUrl"),
            pr_repository=data.get("prRepository"),
            timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None,
            raw=data,
        )


@dataclass
class AgentLifecycleRecord:
    """A `started` or `result` agent-lifecycle record.

    `started`: {key, agentId}. `result`: {key, agentId, result}. The `result`
    payload is arbitrary JSON (stored as JSONB).
    """

    kind: str  # "started" or "result"
    key: str
    agent_id: Optional[str]
    result: Any = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentLifecycleRecord":
        return cls(
            kind=data.get("type", ""),
            key=data.get("key", ""),
            agent_id=data.get("agentId"),
            result=data.get("result"),
            raw=data,
        )


class JSONLParser:
    """Parser for JSONL session transcript files."""

    def __init__(self, claude_dir: Optional[Path] = None):
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.projects_dir = self.claude_dir / "projects"

    def list_session_files(self, project_dir: Optional[Path] = None) -> list[Path]:
        """List all session JSONL files.

        Args:
            project_dir: Specific project dir, or None for all projects.

        Returns:
            List of JSONL file paths.
        """
        if project_dir:
            dirs = [project_dir]
        else:
            dirs = [p for p in self.projects_dir.iterdir() if p.is_dir()]

        files = []
        for d in dirs:
            files.extend(d.glob("*.jsonl"))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def parse_file(self, file_path: Path) -> dict[str, list]:
        """Parse a JSONL file into categorized records.

        Returns:
            Dict with keys: user, assistant, progress, system, summary,
                          queue_operation, file_history
        """
        records = {
            "user": [],
            "assistant": [],
            "progress": [],
            "system": [],
            "summary": [],
            "queue_operation": [],
            "file_history": [],
            "attachment": [],
            "session_meta": [],   # ai-title, custom-title, last-prompt, mode, etc.
            "pr_link": [],
            "agent_lifecycle": [],  # started, result
            "unknown": [],        # record types we don't model (kept as raw line nums)
        }

        # Record types folded into session_meta via SessionMetaRecord
        _META_TYPES = {
            "ai-title", "custom-title", "last-prompt",
            "permission-mode", "mode", "bridge-session", "agent-name",
        }

        with open(file_path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    record_type = data.get("type", "unknown")

                    if record_type == "user":
                        records["user"].append(UserMessage.from_dict(data))
                    elif record_type == "assistant":
                        records["assistant"].append(AssistantMessage.from_dict(data))
                    elif record_type == "progress":
                        records["progress"].append(ProgressEvent.from_dict(data))
                    elif record_type == "system":
                        records["system"].append(SystemMessage.from_dict(data))
                    elif record_type == "summary":
                        records["summary"].append(SummaryMessage.from_dict(data))
                    elif record_type == "queue-operation":
                        records["queue_operation"].append(QueueOperationMessage.from_dict(data))
                    elif record_type == "file-history-snapshot":
                        records["file_history"].append(FileHistorySnapshot.from_dict(data))
                    elif record_type == "attachment":
                        records["attachment"].append(AttachmentRecord.from_dict(data))
                    elif record_type in _META_TYPES:
                        records["session_meta"].append(SessionMetaRecord.from_dict(data))
                    elif record_type == "pr-link":
                        records["pr_link"].append(PrLinkRecord.from_dict(data))
                    elif record_type in ("started", "result"):
                        records["agent_lifecycle"].append(AgentLifecycleRecord.from_dict(data))
                    else:
                        records["unknown"].append((line_num, record_type))

                except json.JSONDecodeError as e:
                    print(f"Warning: Invalid JSON at {file_path}:{line_num}: {e}")
                except (KeyError, ValueError) as e:
                    print(f"Warning: Failed to parse record at {file_path}:{line_num}: {e}")

        return records

    def parse_user_messages(self, file_path: Path) -> list[UserMessage]:
        """Parse only user messages from a JSONL file."""
        return self.parse_file(file_path)["user"]

    def parse_all_user_messages(self) -> list[UserMessage]:
        """Parse user messages from all session files."""
        all_messages = []
        for file_path in self.list_session_files():
            all_messages.extend(self.parse_user_messages(file_path))
        return all_messages


# CLI for testing
if __name__ == "__main__":
    parser = JSONLParser()

    # Get recent session files
    files = parser.list_session_files()
    print(f"Found {len(files)} session files")

    # Parse first file
    if files:
        test_file = files[0]
        print(f"\nParsing: {test_file.name}")
        records = parser.parse_file(test_file)

        print(f"  User messages: {len(records['user'])}")
        print(f"  Assistant messages: {len(records['assistant'])}")
        print(f"  Progress events: {len(records['progress'])}")
        print(f"  System messages: {len(records['system'])}")
        print(f"  Summary messages: {len(records['summary'])}")
        print(f"  Queue operations: {len(records['queue_operation'])}")
        print(f"  File history snapshots: {len(records['file_history'])}")

        # Show user message breakdown
        user_msgs = records["user"]
        prompts = [m for m in user_msgs if m.is_direct_prompt]
        tool_results = [m for m in user_msgs if m.is_tool_result]
        other = [m for m in user_msgs if not m.is_direct_prompt and not m.is_tool_result]

        print("\n  User message breakdown:")
        print(f"    Direct prompts: {len(prompts)}")
        print(f"    Tool results: {len(tool_results)}")
        print(f"    Other: {len(other)}")

        # Show assistant message breakdown
        asst_msgs = records["assistant"]
        with_thinking = [m for m in asst_msgs if m.has_thinking]
        with_tools = [m for m in asst_msgs if m.has_tool_use]
        text_only = [m for m in asst_msgs if not m.has_thinking and not m.has_tool_use]

        print("\n  Assistant message breakdown:")
        print(f"    With thinking: {len(with_thinking)}")
        print(f"    With tool use: {len(with_tools)}")
        print(f"    Text only: {len(text_only)}")

        # Token usage
        total_input = sum(m.usage.total_input_tokens for m in asst_msgs)
        total_output = sum(m.usage.output_tokens for m in asst_msgs)
        total_cache = sum(m.usage.cache_read_input_tokens for m in asst_msgs)

        print("\n  Token usage:")
        print(f"    Total input: {total_input:,}")
        print(f"    Total output: {total_output:,}")
        print(f"    Cache reads: {total_cache:,}")

        # Models used
        models = set(m.model for m in asst_msgs)
        print(f"\n  Models: {', '.join(models)}")

        # Tool usage
        all_tools = []
        for m in asst_msgs:
            all_tools.extend(m.tool_names)
        tool_counts = {}
        for t in all_tools:
            tool_counts[t] = tool_counts.get(t, 0) + 1
        top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
        print("\n  Top tools:")
        for tool, count in top_tools:
            print(f"    {tool}: {count}")
