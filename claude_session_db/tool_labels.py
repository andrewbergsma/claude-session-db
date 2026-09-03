"""ONE table of friendly tool labels, for every renderer.

The problem
-----------
Three surfaces render a `tool_use` as a human-readable chip — `tldr._tool_line`,
`angles.angle_agents`, and `console/server._tool_summary` — and each grew its
own hardcoded `if name == "Bash"` ladder. A tool none of them knew about fell
through to a generic peek: the bare tool name, or `k=v, k=v` over the first
three input keys.

Claude Code v2.1.161-258 added a batch of tools that all landed in that hole.
Counted over 30 days of ~/.claude/projects:

    ToolSearch      3,182     EnterWorktree      397     Monitor       149
    SendUserFile      100     TaskStop            96     TaskUpdate     22
    Artifact           23     ExitWorktree        19     TaskCreate     16
    TaskOutput          2     PushNotification     2     SendFeedback    1

`ToolSearch query=select:mcp__claude_ai_kmcp__get_entry,mcp__…` truncated at 40
characters is not a chip anyone can read, and `EnterWorktree` — 397 uses, the
single most consequential context event in a session, since everything after it
happens somewhere else — rendered as its bare name.

The fix
-------
The salient field per tool lives HERE, once, and the three renderers read it.
They keep their own formatting (tldr wants `Name(target)`, the console wants a
`(label, detail)` pair) but they can no longer disagree about WHAT the salient
field is, in the same way `angles._WRITE_TOOLS` is seeded into the console so
the miner and the UI cannot drift on what counts as a write.

Adding a tool is one line in TOOL_TARGET_FIELDS.
"""
from __future__ import annotations

from typing import Any, Optional

# tool name -> ordered input keys, most salient first. The first key present
# and non-empty wins. Field names are taken from real payloads in the corpus,
# not guessed — note TaskUpdate's camelCase `taskId` against TaskStop's and
# TaskOutput's snake_case `task_id`, which is Claude Code's inconsistency, not
# a typo here.
TOOL_TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    # --- context / worktree ---------------------------------------------
    # The most consequential event in a session after a prompt: everything
    # afterwards happens in a different directory on a different branch.
    "EnterWorktree":    ("name", "path"),
    "ExitWorktree":     ("action",),

    # --- user-facing output ---------------------------------------------
    "SendUserFile":     ("caption", "files"),
    "Artifact":         ("description", "label", "file_path", "url"),
    "ArtifactComments": ("file_path", "url"),
    "ArtifactData":     ("file_path", "url"),
    "ArtifactCheck":    ("file_path", "url"),
    "PushNotification": ("message",),
    "SendFeedback":     ("title", "type"),

    # --- background work / orchestration --------------------------------
    "Monitor":          ("description", "command"),
    "ListAgents":       (),                      # takes no salient input
    "TaskCreate":       ("subject", "description", "activeForm"),
    "TaskUpdate":       ("taskId", "task_id", "status"),
    "TaskStop":         ("task_id", "taskId"),
    "TaskOutput":       ("task_id", "taskId"),

    # --- tool discovery --------------------------------------------------
    "ToolSearch":       ("query",),
}

# Tools whose bare name does not say what happened. The verb replaces the name
# in a chip; everything else keeps its own name.
TOOL_VERBS: dict[str, str] = {
    "EnterWorktree":    "enter worktree",
    "ExitWorktree":     "exit worktree",
    "SendUserFile":     "send file",
    "PushNotification": "notify",
    "SendFeedback":     "feedback",
    "ListAgents":       "list agents",
    "TaskCreate":       "task create",
    "TaskUpdate":       "task update",
    "TaskStop":         "task stop",
    "TaskOutput":       "task output",
    "ToolSearch":       "tool search",
    "Monitor":          "monitor",
    "Artifact":         "artifact",
}

# The orchestration family: tools that create, steer, observe or stop other
# units of work. `angles.angle_agents` headlines these together, because
# "what did this turn set running" is one question, not six.
ORCHESTRATION_TOOLS = (
    "Agent", "Task", "SendMessage", "ListAgents",
    "TaskCreate", "TaskUpdate", "TaskStop", "TaskOutput",
)


def _stringify(value: Any) -> str:
    """One readable line out of a field value.

    Lists matter: `SendUserFile.files` is a list of absolute paths, and
    `str(["/very/long/path", ...])` is unreadable. Paths are basenamed and
    counted instead.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value if v]
        if not items:
            return ""
        names = [i.rsplit("/", 1)[-1] for i in items]
        head = ", ".join(names[:3])
        return head + (f" (+{len(names) - 3})" if len(names) > 3 else "")
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def tool_target(name: str, inp: Optional[dict]) -> str:
    """The one salient field of a tool_use, or "" when the tool has none.

    "" (not None) so callers can treat it as a plain string.
    """
    fields = TOOL_TARGET_FIELDS.get(name)
    if not fields:
        return ""
    inp = inp or {}
    for key in fields:
        text = _stringify(inp.get(key))
        if text:
            return text
    return ""


def is_known(name: str) -> bool:
    """True when this module has a label for the tool."""
    return name in TOOL_TARGET_FIELDS


def tool_label(name: str, inp: Optional[dict]) -> tuple[str, str]:
    """(label, detail) for a tool this module knows, else ("", "").

    `label` is the short chip; `detail` is the longer peek a UI can expand to.
    Callers that already handle a tool (Bash, Edit, Agent, …) keep their own
    branch — this fills the hole, it does not take over the ladder.
    """
    if not is_known(name):
        return ("", "")
    inp = inp or {}
    verb = TOOL_VERBS.get(name, name)
    target = tool_target(name, inp)

    # A few tools carry a second field that changes the MEANING of the first,
    # so it belongs in the label rather than the peek.
    if name == "TaskUpdate" and inp.get("status"):
        label = f"{verb} {target} → {inp['status']}".strip()
    elif name == "ExitWorktree" and inp.get("discard_changes"):
        label = f"{verb} {target} (discarding changes)".strip()
    elif name == "SendFeedback" and inp.get("type"):
        label = f"{verb}[{inp['type']}] {_stringify(inp.get('title'))}".strip()
    elif name == "SendUserFile":
        n = len(inp.get("files") or [])
        label = f"{verb}{'s' if n != 1 else ''} × {n} {target}".strip() if n \
            else f"{verb} {target}".strip()
    else:
        label = f"{verb} {target}".strip() if target else verb

    detail_keys = {
        "Monitor": "command",
        "TaskCreate": "description",
        "Artifact": "file_path",
        "SendFeedback": "details",
        "PushNotification": "message",
        "SendUserFile": "files",
        "ToolSearch": "query",
    }
    detail = _stringify(inp.get(detail_keys.get(name, ""))) if name in detail_keys else ""
    return (label, detail)
