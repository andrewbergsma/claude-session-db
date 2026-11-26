"""autosummary continuation passes: a summarized session that is resumed gets pass N."""

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "autosummary.py"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def auto(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("autosummary_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    return mod


def cfg(**kw):
    base = dict(min_prompts=2, min_delta_prompts=1, max_attempts=2, dry_run=False)
    return SimpleNamespace(**{**base, **kw})


def prompt(ts, text="do the thing"):
    return {"type": "user", "entrypoint": "cli", "cwd": "/repo", "timestamp": ts,
            "message": {"role": "user", "content": text}}


def reply(ts):
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": "ok"}]}}


def transcript(tmp_path, records, age_s=0):
    p = tmp_path / f"{SID}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


BASE = [prompt("2026-11-26T10:00:00Z"), reply("2026-11-26T10:00:05Z"),
        prompt("2026-11-26T10:05:00Z"), reply("2026-11-26T10:05:09Z")]


def test_fresh_session_plans_pass_1(auto, tmp_path):
    job = auto.plan(cfg(), SID, transcript(tmp_path, BASE), [])
    assert job.n == 1 and job.since is None
    assert job.last_ts == "2026-11-26T10:05:09Z"


def test_summarized_and_untouched_is_not_due(auto, tmp_path):
    path = transcript(tmp_path, BASE, age_s=3600)
    ok = {"status": "ok", "pass": 1, "watermark": "2026-11-26T10:05:09Z",
          "ts": "2026-11-26T10:10:00Z", "result": "infrastructure:session/2026-11-26/x"}
    # file mtime predates nothing new: the mtime gate stops it before any parse
    os.utime(path, (auto.parse_ts(ok["watermark"]).timestamp(),) * 2)
    assert auto.plan(cfg(), SID, path, [ok]) is None


def test_resumed_session_plans_continuation(auto, tmp_path):
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z", "and one more thing"),
                                        reply("2026-11-26T15:00:30Z")])
    ok = {"status": "ok", "pass": 1, "watermark": "2026-11-26T10:05:09Z",
          "result": "infrastructure:session/2026-11-26/x"}
    job = auto.plan(cfg(), SID, path, [ok])
    assert (job.n, job.since, job.prior) == (2, "2026-11-26T10:05:09Z", "infrastructure:session/2026-11-26/x")
    assert job.last_ts == "2026-11-26T15:00:30Z"


def test_resume_with_only_tool_noise_is_not_due(auto, tmp_path):
    # records after the watermark, but no real user prompt (meta / tool results)
    noise = {"type": "user", "isMeta": True, "timestamp": "2026-11-26T15:00:00Z",
             "message": {"content": "<command-name>/exit</command-name>"}}
    path = transcript(tmp_path, BASE + [noise])
    ok = {"status": "ok", "pass": 1, "watermark": "2026-11-26T10:05:09Z"}
    assert auto.plan(cfg(), SID, path, [ok]) is None


def test_legacy_ok_record_falls_back_to_ts(auto, tmp_path):
    # ok rows written before this change carry no watermark/pass: use their ts, pass 1
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z")])
    ok = {"status": "ok", "ts": "2026-11-26T14:28:22Z", "result": "duplicate raycast:session/2026-11-25/y"}
    job = auto.plan(cfg(), SID, path, [ok])
    assert (job.n, job.since, job.prior) == (2, "2026-11-26T14:28:22Z", "raycast:session/2026-11-25/y")


def test_third_pass_follows_latest_watermark(auto, tmp_path):
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z"), prompt("2026-11-26T18:00:00Z")])
    recs = [{"status": "ok", "pass": 1, "watermark": "2026-11-26T10:05:09Z", "result": "a:session/d/p1"},
            {"status": "ok", "pass": 2, "watermark": "2026-11-26T15:00:00Z", "result": "a:session/d/p2"}]
    job = auto.plan(cfg(), SID, path, recs)
    assert (job.n, job.since, job.prior) == (3, "2026-11-26T15:00:00Z", "a:session/d/p2")


def test_failed_continuation_attempts_are_capped_per_pass(auto, tmp_path):
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z")])
    recs = [{"status": "ok", "pass": 1, "watermark": "2026-11-26T10:05:09Z"},
            {"status": "failed", "pass": 1}, {"status": "failed", "pass": 2}]
    assert auto.plan(cfg(), SID, path, recs).n == 2      # one pass-2 failure: retry
    recs.append({"status": "failed", "pass": 2})
    assert auto.plan(cfg(), SID, path, recs) is None     # two: give up on pass 2


def test_short_session_skip_is_reconsidered_once_it_grows(auto, tmp_path):
    path = transcript(tmp_path, BASE[:2], age_s=60)
    assert auto.plan(cfg(), SID, path, []) is None       # 1 prompt: skip recorded
    recs = [json.loads(line) for line in auto.LEDGER.read_text().splitlines()]
    assert recs[-1]["reason"] == "prompts=1"
    assert auto.plan(cfg(), SID, path, recs) is None     # unchanged: no re-parse, no new row
    assert len(auto.LEDGER.read_text().splitlines()) == 1
    path = transcript(tmp_path, BASE)                    # resumed, now 2 prompts
    assert auto.plan(cfg(), SID, path, recs).n == 1


def test_hand_run_pass_in_kmcp_moves_the_watermark(auto, tmp_path, monkeypatch):
    # owner ran /session-summary mid-session (kmcp entry ends 10:05:09), then kept going
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z")])
    monkeypatch.setattr(auto, "existing_summaries",
                        lambda sid: [("infrastructure:session/2026-11-26/self", "2026-11-26T10:05:09Z")])
    job = auto.plan(cfg(), SID, path, [])
    assert job.n == 1
    job = auto.reconcile_with_kmcp(cfg(), job)
    assert (job.n, job.since, job.prior) == (2, "2026-11-26T10:05:09Z", "infrastructure:session/2026-11-26/self")
    rec = json.loads(auto.LEDGER.read_text().splitlines()[-1])
    assert rec["status"] == "ok" and rec["pass"] == 1 and rec["watermark"] == "2026-11-26T10:05:09Z"


def test_hand_run_pass_covering_everything_ends_it(auto, tmp_path, monkeypatch):
    path = transcript(tmp_path, BASE)
    monkeypatch.setattr(auto, "existing_summaries",
                        lambda sid: [("infrastructure:session/2026-11-26/self", "2026-11-26T10:05:09Z")])
    assert auto.reconcile_with_kmcp(cfg(), auto.plan(cfg(), SID, path, [])) is None


def test_own_earlier_pass_is_not_mistaken_for_a_hand_run(auto, tmp_path, monkeypatch):
    path = transcript(tmp_path, BASE + [prompt("2026-11-26T15:00:00Z")])
    auto.append_ledger({"session_id": SID, "status": "ok", "pass": 1,
                        "watermark": "2026-11-26T10:05:09Z", "result": "infrastructure:session/2026-11-26/p1"})
    # the worker wrote a slightly later ended_at than the watermark
    monkeypatch.setattr(auto, "existing_summaries",
                        lambda sid: [("infrastructure:session/2026-11-26/p1", "2026-11-26T10:06:00Z")])
    job = auto.plan(cfg(), SID, path, auto.read_ledger()[SID])
    assert auto.reconcile_with_kmcp(cfg(), job) == job


def test_delta_prompt_formats(auto):
    text = auto.DELTA_PROMPT.format(sid=SID, digest="/tmp/d.txt", csd="csd", project_path="/repo",
                                    n=2, since="2026-11-26T10:05:09Z", prior="a:session/d/p1")
    assert "Session (cont. 2)" in text and "delta-capture" in text and "--pass 2" in text
    full = auto.FULL_PROMPT.format(sid=SID, digest="/tmp/d.txt", csd="csd", project_path="/repo",
                                   pass_line="Pass 2: delta since X, prior Y")
    assert "Pass 2: delta since X" in full
