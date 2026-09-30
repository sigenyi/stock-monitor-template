"""run_extraction_pipeline: stage guards and history side effects."""

import json
from pathlib import Path

import pytest
import requests

from filings import extraction as ex

CONFIG_FILE = Path(__file__).parent / "fixtures" / "omc_filings_config.json"
CIK = "0000029989"


@pytest.fixture
def omc_cfg():
    cfg = json.loads(CONFIG_FILE.read_text())
    cfg["_fye_month"] = 12
    return cfg


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Offline pipeline: fake document retrieval + Claude, captured email."""
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(ex.requests, "get", blocked)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(ex, "HISTORY_PATH", tmp_path / "history.json")

    captured = {"emails": [], "sessions": [], "anchors": [], "claude": []}

    def fake_prepare(filing, cik, session, phrases=None):
        captured["sessions"].append(session)
        captured["anchors"].append(phrases)
        return "document text"

    def fake_claude(text, form, cfg, keys):
        captured["claude"].append(keys)
        return ex.parse_extraction_json(json.dumps({
            "leverage_ratio_x": {"found": True, "value": "3.4x", "quote": "3.4x"},
            "adj_margin_pct": {"found": True, "value": 15.9},
        }), keys)

    def fake_send(subject, body, severity=None):
        captured["emails"].append({"subject": subject, "body": body,
                                   "severity": severity})

    monkeypatch.setattr(ex, "prepare_document_text", fake_prepare)
    monkeypatch.setattr(ex, "call_claude", fake_claude)
    monkeypatch.setattr(ex, "send_email", fake_send)
    return captured


def ten_q(**overrides):
    f = {"form": "10-Q", "accessionNumber": "0000029989-26-000020",
         "filingDate": "2026-07-30", "reportDate": "2026-06-30",
         "primaryDocument": "omc-20260630.htm"}
    f.update(overrides)
    return f


def test_happy_path_writes_namespaced_history_and_sends_review(env, omc_cfg):
    ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "contact@example.com")
    (email,) = env["emails"]
    assert email["subject"] == "OMC 2026 Q2 review draft — 1 red trigger(s)"
    assert email["severity"] == "red"
    history = json.loads(ex.HISTORY_PATH.read_text())
    assert list(history) == [CIK]
    fields = history[CIK]["2026-Q2"]["fields"]
    assert fields["leverage_ratio_x"]["value"] == 3.4
    assert fields["leverage_ratio_x"]["source_form"] == "10-Q"
    # Profile drives anchors, keys and the SEC User-Agent.
    assert set(env["anchors"][0]) == set(ex.anchor_phrases(omc_cfg["profile"]))
    assert env["claude"][0] == ex.SCHEMA_KEYS
    assert env["sessions"][0].headers["User-Agent"] == "StockMonitor contact@example.com"


def test_pipeline_uses_fiscal_year_end_from_cfg(env, omc_cfg):
    omc_cfg["_fye_month"] = 1
    ex.run_extraction_pipeline(ten_q(reportDate="2026-05-02"), omc_cfg, {}, "c@example.com")
    history = json.loads(ex.HISTORY_PATH.read_text())
    assert list(history[CIK]) == ["2027-Q1"]


def test_pipeline_normalizes_cik_and_keeps_other_companies(env, omc_cfg):
    ex.HISTORY_PATH.write_text(json.dumps({"0000000001": {"2026-Q1": {"x": 1}}}))
    omc_cfg["company"]["cik"] = "29989"
    ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "c@example.com")
    history = json.loads(ex.HISTORY_PATH.read_text())
    assert set(history) == {"0000000001", CIK}
    assert history["0000000001"] == {"2026-Q1": {"x": 1}}


def test_stage1_failure_sends_extraction_failed_email(env, omc_cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("model declined the request (stop_reason=refusal)")
    monkeypatch.setattr(ex, "call_claude", boom)
    ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "c@example.com")
    (email,) = env["emails"]
    assert email["subject"] == "OMC 10-Q: extraction failed — review the filing manually"
    assert email["severity"] == "yellow"
    assert "stop_reason=refusal" in email["body"]
    assert "force_accession = 0000029989-26-000020" in email["body"]
    assert not ex.HISTORY_PATH.exists()


def test_stage2_failure_sends_review_failed_email_and_does_not_raise(env, omc_cfg):
    bad = ten_q(reportDate="", filingDate="")  # quarter cannot be derived
    ex.run_extraction_pipeline(bad, omc_cfg, {}, "c@example.com")
    (email,) = env["emails"]
    assert email["subject"] == "OMC 10-Q: review step failed — review the filing manually"
    assert email["severity"] == "yellow"
    assert "neither a reportDate nor a filingDate" in email["body"]
    assert ("Filing index: https://www.sec.gov/Archives/edgar/data/29989/"
            "000002998926000020/") in email["body"]
    assert "force_accession = 0000029989-26-000020" in email["body"]


def test_stage2_failure_in_rules_is_caught(env, omc_cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("unexpected")
    monkeypatch.setattr(ex, "run_rules", boom)
    ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "c@example.com")
    (email,) = env["emails"]
    assert "review step failed" in email["subject"]
    assert "unexpected" in email["body"]


def test_stage3_smtp_failure_propagates(env, omc_cfg, monkeypatch):
    def smtp_down(subject, body, severity=None):
        raise OSError("SMTP down")
    monkeypatch.setattr(ex, "send_email", smtp_down)
    with pytest.raises(OSError, match="SMTP down"):
        ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "c@example.com")


def test_failure_email_smtp_failure_propagates(env, omc_cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("EDGAR down")

    def smtp_down(subject, body, severity=None):
        raise OSError("SMTP down")
    monkeypatch.setattr(ex, "call_claude", boom)
    monkeypatch.setattr(ex, "send_email", smtp_down)
    with pytest.raises(OSError, match="SMTP down"):
        ex.run_extraction_pipeline(ten_q(), omc_cfg, {}, "c@example.com")


def test_pipeline_with_minimal_config(env):
    cfg = {"company": {"cik": "123", "name": "Example Corp"}}
    ex.run_extraction_pipeline(ten_q(), cfg, {}, "c@example.com")
    (email,) = env["emails"]
    # No ticker -> the company name labels the email; no thresholds -> no
    # numeric rules fire.
    assert email["subject"] == "Example Corp 2026 Q2 review draft — no triggers"
    assert env["claude"][0] == ex.schema_keys({})
    assert env["anchors"][0] == ex.BASE_ANCHOR_PHRASES
    assert "0000000123" in json.loads(ex.HISTORY_PATH.read_text())


def test_sec_session_user_agent():
    assert ex.sec_session("c@example.com", "MyTool").headers["User-Agent"] == \
        "MyTool c@example.com"
    assert ex.sec_session("c@example.com").headers["User-Agent"] == \
        "StockMonitor c@example.com"
    assert ex.user_agent_tool_name({"user_agent_tool_name": ""}) == "StockMonitor"
