"""Friendly labels for the tools Claude Code v2.1.161-258 added.

Three surfaces render a `tool_use` chip — `tldr._tool_line`,
`angles.angle_agents`, `console/server._tool_summary` — and each had its own
`if name == "Bash"` ladder. Every tool from the v2.1.161-258 batch fell through
to the generic peek: the bare name, or `k=v, k=v` over the first three input
keys. Counted over 30 days: ToolSearch 3,182 · EnterWorktree 397 · Monitor 149 ·
SendUserFile 100 · TaskStop 96 · TaskUpdate 22 · Artifact 23 · ExitWorktree 19 ·
TaskCreate 16 · TaskOutput 2 · PushNotification 2 · SendFeedback 1.

`EnterWorktree` is the one that matters most: 397 uses, and it is the single
most consequential context event in a session — everything after it happens in
a different directory on a different branch — rendered as its bare name.

The salient field per tool lives in ONE table so the three renderers cannot
drift, exactly as `angles._WRITE_TOOLS` is seeded into the console.
"""
from __future__ import annotations

import pytest

from claude_session_db import tldr, tool_labels
from claude_session_db.console import server


# Real inputs from the corpus. Note TaskUpdate's camelCase `taskId` against
# TaskStop's snake_case `task_id` — Claude Code's inconsistency, and exactly
# the sort of thing a per-renderer ladder gets wrong in one place out of three.
CASES = {
    "EnterWorktree":    ({"name": "network-inventory-v1"}, "network-inventory-v1"),
    "ExitWorktree":     ({"action": "keep"}, "keep"),
    "ToolSearch":       ({"query": "select:Read,Edit", "max_results": 5},
                         "select:Read,Edit"),
    "Monitor":          ({"command": "zsh watch670.sh",
                          "description": "PR 670 merge + prod deploy-live",
                          "timeout_ms": 2700000, "persistent": False},
                         "PR 670 merge + prod deploy-live"),
    "SendUserFile":     ({"files": ["/tmp/a/budget-after.png",
                                    "/tmp/a/budget-detail.png"],
                          "status": "normal", "caption": "Budget surface"},
                         "Budget surface"),
    "Artifact":         ({"file_path": "/tmp/a/vopak-timeline.html",
                          "description": "Project timeline", "favicon": "x"},
                         "Project timeline"),
    "PushNotification": ({"message": "Ship queue complete", "status": "proactive"},
                         "Ship queue complete"),
    "SendFeedback":     ({"type": "bug", "title": "Manager session dispatched",
                          "details": "long"}, "Manager session dispatched"),
    "TaskCreate":       ({"subject": "Phase A — repo scaffold",
                          "description": "d", "activeForm": "Building"},
                         "Phase A — repo scaffold"),
    "TaskUpdate":       ({"taskId": "1", "status": "in_progress"}, "1"),
    "TaskStop":         ({"task_id": "ab65926341661ecc3"}, "ab65926341661ecc3"),
    "TaskOutput":       ({"task_id": "aab8136ff0c055804", "block": True},
                         "aab8136ff0c055804"),
    "ListAgents":       ({}, ""),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_tool_is_known(name):
    assert tool_labels.is_known(name), f"{name} falls through to the generic peek"


@pytest.mark.parametrize("name", sorted(CASES))
def test_salient_field_is_extracted(name):
    inp, expected = CASES[name]
    assert tool_labels.tool_target(name, inp) == expected


@pytest.mark.parametrize("name", sorted(CASES))
def test_tldr_renders_the_target_not_a_random_string(name):
    """`_tool_line`'s fallback is "first string value in the input", which for
    Monitor returns the raw shell command and for SendFeedback the whole bug
    report."""
    inp, expected = CASES[name]
    line = tldr._tool_line(name, inp)
    assert line.startswith(f"{name}(")
    if expected:
        assert expected[:40] in line


@pytest.mark.parametrize("name", sorted(CASES))
def test_console_renders_a_label(name):
    inp, _ = CASES[name]
    label, _detail = server._tool_summary(name, inp)
    assert label and label != name.lower()


def test_enter_worktree_reads_like_an_event():
    label, _ = server._tool_summary("EnterWorktree", {"name": "net-v1"})
    assert label == "enter worktree net-v1"


def test_enter_worktree_falls_back_to_path():
    """253 of 397 calls carry `name`; the other 144 carry `path`."""
    assert tool_labels.tool_target("EnterWorktree", {"path": "/a/b/wt"}) == "/a/b/wt"


def test_task_update_carries_the_status_in_the_label():
    """The id alone says nothing; `1 → in_progress` is the event."""
    label, _ = server._tool_summary("TaskUpdate", {"taskId": "1",
                                                   "status": "in_progress"})
    assert label == "task update 1 → in_progress"


def test_send_user_file_counts_the_files_and_basenames_them():
    """`files` is a list of absolute paths; str(list) is unreadable."""
    label, detail = server._tool_summary(
        "SendUserFile", {"files": ["/very/long/a.png", "/very/long/b.png"],
                         "caption": "two charts"})
    assert "× 2" in label and "two charts" in label
    assert detail == "a.png, b.png"


def test_file_list_over_three_is_elided():
    got = tool_labels._stringify([f"/x/{i}.png" for i in range(6)])
    assert got == "0.png, 1.png, 2.png (+3)"


def test_exit_worktree_flags_a_discard():
    """"exit worktree" and "exit worktree, throwing the work away" must not
    render identically."""
    label, _ = server._tool_summary("ExitWorktree", {"action": "discard",
                                                     "discard_changes": True})
    assert "discarding changes" in label


def test_unknown_tool_still_falls_through_to_the_generic_peek():
    label, peek = server._tool_summary("SomeToolFromTheFuture", {"a": 1, "b": "x"})
    assert label == "SomeToolFromTheFuture"
    assert "a=1" in peek


def test_known_tool_ladders_are_not_hijacked():
    """This module fills the hole; it does not take over the existing ladder."""
    for name in ("Bash", "Edit", "Write", "Read", "Skill", "Agent", "SendMessage"):
        assert not tool_labels.is_known(name), f"{name} must keep its own branch"
    assert server._tool_summary("Bash", {"command": "ls -la",
                                         "description": "list"})[0] == "ls -la"


def test_todowrite_branch_is_kept_and_marked_legacy():
    """Replaced by the Task* family, but the archive is lossless: older
    sessions carry real TodoWrite calls that must keep rendering."""
    import inspect
    src = inspect.getsource(server._tool_summary)
    assert 'if name in ("TodoWrite",):' in src
    assert "LEGACY" in src
    assert server._tool_summary("TodoWrite", {})[0] == "TodoWrite"


def test_orchestration_family_is_one_angle():
    """`angle_agents` used to see only Agent/SendMessage/TaskStop, so a turn
    that created three background tasks and updated two showed nothing."""
    fam = tool_labels.ORCHESTRATION_TOOLS
    for name in ("Agent", "SendMessage", "TaskCreate", "TaskUpdate",
                 "TaskStop", "TaskOutput", "ListAgents"):
        assert name in fam


def test_angles_uses_the_shared_family():
    import inspect
    from claude_session_db import angles
    src = inspect.getsource(angles.angle_agents)
    assert "tool_labels.ORCHESTRATION_TOOLS" in src
    assert "tool_labels.tool_label" in src


def test_no_renderer_hardcodes_its_own_table():
    """One table, three readers — the whole point."""
    import inspect
    for mod, fn in ((tldr, tldr._tool_line),
                    (server, server._tool_summary)):
        assert "tool_labels." in inspect.getsource(fn)
