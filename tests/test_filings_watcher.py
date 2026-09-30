"""Watcher main loop: CIK-bound state, seeding, backlog valve, FORCE mode,
extraction toggle, User-Agent, time-stop."""

import datetime as dt
import json
from pathlib import Path

import pytest
import requests

from filings import watcher

REAL_CONFIG = json.loads((Path(__file__).parent / "fixtures" / "omc_filings_config.json").read_text())
CIK = "0000029989"
OTHER_CIK = "0000000042"


def f(accession, form="8-K", filing_date="2026-07-28", items="", report_date=None):
    return {"form": form, "accessionNumber": accession, "filingDate": filing_date,
            "reportDate": report_date if report_date is not None else filing_date,
            "items": items, "primaryDocument": f"{accession}.htm"}


OLD = [f("0000029989-26-000001", "10-Q", "2026-04-30"),
       f("0000029989-26-000002", "8-K", "2026-04-28", "2.02,9.01")]


class Env:
    def __init__(self, monkeypatch, tmp_path):
        self.monkeypatch = monkeypatch
        self.tmp = tmp_path
        self.emails = []
        self.pipeline_calls = []
        self.fetch_calls = []
        self.filings = list(OLD)
        self.fiscal_year_end = "1231"
        self.cfg = json.loads(json.dumps(REAL_CONFIG))
        self.baselines = {}
        monkeypatch.setattr(watcher, "CONFIG_PATH", tmp_path / "filings_config.json")
        monkeypatch.setattr(watcher, "BASELINES_PATH", tmp_path / "baselines.json")
        monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(watcher, "require_env", lambda name: f"{name.lower()}@example.com")
        monkeypatch.setattr(watcher, "send_email", self._send)
        monkeypatch.setattr(watcher, "run_extraction_pipeline", self._pipeline)
        monkeypatch.setattr(watcher, "fetch_recent_filings", self._fetch)
        monkeypatch.delenv("FORCE_ACCESSION", raising=False)

    def _send(self, subject, body, severity=None):
        self.emails.append({"subject": subject, "body": body, "severity": severity})

    def _pipeline(self, filing, cfg, baselines, contact_email):
        self.pipeline_calls.append({"filing": filing, "cfg": cfg})

    def _fetch(self, cik, session):
        self.fetch_calls.append({"cik": cik, "session": session})
        return list(self.filings), self.fiscal_year_end

    @property
    def state_path(self) -> Path:
        return self.tmp / "state.json"

    def write_state(self, state) -> None:
        self.state_path.write_text(json.dumps(state))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def run(self) -> int:
        (self.tmp / "filings_config.json").write_text(json.dumps(self.cfg))
        if self.baselines is not None:
            (self.tmp / "baselines.json").write_text(json.dumps(self.baselines))
        return watcher.main()


@pytest.fixture
def env(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    return Env(monkeypatch, tmp_path)


def seeded_state(accessions, cik=CIK):
    state = {"flags": {}, "seeded": True, "seen_accessions": sorted(accessions)}
    if cik:
        state["cik"] = cik
    return state


# ---------------------------------------------------------------------------
# Seeding & CIK binding
# ---------------------------------------------------------------------------

def test_first_run_seeds_silently_and_binds_cik(env):
    assert env.run() == 0
    assert env.emails == []
    state = env.state()
    assert state["seeded"] is True
    assert state["cik"] == CIK
    assert state["seen_accessions"] == sorted(x["accessionNumber"] for x in OLD)


def test_shipped_state_file_seeds(env):
    env.state_path.write_text(
        Path(watcher.__file__).with_name("state.json").read_text())
    assert env.run() == 0
    assert env.emails == []
    assert env.state()["cik"] == CIK


def test_cik_change_reseeds_silently(env, capsys):
    env.write_state(seeded_state(["0000000042-25-000001"], cik=OTHER_CIK))
    env.filings = OLD + [f("0000029989-26-000003", "8-K", "2026-07-28", "2.02")]
    assert env.run() == 0
    assert env.emails == [] and env.pipeline_calls == []
    out = capsys.readouterr().out
    assert (f"WARNING: state.json belongs to CIK {OTHER_CIK} but config watches "
            f"CIK {CIK}; re-seeding silently.") in out
    state = env.state()
    assert state["cik"] == CIK
    assert state["seen_accessions"] == sorted(x["accessionNumber"] for x in env.filings)


def test_legacy_seeded_state_adopts_cik_without_reseeding(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD], cik=None))
    new = f("0000029989-26-000003", "8-K", "2026-07-28", "5.07")
    env.filings = OLD + [new]
    assert env.run() == 0
    assert [e["severity"] for e in env.emails] == ["info"]  # processed, not re-seeded
    state = env.state()
    assert state["cik"] == CIK
    assert new["accessionNumber"] in state["seen_accessions"]


def test_legacy_state_binding_is_saved_even_without_new_filings(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD], cik=None))
    assert env.run() == 0
    assert env.state()["cik"] == CIK


def test_cik_is_normalized(env):
    env.cfg["company"]["cik"] = 29989
    assert env.run() == 0
    assert env.fetch_calls[0]["cik"] == CIK
    assert env.state()["cik"] == CIK


def test_short_cik_in_state_matches_after_normalization(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD], cik="29989"))
    env.filings = OLD + [f("0000029989-26-000003", "8-K", "2026-07-28", "5.07")]
    assert env.run() == 0
    assert len(env.emails) == 1  # same company: no re-seed


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------

def test_new_filings_processed_oldest_first_and_persisted(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    later = f("0000029989-26-000005", "8-K", "2026-07-29", "8.01")
    earlier = f("0000029989-26-000004", "8-K", "2026-07-20", "8.01")
    unwatched = f("0000029989-26-000006", "4", "2026-07-21")
    env.filings = OLD + [later, earlier, unwatched]
    assert env.run() == 0
    assert [e["subject"] for e in env.emails] == ["OMC filing: 8-K (2026-07-20)",
                                                  "OMC filing: 8-K (2026-07-29)"]
    seen = env.state()["seen_accessions"]
    assert earlier["accessionNumber"] in seen and later["accessionNumber"] in seen
    assert unwatched["accessionNumber"] not in seen


def test_failed_filing_left_unseen_for_retry(env, monkeypatch):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    bad = f("0000029989-26-000007", "8-K", "2026-07-20", "8.01")
    env.filings = OLD + [bad]

    def smtp_down(subject, body, severity=None):
        raise OSError("SMTP down")
    monkeypatch.setattr(watcher, "send_email", smtp_down)
    assert env.run() == 0
    assert bad["accessionNumber"] not in env.state()["seen_accessions"]


def test_backlog_valve_refuses_more_than_limit(env, capsys):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    backlog = [f(f"0000029989-25-{i:06d}", "8-K", "2025-06-01", "8.01")
               for i in range(watcher.MAX_NEW_PER_RUN + 1)]
    env.filings = OLD + backlog
    before = env.state_path.read_text()
    assert env.run() == 1
    assert env.emails == [] and env.pipeline_calls == []
    assert env.state_path.read_text() == before
    err = capsys.readouterr().err
    assert "FATAL" in err and "filings/state.json" in err and "re-seed" in err


def test_backlog_valve_allows_exactly_the_limit(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + [f(f"0000029989-25-{i:06d}", "8-K", "2025-06-01", "8.01")
                         for i in range(watcher.MAX_NEW_PER_RUN)]
    assert env.run() == 0
    assert len(env.emails) == watcher.MAX_NEW_PER_RUN


# ---------------------------------------------------------------------------
# Extraction toggle
# ---------------------------------------------------------------------------

def _earnings_and_10q():
    return [f("0000029989-26-000010", "8-K", "2026-07-21", "2.02,9.01"),
            f("0000029989-26-000011", "10-Q", "2026-07-30", report_date="2026-06-30")]


def test_extraction_enabled_notifies_and_runs_pipeline(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + _earnings_and_10q()
    assert env.run() == 0
    assert len(env.pipeline_calls) == 2
    assert all("a review draft email follows" in e["body"] for e in env.emails)
    # Fiscal year-end month is resolved and handed to extraction on cfg.
    assert env.pipeline_calls[0]["cfg"]["_fye_month"] == 12


def test_extraction_disabled_sends_notification_only(env):
    env.cfg["extraction"]["enabled"] = False
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + _earnings_and_10q()
    assert env.run() == 0
    assert env.pipeline_calls == []
    bodies = [e["body"] for e in env.emails]
    assert len(bodies) == 2
    assert bodies[0].endswith("Earnings 8-K (LLM extraction disabled in filings_config.json).")
    assert bodies[1].endswith("Periodic report (LLM extraction disabled in "
                              "filings_config.json).")
    assert not any("review draft" in b for b in bodies)


@pytest.mark.parametrize("extraction_cfg, enabled", [
    (None, True), ({}, True), ({"enabled": None}, True), ({"enabled": True}, True),
    ({"enabled": False}, False),
])
def test_extraction_enabled_defaults(extraction_cfg, enabled):
    cfg = {} if extraction_cfg is None else {"extraction": extraction_cfg}
    assert watcher.extraction_enabled(cfg) is enabled


def test_item_206_red_alert_still_first(env):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + [f("0000029989-26-000012", "8-K", "2026-07-21", "2.02,2.06")]
    assert env.run() == 0
    assert [e["severity"] for e in env.emails] == ["red", "info"]
    assert len(env.pipeline_calls) == 1


@pytest.mark.parametrize("form, items, severities, extracted", [
    # Documented in README ("What it does" / "Emails you can receive" /
    # "Limitations") and CLAUDE.md: a 2.06-only 8-K gets just the red alert,
    # with no notification and no extraction; an 8-K listing neither 2.02 nor
    # 2.06 gets a plain notification.
    ("8-K", "2.06", ["red"], False),
    ("8-K/A", "2.06,9.01", ["red"], False),
    ("8-K", "2.02,2.06", ["red", "info"], True),
    ("8-K", "5.02", ["info"], False),
    ("8-K", "5.02,9.01", ["info"], False),
    ("8-K", "", ["info"], False),
])
def test_8k_item_routing(env, form, items, severities, extracted):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + [f("0000029989-26-000013", form, "2026-07-21", items)]
    assert env.run() == 0
    assert [e["severity"] for e in env.emails] == severities
    assert bool(env.pipeline_calls) is extracted
    if "info" in severities and not extracted:
        info = next(e for e in env.emails if e["severity"] == "info")
        assert info["subject"] == f"OMC filing: {form} (2026-07-21)"
        assert "review draft" not in info["body"]


@pytest.mark.parametrize("ticker", ["absent", None, ""])
def test_subjects_fall_back_to_company_name_without_ticker(env, ticker):
    if ticker == "absent":
        del env.cfg["company"]["ticker"]
    else:
        env.cfg["company"]["ticker"] = ticker
    env.cfg["company"]["name"] = "Example Corp"
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + [f("0000029989-26-000014", "8-K", "2026-07-28", "8.01"),
                         f("0000029989-26-000015", "8-K", "2026-07-29", "2.06")]
    assert env.run() == 0
    assert [(e["severity"], e["subject"]) for e in env.emails] == [
        ("info", "Example Corp filing: 8-K (2026-07-28)"),
        ("red", "Goodwill impairment 8-K filed — Example Corp (Item 2.06)"),
    ]
    assert not any("None" in e["subject"] for e in env.emails)


def test_fiscal_year_end_from_submissions_when_not_configured(env):
    del env.cfg["company"]["fiscal_year_end"]
    env.fiscal_year_end = "0131"
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + _earnings_and_10q()
    assert env.run() == 0
    assert env.pipeline_calls[0]["cfg"]["_fye_month"] == 1


# ---------------------------------------------------------------------------
# FORCE mode
# ---------------------------------------------------------------------------

def test_force_with_empty_state_does_not_key_error(env, monkeypatch):
    env.write_state({})
    target = OLD[1]["accessionNumber"]
    monkeypatch.setenv("FORCE_ACCESSION", target.replace("-", ""))
    assert env.run() == 0
    assert len(env.pipeline_calls) == 1
    assert env.state()["seen_accessions"] == [target]


def test_force_ignores_seen_state(env, monkeypatch):
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    monkeypatch.setenv("FORCE_ACCESSION", OLD[0]["accessionNumber"])
    assert env.run() == 0
    assert len(env.pipeline_calls) == 1


@pytest.mark.parametrize("bad", ["abc", "0000029989-26-00001", "0000029989-26-000001; rm",
                                 "000002998926000001x", "0000029989--26-000001"])
def test_force_rejects_malformed_accession(env, monkeypatch, capsys, bad):
    monkeypatch.setenv("FORCE_ACCESSION", bad)
    assert env.run() == 1
    assert env.fetch_calls == [] and env.emails == []
    assert "FATAL" in capsys.readouterr().err


def test_force_unknown_accession_returns_1(env, monkeypatch):
    monkeypatch.setenv("FORCE_ACCESSION", "0000029989-26-999999")
    assert env.run() == 1
    assert env.emails == []


# ---------------------------------------------------------------------------
# Config handling, User-Agent
# ---------------------------------------------------------------------------

def test_user_agent_uses_configured_tool_name(env):
    env.cfg["user_agent_tool_name"] = "AcmeFilingBot"
    assert env.run() == 0
    session = env.fetch_calls[0]["session"]
    assert session.headers["User-Agent"] == "AcmeFilingBot sec_contact_email@example.com"


def test_user_agent_default_tool_name(env):
    del env.cfg["user_agent_tool_name"]
    assert env.run() == 0
    assert env.fetch_calls[0]["session"].headers["User-Agent"] == \
        "StockMonitor sec_contact_email@example.com"


def test_missing_baselines_file_is_fine(env):
    env.baselines = None
    assert env.run() == 0


def test_empty_baselines_object_is_fine(env):
    env.baselines = {}
    assert env.run() == 0


def test_missing_config_is_fatal(env, capsys):
    assert watcher.main() == 1
    assert "FATAL" in capsys.readouterr().err


def test_config_without_cik_is_fatal(env, capsys):
    del env.cfg["company"]["cik"]
    assert env.run() == 1
    assert "company.cik" in capsys.readouterr().err


def test_watched_forms_default_when_absent(env):
    del env.cfg["watched_forms"]
    env.write_state(seeded_state([x["accessionNumber"] for x in OLD]))
    env.filings = OLD + [f("0000029989-26-000013", "10-K/A", "2026-07-21")]
    env.cfg["extraction"]["enabled"] = False
    assert env.run() == 0
    assert len(env.emails) == 1


# ---------------------------------------------------------------------------
# Submissions parsing
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data):
        self.data = data
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return _FakeResp(self.data)


def test_fetch_recent_filings_tolerates_short_arrays():
    data = {"fiscalYearEnd": "1231", "filings": {"recent": {
        "accessionNumber": ["a1", "a2", "a3"],
        "form": ["8-K", "10-Q"],
        "filingDate": ["2026-07-28", "2026-07-30", None],
        "items": None,
    }}}
    session = _FakeSession(data)
    filings, fye = watcher.fetch_recent_filings("29989", session)
    assert session.urls == ["https://data.sec.gov/submissions/CIK0000029989.json"]
    assert fye == "1231"
    assert len(filings) == 3
    assert filings[2] == {"form": "", "accessionNumber": "a3", "filingDate": "",
                          "reportDate": "", "items": "", "primaryDocument": ""}
    assert filings[0]["form"] == "8-K" and filings[0]["items"] == ""


def test_fetch_recent_filings_without_fiscal_year_end():
    filings, fye = watcher.fetch_recent_filings("1", _FakeSession({}))
    assert filings == [] and fye is None


# ---------------------------------------------------------------------------
# Time-stop
# ---------------------------------------------------------------------------

def test_time_stop_skipped_when_not_configured(env):
    state = {"flags": {}}
    assert watcher.check_time_stop(state, {}) is False
    assert watcher.check_time_stop(state, {"time_stop": None}) is False
    assert watcher.check_time_stop(state, {"time_stop": {"review_after": None,
                                                         "note": None}}) is False
    assert env.emails == []


def test_time_stop_sends_once_and_note_only_if_present(env):
    past = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    state = {"flags": {}}
    assert watcher.check_time_stop(state, {"time_stop": {"review_after": past}}) is True
    assert "Stored note" not in env.emails[0]["body"]
    assert env.emails[0]["severity"] == "clock"
    assert watcher.check_time_stop(state, {"time_stop": {"review_after": past}}) is False

    state = {"flags": {}}
    watcher.check_time_stop(state, {"time_stop": {"review_after": past,
                                                  "note": "Re-check the position."}})
    assert env.emails[-1]["body"].endswith("Stored note:\nRe-check the position.")
