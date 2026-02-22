"""Parser for sessions-index.json files."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
import json


@dataclass
class SessionIndexEntry:
    """A single session entry from sessions-index.json."""

    # Required fields
    session_id: str
    full_path: Path
    file_mtime: int  # Epoch milliseconds
    first_prompt: str
    message_count: int
    created: datetime
    modified: datetime
    git_branch: str
    project_path: Path
    is_sidechain: bool

    # Optional fields (may be missing in older sessions)
    summary: Optional[str] = None

    # Computed properties
    @property
    def file_mtime_datetime(self) -> datetime:
        """Convert file_mtime epoch ms to datetime."""
        return datetime.fromtimestamp(self.file_mtime / 1000)

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        return (self.modified - self.created).total_seconds()

    @property
    def project_name(self) -> str:
        """Extract project name from path."""
        return self.project_path.name

    @classmethod
    def from_dict(cls, data: dict) -> "SessionIndexEntry":
        """Parse from JSON dict."""
        return cls(
            session_id=data["sessionId"],
            full_path=Path(data["fullPath"]),
            file_mtime=data["fileMtime"],
            first_prompt=data["firstPrompt"],
            message_count=data["messageCount"],
            created=datetime.fromisoformat(data["created"].replace("Z", "+00:00")),
            modified=datetime.fromisoformat(data["modified"].replace("Z", "+00:00")),
            git_branch=data["gitBranch"],
            project_path=Path(data["projectPath"]),
            is_sidechain=data["isSidechain"],
            summary=data.get("summary"),  # Optional
        )

    def to_dict(self) -> dict:
        """Convert to flat dict for database/CSV export."""
        return {
            "session_id": self.session_id,
            "full_path": str(self.full_path),
            "file_mtime": self.file_mtime,
            "file_mtime_datetime": self.file_mtime_datetime.isoformat(),
            "first_prompt": self.first_prompt,
            "summary": self.summary,
            "message_count": self.message_count,
            "created": self.created.isoformat(),
            "modified": self.modified.isoformat(),
            "duration_seconds": self.duration_seconds,
            "git_branch": self.git_branch,
            "project_path": str(self.project_path),
            "project_name": self.project_name,
            "is_sidechain": self.is_sidechain,
        }


class SessionsIndexParser:
    """Parser for sessions-index.json files."""

    def __init__(self, claude_dir: Optional[Path] = None):
        """Initialize parser.

        Args:
            claude_dir: Path to ~/.claude directory. Defaults to ~/.claude.
        """
        self.claude_dir = claude_dir or Path.home() / ".claude"
        self.projects_dir = self.claude_dir / "projects"

    def list_projects(self) -> list[Path]:
        """List all project directories with sessions."""
        if not self.projects_dir.exists():
            return []
        return [
            p
            for p in self.projects_dir.iterdir()
            if p.is_dir() and (p / "sessions-index.json").exists()
        ]

    def decode_project_path(self, encoded: str) -> Path:
        """Decode project path from directory name.

        Example: -Users-andrew-GitHub-knowledge -> /Users/andrew/GitHub/knowledge
        """
        # Remove leading dash and replace remaining dashes with /
        if encoded.startswith("-"):
            encoded = encoded[1:]
        return Path("/" + encoded.replace("-", "/"))

    def parse_project(self, project_dir: Path) -> list[SessionIndexEntry]:
        """Parse sessions-index.json for a project.

        Args:
            project_dir: Path to project directory (e.g., ~/.claude/projects/-Users-...)

        Returns:
            List of SessionIndexEntry objects.
        """
        index_file = project_dir / "sessions-index.json"
        if not index_file.exists():
            return []

        with open(index_file) as f:
            data = json.load(f)

        entries = []
        for entry_data in data.get("entries", []):
            try:
                entries.append(SessionIndexEntry.from_dict(entry_data))
            except (KeyError, ValueError) as e:
                # Log parsing errors but continue
                print(f"Warning: Failed to parse entry in {index_file}: {e}")

        return entries

    def parse_all(self) -> list[SessionIndexEntry]:
        """Parse all sessions-index.json files across all projects.

        Returns:
            List of all SessionIndexEntry objects.
        """
        all_entries = []
        for project_dir in self.list_projects():
            all_entries.extend(self.parse_project(project_dir))
        return all_entries

    def to_dicts(self, entries: Optional[list[SessionIndexEntry]] = None) -> list[dict]:
        """Convert entries to list of dicts for export.

        Args:
            entries: List of entries to convert. If None, parses all.

        Returns:
            List of flat dictionaries.
        """
        if entries is None:
            entries = self.parse_all()
        return [e.to_dict() for e in entries]

    def to_json(self, entries: Optional[list[SessionIndexEntry]] = None) -> str:
        """Export entries as JSON string.

        Args:
            entries: List of entries to convert. If None, parses all.

        Returns:
            JSON string.
        """
        return json.dumps(self.to_dicts(entries), indent=2, default=str)


# CLI for testing
if __name__ == "__main__":
    parser = SessionsIndexParser()
    entries = parser.parse_all()

    print(f"Found {len(entries)} sessions across {len(parser.list_projects())} projects")
    print()

    # Print summary
    for entry in sorted(entries, key=lambda e: e.modified, reverse=True)[:10]:
        print(f"{entry.session_id[:8]}  {entry.message_count:3d} msgs  {entry.summary[:50]}")
