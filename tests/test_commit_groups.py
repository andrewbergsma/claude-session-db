"""Unit tests for the Git rail's by-branch commit grouping.

Real git repositories under tmp_path, for the same reason the repos lens tests
use them: the grouper reads git's own topology, so a stubbed git would only
test the stub. What is pinned here is every claim with a wrong answer readily
available — the first-parent line owning its commits, a merge's side list
naming the branch that landed, a PR's headRefName beating the merge subject
(the only name that survives `--delete-branch`), a live unmerged branch keeping
its own group, no commit lost or duplicated, and the doctrine that an
underivable topology degrades to a note instead of raising. Run:

    uv run --extra dev pytest tests/test_commit_groups.py -q
"""
import subprocess

import pytest

from claude_session_db.console import server as S


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def commit(cwd, name, msg):
    (cwd / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", msg)
    return git(cwd, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A repo whose history exercises all four grouping rules at once.

        main ── A ── B ──────────── M1 ──── M2 ──── C
                      \\           /       /
                       feat/gone  F1,F2   /   (merged with a `Merge branch` subject,
                        \\               /        then deleted)
                         feat/pr ─ P1 ─┘   (merged with a gh-style subject + a PR)
        feat/live ── L1, L2                (never merged, still exists)
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    commit(root, "A", "A: first")
    base = commit(root, "B", "B: second")

    git(root, "checkout", "-b", "feat/gone")
    f1 = commit(root, "F1", "F1: side work")
    f2 = commit(root, "F2", "F2: more side work")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "feat/gone", "-m",
        "Merge branch 'feat/gone': the side work")
    m1 = git(root, "rev-parse", "HEAD").strip()
    git(root, "branch", "-D", "feat/gone")      # the usual post-merge cleanup

    git(root, "checkout", "-b", "pr-head", base)
    p1 = commit(root, "P1", "P1: the PR's only commit")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "pr-head", "-m",
        "Merge pull request #7 from owner/wrong-name-in-subject")
    m2 = git(root, "rev-parse", "HEAD").strip()
    git(root, "branch", "-D", "pr-head")

    c = commit(root, "C", "C: back on trunk")

    git(root, "checkout", "-b", "feat/live", base)
    l1 = commit(root, "L1", "L1: unmerged")
    l2 = commit(root, "L2", "L2: still unmerged")
    git(root, "checkout", "main")
    return {"root": root, "base": base, "f1": f1, "f2": f2, "m1": m1,
            "p1": p1, "m2": m2, "c": c, "l1": l1, "l2": l2}


def flat(root, n=40):
    """The flat list the payload already ships — `log --all`, the repo view's."""
    return S._all_commits(str(root), n)


def topo_of(root):
    return S._git_topology(str(root), S._head_branch_of(str(root)))


def by_branch(groups):
    return {g["branch"]: g for g in groups}


def short(root, full):
    return git(root, "rev-parse", "--short", full).strip()


# ---- the topology itself ---------------------------------------------------

def test_topology_resolves_trunk_and_the_first_parent_line(repo):
    t = topo_of(repo["root"])
    assert t["trunk"] == "main"          # probed, not guessed
    assert t["head_branch"] == "main"
    assert t["note"] is None
    # A, B, M1, M2, C — the merges' side commits are NOT on the line
    assert len(t["line"]) == 5
    assert len(t["merges"]) == 2
    assert {m["name"] for m in t["merges"]} == {"feat/gone",
                                               "wrong-name-in-subject"}


def test_side_list_is_exactly_what_the_merge_carried(repo):
    t = topo_of(repo["root"])
    sides = {m["name"]: set(m["side"]) for m in t["merges"]}
    assert sides["feat/gone"] == {repo["f1"], repo["f2"]}
    assert sides["wrong-name-in-subject"] == {repo["p1"]}


@pytest.mark.parametrize("subject,expect", [
    ("Merge pull request #20 from andrewbergsma/feat/x", "feat/x"),
    ("Merge branch 'feat/console-repos-lens': the lens", "feat/console-repos-lens"),
    ("Merge remote-tracking branch 'origin/feat/y' into main", "feat/y"),
    ("Merge branch 'master' into main", "master"),
    ("combine the two lines", None),
    ("", None),
])
def test_merge_subject_parsing(subject, expect):
    assert S._merged_branch_from_subject(subject) == expect


# ---- the grouping ----------------------------------------------------------

def test_every_commit_lands_in_exactly_one_group(repo):
    rows = flat(repo["root"])
    groups, note = S.group_commits(rows, topo_of(repo["root"]))
    assert note is None
    seen = [c["hash"] for g in groups for c in g["commits"]]
    assert sorted(seen) == sorted(c["hash"] for c in rows)   # nothing lost
    assert len(seen) == len(set(seen))                       # nothing duplicated
    assert all(g["count"] == len(g["commits"]) for g in groups)


def test_first_parent_line_belongs_to_the_current_branch(repo):
    root = repo["root"]
    groups, _ = S.group_commits(flat(root), topo_of(root))
    assert groups[0]["branch"] == "main"      # the current line always leads
    assert groups[0]["current"] is True
    assert groups[0]["merged"] is False
    main = {c["hash"] for c in groups[0]["commits"]}
    for key in ("base", "m1", "m2", "c"):
        assert short(root, repo[key]) in main
    # the merge commits stay on the line they were logged from, not with the
    # branch they pulled in
    assert short(root, repo["f1"]) not in main


def test_merged_group_is_named_marked_and_carries_its_merge_hash(repo):
    root = repo["root"]
    groups, _ = S.group_commits(flat(root), topo_of(root))
    g = by_branch(groups)["feat/gone"]
    assert g["merged"] is True
    assert g["merge_hash"] == short(root, repo["m1"])
    assert g["pr"] is None
    assert g["current"] is False
    assert {c["hash"] for c in g["commits"]} == {short(root, repo["f1"]),
                                                short(root, repo["f2"])}


def test_pr_head_ref_name_beats_the_merge_subject(repo):
    """A PR whose oids cover the side commits names the group.

    headRefName is the only name that survives `gh pr merge --delete-branch`;
    the subject is a fallback, and here it is deliberately wrong.
    """
    root = repo["root"]
    gh = {"prs": [{"number": 7, "state": "MERGED", "url": "https://x/pull/7",
                   "checks": "pass", "branch": "feat/the-real-name",
                   "oids": [repo["p1"]]}]}
    groups, _ = S.group_commits(flat(root), topo_of(root), gh)
    names = by_branch(groups)
    assert "feat/the-real-name" in names
    assert "wrong-name-in-subject" not in names
    g = names["feat/the-real-name"]
    assert g["merged"] is True and g["pr"]["number"] == 7
    assert g["pr"]["url"] == "https://x/pull/7"

    # …and with no gh listing at all, the subject is used rather than nothing
    plain = by_branch(S.group_commits(flat(root), topo_of(root))[0])
    assert "wrong-name-in-subject" in plain


def test_unmerged_local_branch_keeps_its_own_group(repo):
    root = repo["root"]
    groups, _ = S.group_commits(flat(root), topo_of(root))
    g = by_branch(groups)["feat/live"]
    assert g["merged"] is False
    assert g["merge_hash"] is None
    assert g["ahead"] in (2, None)        # None only on a git without the atom
    assert {c["hash"] for c in g["commits"]} == {short(root, repo["l1"]),
                                                short(root, repo["l2"])}


def test_groups_are_newest_first_after_the_current_branch(repo):
    groups, _ = S.group_commits(flat(repo["root"]), topo_of(repo["root"]))
    assert groups[0]["current"] is True
    rest = [g["branch"] for g in groups[1:]]
    # feat/live's commits are the newest of the three side groups
    assert rest[0] == "feat/live"
    assert set(rest) == {"feat/live", "feat/gone", "wrong-name-in-subject"}


def test_a_merge_with_an_unparseable_subject_still_groups(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    commit(root, "A", "A")
    base = git(root, "rev-parse", "HEAD").strip()
    git(root, "checkout", "-b", "side")
    side = commit(root, "S", "S: side")
    git(root, "checkout", "main")
    git(root, "merge", "--no-ff", "side", "-m", "combine the two lines")
    git(root, "branch", "-D", "side")
    groups, _ = S.group_commits(flat(root), topo_of(root))
    g = by_branch(groups)["(merged branch)"]
    assert g["merged"] is True
    assert [c["hash"] for c in g["commits"]] == [short(root, side)]
    assert base                                    # sanity: the fixture built


def test_a_worktree_branch_is_flagged_on_its_group(repo, tmp_path):
    root = repo["root"]
    wt = tmp_path / "wt"
    git(root, "worktree", "add", str(wt), "feat/live")
    g = by_branch(S.group_commits(flat(root), topo_of(root))[0])["feat/live"]
    assert g["worktree"] == str(wt)


def test_grouping_never_raises_and_degrades_to_a_note():
    # a topology that is garbage: the caller keeps its flat list, with a reason
    groups, note = S.group_commits([{"hash": "abc1234", "when": None}],
                                   {"line": None, "merges": 7})
    assert groups == []
    assert note and note.startswith("grouping failed:")

    # …and a genuinely empty input is not an error
    assert S.group_commits([], {"line": [], "note": None}) == ([], None)


def test_an_unreadable_first_parent_line_is_a_note_not_an_exception(tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    t = S._git_topology(str(empty))
    assert t["line"] == []
    assert "first-parent" in (t["note"] or "")
    assert S.group_commits([{"hash": "abc1234"}], t)[0][0]["branch"] == "HEAD"


def test_commit_dicts_are_carried_by_reference(repo):
    """The flat list and the groups must never disagree about a commit.

    Attribution (`pr`) is stamped on the flat rows before grouping, so the
    groups must hold those very dicts, not copies of them.
    """
    rows = flat(repo["root"])
    groups, _ = S.group_commits(rows, topo_of(repo["root"]))
    ids = {id(c) for g in groups for c in g["commits"]}
    assert ids == {id(c) for c in rows}


# ---- by-PR grouping --------------------------------------------------------
# The other partition: which PULL REQUEST each commit landed as. Pinned here is
# every rule with a wrong answer readily available — a squash marker read off
# the subject, a `Merge pull request #N` claiming its own side commits, gh's
# mergeCommit oid resolving a commit whose subject says nothing, a PR number
# with no gh row still forming a group, the no-PR leftovers trailing, and the
# mode falling back to by-branch when nothing resolves at all.

@pytest.fixture
def squash_repo(tmp_path):
    """A squash-merge trunk — the motivating case. Every landed commit IS a PR.

        main ── A (no PR) ── B (#756) ── C (#757) ── D (no PR)
    """
    root = tmp_path / "squash"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    a = commit(root, "A", "chore: bootstrap the repo")
    b = commit(root, "B", "feat(commercial): notice-clock state machine (#756)")
    c = commit(root, "C", "feat: crew de-identification (#757)")
    d = commit(root, "D", "chore: bump deps")
    return {"root": root, "a": a, "b": b, "c": c, "d": d}


def by_key(groups):
    """Groups keyed the way the UI addresses them: #N for a PR, else branch."""
    return {(f"#{g['pr']['number']}" if g.get("kind") == "pr" else g["branch"]): g
            for g in groups}


@pytest.mark.parametrize("subject,expect", [
    ("feat(commercial): notice-clock state machine (#756)", (756, "squash")),
    ("fix: trailing whitespace (#12)  ", (12, "squash")),
    ("Merge pull request #7 from owner/feat/x", (7, "merge")),
    ("Merge pull request #7", (7, "merge")),
    # a number that is NOT the squash marker: mid-subject, or no number at all
    ("revert the (#12) change and move on", (None, None)),
    ("Merge branch 'feat/x'", (None, None)),
    ("plain subject", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_pr_number_from_subject(subject, expect):
    assert S._pr_number_from_subject(subject) == expect


def test_squash_suffix_makes_one_group_per_pr(squash_repo):
    root = squash_repo["root"]
    groups, note = S.group_commits(flat(root), topo_of(root), None, "pr")
    assert note is None
    keys = by_key(groups)
    assert set(keys) == {"#756", "#757", "main"}
    assert keys["#756"]["count"] == 1
    assert keys["#756"]["commits"][0]["hash"] == short(root, squash_repo["b"])
    # the title is recovered by stripping the marker — it IS the PR title
    assert keys["#756"]["pr"]["title"] == \
        "feat(commercial): notice-clock state machine"
    # no gh row behind it: a plain header, with no invented state or link
    assert keys["#756"]["pr"]["known"] is False
    assert keys["#756"]["pr"]["state"] is None
    assert keys["#756"]["pr"]["url"] is None
    assert keys["#756"]["branch"] == "#756"


def test_commits_with_no_pr_trail_in_a_direct_group(squash_repo):
    root = squash_repo["root"]
    groups, _ = S.group_commits(flat(root), topo_of(root), None, "pr")
    last = groups[-1]
    assert last["kind"] == "direct"          # trailing, even though D is newest
    assert last["branch"] == "main"
    assert {c["hash"] for c in last["commits"]} == {short(root, squash_repo["a"]),
                                                   short(root, squash_repo["d"])}
    # …and the PR groups above it are newest-first
    assert [g["pr"]["number"] for g in groups[:-1]] == [757, 756]


def test_gh_row_supplies_the_title_state_and_link(squash_repo):
    root = squash_repo["root"]
    gh = {"prs": [{"number": 756, "title": "Notice clock", "state": "MERGED",
                   "draft": False, "checks": "pass", "branch": "feat/clock",
                   "url": "https://x/pull/756", "merged_at": "2026-08-30T10:00:00Z",
                   "oids": [], "merge_oid": None}]}
    g = by_key(S.group_commits(flat(root), topo_of(root), gh, "pr")[0])["#756"]
    assert g["pr"] == {"number": 756, "title": "Notice clock", "state": "MERGED",
                       "draft": False, "checks": "pass", "branch": "feat/clock",
                       "url": "https://x/pull/756",
                       "merged_at": "2026-08-30T10:00:00Z", "known": True}
    assert g["branch"] == "feat/clock"
    assert g["merged"] is True                # landed work folds by default


def test_merge_commit_and_its_side_commits_are_one_pr_group(repo):
    """`Merge pull request #7` claims the merge AND everything it carried."""
    root = repo["root"]
    groups, _ = S.group_commits(flat(root), topo_of(root), None, "pr")
    keys = by_key(groups)
    assert {c["hash"] for c in keys["#7"]["commits"]} == {short(root, repo["m2"]),
                                                         short(root, repo["p1"])}
    # the OTHER merge named no PR, so its side branch keeps its branch group
    assert keys["feat/gone"]["kind"] == "branch"
    assert keys["feat/gone"]["merged"] is True


def test_merge_commit_oid_resolves_a_commit_with_no_subject_marker(squash_repo):
    """gh's own record of where a PR landed, for a subject that says nothing."""
    root = squash_repo["root"]
    gh = {"prs": [{"number": 900, "title": "Bump deps", "state": "MERGED",
                   "branch": "chore/deps", "url": "https://x/pull/900",
                   "merged_at": None, "checks": None, "draft": False,
                   "oids": [], "merge_oid": squash_repo["d"]}]}
    keys = by_key(S.group_commits(flat(root), topo_of(root), gh, "pr")[0])
    assert {c["hash"] for c in keys["#900"]["commits"]} == \
        {short(root, squash_repo["d"])}
    # A alone is left with no PR
    assert {c["hash"] for c in keys["main"]["commits"]} == \
        {short(root, squash_repo["a"])}


def test_open_pr_heads_its_unmerged_branch_with_ahead_behind(repo):
    root = repo["root"]
    gh = {"prs": [{"number": 42, "title": "Live work", "state": "OPEN",
                   "draft": False, "checks": "pending", "branch": "feat/live",
                   "url": "https://x/pull/42", "merged_at": None,
                   "oids": [repo["l1"], repo["l2"]], "merge_oid": None}]}
    g = by_key(S.group_commits(flat(root), topo_of(root), gh, "pr")[0])["#42"]
    assert g["kind"] == "pr"
    assert g["merged"] is False               # open work opens by default
    assert g["ahead"] in (2, None)            # None only on a git without the atom
    assert {c["hash"] for c in g["commits"]} == {short(root, repo["l1"]),
                                                short(root, repo["l2"])}


def test_pr_mode_loses_and_duplicates_nothing(repo):
    rows = flat(repo["root"])
    groups, _ = S.group_commits(rows, topo_of(repo["root"]), None, "pr")
    seen = [c["hash"] for g in groups for c in g["commits"]]
    assert sorted(seen) == sorted(c["hash"] for c in rows)
    assert len(seen) == len(set(seen))
    assert all(g["count"] == len(g["commits"]) for g in groups)
    assert {id(c) for g in groups for c in g["commits"]} == {id(c) for c in rows}


def test_branch_mode_is_untouched_by_the_new_default(repo):
    """The default argument still means by-branch, exactly as before."""
    root = repo["root"]
    a, _ = S.group_commits(flat(root), topo_of(root))
    b, _ = S.group_commits(flat(root), topo_of(root), None, "branch")
    assert [g["branch"] for g in a] == [g["branch"] for g in b]
    assert a[0]["current"] is True
    assert all(g["kind"] == "branch" for g in a)


# ---- mode selection --------------------------------------------------------

def test_payload_defaults_to_pr_when_anything_resolves(squash_repo):
    root = squash_repo["root"]
    p = S.commit_groups_payload(flat(root), topo_of(root))
    assert p["group_mode"] == "pr"
    assert p["group_alt"]["mode"] == "branch"
    assert any(g.get("kind") == "pr" for g in p["groups"])
    # the alternative is the branch partition, over the very same commit dicts
    assert [g["branch"] for g in p["group_alt"]["groups"]] == ["main"]
    ids = {id(c) for g in p["groups"] for c in g["commits"]}
    assert ids == {id(c) for g in p["group_alt"]["groups"] for c in g["commits"]}


def test_payload_falls_back_to_branch_with_no_prs_and_offers_no_toggle(tmp_path):
    root = tmp_path / "nopr"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    commit(root, "A", "chore: one")
    commit(root, "B", "chore: two")
    p = S.commit_groups_payload(flat(root), topo_of(root))
    assert p["group_mode"] == "branch"
    assert p["group_alt"] is None             # nothing to toggle to
    assert [g["branch"] for g in p["groups"]] == ["main"]
    assert p["group_note"] is None


def test_pr_grouping_never_raises_and_degrades_to_a_note():
    groups, note = S.group_commits([{"hash": "abc1234", "when": None}],
                                   {"line": None, "merges": 7}, None, "pr")
    assert groups == []
    assert note and note.startswith("grouping failed:")
    # …and the payload wrapper degrades with it rather than propagating
    p = S.commit_groups_payload([{"hash": "abc1234", "when": None}],
                                {"line": None, "merges": 7})
    assert p["groups"] == [] and p["group_mode"] == "branch"
    assert p["group_alt"] is None
    assert p["group_note"].startswith("grouping failed:")
