"""Unit tests for the cross-repo lens (/api/repos).

Real git repositories under tmp_path — the lens is a git reader, so stubbing
git would only test the stub. What is pinned here is the behaviour that has a
wrong answer available: worktrees folding into their parent (not standing as
their own repo), trunk resolution when the branch is `master`, ahead/behind
against the trunk, stale worktree registrations, and the doctrine that a
missing directory degrades ONE row instead of raising. Run:

    uv run --extra dev pytest tests/test_repos_lens.py -q
"""
import json
import subprocess

import pytest

from claude_session_db.console import server as S


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A repo on `main` with one commit, plus an `origin` it is level with."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@t"); git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    git(root, "add", "-A"); git(root, "commit", "-qm", "first")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "-u", "origin", "main")
    return root


# ---- registry: a worktree is a checkout, not a repository -------------------
def test_worktree_folds_into_parent(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert S._repo_toplevel(str(wt)) == str(repo.resolve())
    assert S._repo_toplevel(str(repo)) == str(repo.resolve())


def test_non_repo_and_missing_cwd_are_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert S._repo_toplevel(str(plain)) is None
    assert S._repo_toplevel(str(tmp_path / "nope")) is None
    assert S._repo_toplevel("") is None


# ---- trunk resolution -------------------------------------------------------
def test_trunk_probes_master_when_no_origin_head(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    git(root, "init", "-b", "master")
    git(root, "config", "user.email", "t@t"); git(root, "config", "user.name", "t")
    (root / "f").write_text("x")
    git(root, "add", "-A"); git(root, "commit", "-qm", "c")
    assert S._trunk_of(str(root)) == ("master", "probe")


def test_trunk_unresolved_has_no_false_main(tmp_path):
    root = tmp_path / "odd"
    root.mkdir()
    git(root, "init", "-b", "wip")
    git(root, "config", "user.email", "t@t"); git(root, "config", "user.name", "t")
    (root / "f").write_text("x")
    git(root, "add", "-A"); git(root, "commit", "-qm", "c")
    name, how = S._trunk_of(str(root))
    assert name is None and how == "unresolved"


# ---- branch inventory -------------------------------------------------------
def test_ahead_behind_against_trunk(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("two\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "feature work")
    git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("three\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "trunk moved")

    rows, total, note = S._branch_inventory(str(repo), "main")
    assert note is None and total == 2
    by = {b["name"]: b for b in rows}
    assert by["main"]["is_trunk"] is True
    assert (by["feature"]["ahead"], by["feature"]["behind"]) == (1, 1)


def test_branch_inventory_without_trunk_keeps_the_list(repo):
    """No trunk resolved -> counts unknown, but the branches still show."""
    rows, total, note = S._branch_inventory(str(repo), None)
    assert note is None and total == 1
    assert rows[0]["ahead"] is None and rows[0]["name"] == "main"


# ---- worktree inventory -----------------------------------------------------
def test_worktree_listed_and_stale_registration_flagged(repo, tmp_path):
    live = tmp_path / "live"
    gone = tmp_path / "gone"
    git(repo, "worktree", "add", "-q", "-b", "live-br", str(live))
    git(repo, "worktree", "add", "-q", "-b", "gone-br", str(gone))
    import shutil
    shutil.rmtree(gone)                       # the folder goes; git's record stays

    trees, total = S._worktree_inventory(str(repo))
    assert total == 2                          # the main checkout is not a worktree
    by = {t["branch"]: t for t in trees}
    assert by["live-br"]["exists"] is True
    assert by["gone-br"]["exists"] is False


# ---- the row ----------------------------------------------------------------
def test_snapshot_row_shape(repo):
    (repo / "a.txt").write_text("edited\n")    # tracked modification
    (repo / "new.txt").write_text("u\n")       # untracked
    r = S.repo_snapshot(str(repo))
    assert r["ok"] and r["name"] == "work"
    assert r["trunk"] == "main" and r["branch"] == "main"
    assert r["dirty"] == 1 and r["untracked"] == 1
    assert r["trunk_ahead"] == 0 and r["trunk_behind"] == 0
    assert r["last_commit"]["subject"] == "first"


def test_unpushed_trunk_is_visible(repo):
    (repo / "d.txt").write_text("d\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "local only")
    r = S.repo_snapshot(str(repo))
    assert r["trunk_ahead"] == 1 and r["trunk_behind"] == 0


def test_missing_directory_degrades_one_row(tmp_path):
    r = S.repo_snapshot(str(tmp_path / "vanished"))
    assert r["ok"] is False and "gone" in r["reason"]


def test_plain_directory_is_not_a_repo_row(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = S.repo_snapshot(str(plain))
    assert r["ok"] is False and r["reason"] == "not a git repository"


# ---- payload doctrine: the request path never fans out ----------------------
def test_payload_reports_warming_before_the_first_walk(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_REPOS", {})
    monkeypatch.setattr(S, "REPOS_FILE", tmp_path / "absent.json")
    out = S.repos_payload()
    assert out["warming"] is True and out["repos"] == []


def test_payload_serves_the_store_without_touching_git(monkeypatch, tmp_path):
    store = {"repos": [{"name": "x", "ok": True}], "registry_source": "test",
             "generated_at": 0, "refresh_s": 90}
    monkeypatch.setattr(S, "_REPOS", {})
    f = tmp_path / "repos.json"
    f.write_text(json.dumps(store))
    monkeypatch.setattr(S, "REPOS_FILE", f)

    def boom(*a, **k):                      # any git call here is a bug
        raise AssertionError("repos_payload must not run git")
    monkeypatch.setattr(S, "_git", boom)

    out = S.repos_payload()
    assert out["repos"] == store["repos"] and out["age_s"] >= 0


# ---- detail lens ------------------------------------------------------------
def test_web_url_from_both_remote_spellings(repo):
    git(repo, "remote", "set-url", "origin", "git@github.com:o/r.git")
    assert S._web_url(str(repo)) == "https://github.com/o/r"
    git(repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
    assert S._web_url(str(repo)) == "https://github.com/o/r"
    git(repo, "remote", "set-url", "origin", "https://gitlab.com/o/r.git")
    assert S._web_url(str(repo)) is None    # never guess a non-GitHub URL


def test_commits_span_all_refs_and_flag_merges(repo):
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "s.txt").write_text("s\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "on side")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "side", "-m", "merge side")

    cs = S._all_commits(str(repo), 10)
    subs = [c["subject"] for c in cs]
    assert "on side" in subs                # a branch commit HEAD's log would show
    assert any(c["is_merge"] for c in cs)
    assert not any(c["is_merge"] for c in cs if c["subject"] == "on side")


def test_symbolic_head_refs_are_dropped(repo, monkeypatch):
    """origin/HEAD is an alias for the default branch — a badge for it links nowhere."""
    calls = {}

    def fake(args, cwd):
        if args[0] == "log":
            return 0, S._FS.join(["abc1234", "s", "2026-01-01T00:00:00Z", "a",
                                  "HEAD -> main, origin/main, origin/HEAD", "p1"])
        return S._git(args, cwd)
    monkeypatch.setattr(S, "_git", fake)
    refs = S._all_commits(str(repo), 1)[0]["refs"]
    assert refs == ["HEAD -> main", "origin/main"]


def test_detail_refuses_a_root_the_registry_does_not_know(monkeypatch):
    monkeypatch.setattr(S, "_repo_registry", lambda force=False: ([], "test"))
    payload, code = S.repo_detail_payload("", "/etc")
    assert code == 404 and payload["error"] == "unknown repo root"


def test_detail_needs_an_address(monkeypatch):
    monkeypatch.setattr(S, "_repo_registry", lambda force=False: ([], "test"))
    payload, code = S.repo_detail_payload("", "")
    assert code == 400


def test_detail_lifts_the_card_caps(repo, monkeypatch):
    for i in range(3):
        git(repo, "branch", f"b{i}")
    monkeypatch.setattr(S, "REPO_BRANCH_CAP", 1)          # card would show one
    monkeypatch.setattr(S, "_repo_registry", lambda force=False: ([str(repo)], "test"))
    card = S.repo_snapshot(str(repo))
    assert len(card["branches"]) == 1 and card["branch_total"] == 4
    detail, code = S.repo_detail_payload("", str(repo))
    assert code == 200 and len(detail["branches"]) == 4   # detail shows all
