"""History (CIK-namespaced, merge ordering), rules engine, plain-English
layer and the review email."""

import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest
import requests

from filings import extraction as ex

CONFIG_FILE = Path(__file__).parent / "fixtures" / "omc_filings_config.json"
CIK = "0000029989"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(ex.requests, "get", blocked)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(ex, "HISTORY_PATH", tmp_path / "history.json")


@pytest.fixture
def omc_cfg():
    return json.loads(CONFIG_FILE.read_text())


def entry(value, quote=None, found=True):
    return {"found": found, "value": value, "quote": quote, "location": None}


def extraction(**values):
    data = {k: entry(None, found=False) for k in ex.SCHEMA_KEYS}
    for key, value in values.items():
        data[key] = entry(value, quote=f"quote for {key}")
    return data


def filing(form, filing_date, accession="0000029989-26-000001", report_date=None):
    return {"form": form, "filingDate": filing_date, "accessionNumber": accession,
            "reportDate": report_date or filing_date, "primaryDocument": "doc.htm"}


def record_with(**values):
    records = {}
    ex.merge_into_history(records, "2026-Q2", extraction(**values),
                          filing("10-Q", "2026-07-30"))
    return records["2026-Q2"], records


# ---------------------------------------------------------------------------
# History file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [None, "", "   \n  "])
def test_load_history_tolerates_missing_or_blank(content):
    if content is not None:
        ex.HISTORY_PATH.write_text(content)
    assert ex.load_history() == {}


def test_history_is_namespaced_by_cik_and_round_trips():
    history = ex.load_history()
    records = ex.company_history(history, CIK)
    assert history == {CIK: {}}
    ex.merge_into_history(records, "2026-Q2", extraction(leverage_ratio_x=2.5),
                          filing("10-Q", "2026-07-30"))
    ex.company_history(history, "0000000001")  # another company stays separate
    ex.save_history(history)
    text = ex.HISTORY_PATH.read_text()
    assert text.endswith("}\n")
    assert text == json.dumps(history, indent=2, sort_keys=True) + "\n"
    loaded = ex.load_history()
    assert set(loaded) == {CIK, "0000000001"}
    assert ex.field_value(loaded[CIK]["2026-Q2"], "leverage_ratio_x") == 2.5


# ---------------------------------------------------------------------------
# Merge ordering
# ---------------------------------------------------------------------------

def test_older_8k_does_not_overwrite_newer_10q(capsys):
    records = {}
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.5),
                          filing("10-Q", "2026-07-30", "A-10Q"))
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.1),
                          filing("8-K", "2026-07-21", "A-8K"))
    field = records["2026-Q2"]["fields"]["adj_margin_pct"]
    assert field["value"] == 15.5
    assert field["accession"] == "A-10Q"
    assert "History merge: keeping newer" in capsys.readouterr().out
    assert len(records["2026-Q2"]["sources"]) == 2


def test_newer_10q_overwrites_older_8k():
    records = {}
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.1),
                          filing("8-K", "2026-07-21", "A-8K"))
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.5),
                          filing("10-Q", "2026-07-30", "A-10Q"))
    assert records["2026-Q2"]["fields"]["adj_margin_pct"]["value"] == 15.5


@pytest.mark.parametrize("order", [("8-K", "10-Q"), ("10-Q", "8-K")])
def test_equal_filing_date_10q_wins(order):
    records = {}
    values = {"8-K": 15.1, "10-Q": 15.5}
    for form in order:
        ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=values[form]),
                              filing(form, "2026-07-28", f"A-{form}"))
    assert records["2026-Q2"]["fields"]["adj_margin_pct"]["value"] == 15.5


def test_found_false_only_fills_empty_slots_and_sources_dedupe():
    records = {}
    ten_q = filing("10-Q", "2026-07-30", "A-10Q")
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.5), ten_q)
    ex.merge_into_history(records, "2026-Q2", extraction(), filing("8-K/A", "2026-08-15", "A-8KA"))
    ex.merge_into_history(records, "2026-Q2", extraction(adj_margin_pct=15.5), ten_q)
    rec = records["2026-Q2"]
    assert rec["fields"]["adj_margin_pct"]["value"] == 15.5
    assert rec["fields"]["book_equity_b"]["found"] is False
    assert [s["accession"] for s in rec["sources"]] == ["A-10Q", "A-8KA"]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

TRIGGER_ALL = dict(
    organic_growth_core_pct=2.0,
    total_reported_revenue_change_pct=-3.0,
    perimeter_changed=True,
    adj_margin_pct=14.0,
    segment_organic_pct=1.0,
    group_organic_pct=2.5,
    buyback_dollars_qtr_m=500.0,
    buyback_suspension_language=True,
    leverage_ratio_x=3.4,
    goodwill_impairment_charge={"impaired": True, "amount_m": 250.0},
    goodwill_cushion_pct=8.0,
    guidance_language={"softened": True, "assessment": "Lowered."},
)


def _history_for_trigger_all():
    """Previous quarters that make the streak/segment/cushion rules fire."""
    records = {}
    for quarter, date in [("2025-Q2", "2025-07-30"), ("2025-Q3", "2025-10-30"),
                          ("2025-Q4", "2026-02-10"), ("2026-Q1", "2026-04-30")]:
        ex.merge_into_history(records, quarter, extraction(
            organic_growth_core_pct=1.0, total_reported_revenue_change_pct=-1.0,
            segment_organic_pct=0.5, group_organic_pct=1.5,
            goodwill_cushion_pct=12.0 if quarter == "2025-Q4" else None),
            filing("10-Q", date, f"A-{quarter}"))
    ex.merge_into_history(records, "2026-Q2", extraction(**TRIGGER_ALL),
                          filing("10-Q", "2026-07-30", "A-2026-Q2"))
    return records


def ids(fired):
    return [x["id"] for x in fired]


def test_all_rules_fire_with_thresholds_and_every_id_has_plain(omc_cfg):
    records = _history_for_trigger_all()
    fired, notes = ex.run_rules("2026-Q2", records["2026-Q2"], records, {}, omc_cfg,
                                "10-Q", dt.date(2026, 6, 30))
    assert ids(fired) == ["buyback_suspended", "buyback_below_floor", "leverage_high",
                          "goodwill_impairment", "segment_lag", "guidance_softened",
                          "margin_floor", "goodwill_cushion", "perimeter_changed",
                          "subtraction_growth"]
    for x in fired:
        assert set(x) == {"id", "severity", "rule", "detail", "quote"}
        assert x["id"] in ex.PLAIN
    by_id = {x["id"]: x for x in fired}
    assert by_id["segment_lag"]["rule"] == (
        "Integrated Media lagging group organic growth for two consecutive quarters")
    assert by_id["margin_floor"]["rule"] == "Adjusted EBITA margin below floor"
    assert by_id["perimeter_changed"]["rule"] == "Core Operations perimeter changed"
    assert by_id["buyback_below_floor"]["detail"] == (
        "Quarterly repurchases $500.0M < red floor $700M.")
    assert by_id["leverage_high"]["detail"] == "Leverage 3.4x > 3.0x."
    assert by_id["subtraction_growth"]["severity"] == "yellow"
    assert "5 consecutive quarter(s)" in by_id["subtraction_growth"]["detail"]
    assert any(n.startswith("Standing note: this 10-Q covers June 30") for n in notes)


def test_numeric_rules_skipped_without_thresholds(omc_cfg):
    omc_cfg["thresholds"] = {"leverage_red_x": None}
    records = _history_for_trigger_all()
    fired, notes = ex.run_rules("2026-Q2", records["2026-Q2"], records, {}, omc_cfg,
                                "10-Q", dt.date(2026, 6, 30))
    assert set(ids(fired)) == {"buyback_suspended", "goodwill_impairment", "segment_lag",
                               "guidance_softened", "goodwill_cushion",
                               "perimeter_changed"}
    streak = [n for n in notes if n.startswith("Subtraction-growth streak")]
    assert streak and "escalates" not in streak[0]
    del omc_cfg["thresholds"]
    fired2, _ = ex.run_rules("2026-Q2", records["2026-Q2"], records, {}, omc_cfg,
                             "10-Q", dt.date(2026, 6, 30))
    assert ids(fired2) == ids(fired)


def test_out_of_range_integer_threshold_is_skipped_not_fatal(omc_cfg, capsys):
    # A JSON integer beyond float range (json.loads keeps it as an int) must
    # skip that rule with a warning, not raise OverflowError in stage 2.
    omc_cfg["thresholds"]["leverage_red_x"] = 10**400
    omc_cfg["thresholds"]["margin_yellow_pct"] = 10**400
    omc_cfg["profile"]["segment"]["share_of_core_pct"] = 10**400
    records = _history_for_trigger_all()
    fired, _ = ex.run_rules("2026-Q2", records["2026-Q2"], records, {}, omc_cfg,
                            "10-Q", dt.date(2026, 6, 30))
    assert "leverage_high" not in ids(fired)
    assert "margin_floor" not in ids(fired)
    assert "buyback_below_floor" in ids(fired)  # other thresholds unaffected
    assert "WARNING: thresholds.leverage_red_x" in capsys.readouterr().out
    _, meaning = ex.plain_text("segment_lag", omc_cfg)
    assert "Integrated Media" in meaning and "% of" not in meaning


def test_segment_rule_skipped_without_segment(omc_cfg):
    del omc_cfg["profile"]["segment"]
    records = _history_for_trigger_all()
    fired, _ = ex.run_rules("2026-Q2", records["2026-Q2"], records, {}, omc_cfg,
                            "10-Q", dt.date(2026, 6, 30))
    assert "segment_lag" not in ids(fired)


def test_generic_rule_names_without_profile():
    cfg = {"company": {"cik": CIK, "name": "Example Corp"},
           "thresholds": {"margin_yellow_pct": 20}}
    rec, records = record_with(adj_margin_pct=10.0, perimeter_changed=True)
    fired, _ = ex.run_rules("2026-Q2", rec, records, {}, cfg, "10-Q", dt.date(2026, 6, 30))
    rules = {x["id"]: x["rule"] for x in fired}
    assert rules == {"margin_floor": "Adjusted operating margin below floor",
                     "perimeter_changed": "Organic-growth perimeter changed"}


def test_two_negative_quarters_uses_baselines_history(omc_cfg):
    rec, records = record_with(organic_growth_core_pct=-1.5)
    baselines = {"organic_growth_history": [{"quarter": "2026-Q1", "organic_core_pct": -0.4}]}
    fired, _ = ex.run_rules("2026-Q2", rec, records, baselines, omc_cfg, "8-K",
                            dt.date(2026, 6, 30))
    assert ids(fired) == ["two_neg_organic"]
    assert fired[0]["detail"] == "2026-Q2: -1.5% ; 2026-Q1: -0.4% (from baselines)"

    fired, notes = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, "8-K",
                                dt.date(2026, 6, 30))
    assert fired == []
    assert any("no 2026-Q1 figure is stored" in n for n in notes)


def test_buyback_detail_includes_run_rate_only_when_known(omc_cfg):
    rec, records = record_with(buyback_dollars_qtr_m=600)
    fired, _ = ex.run_rules("2026-Q2", rec, records,
                            {"buyback": {"run_rate_qtr_m": 875}}, omc_cfg, "10-Q", None)
    assert fired[0]["detail"] == "Quarterly repurchases $600M < red floor $700M (run-rate ~$875M)."
    fired, _ = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, "10-Q", None)
    assert fired[0]["detail"] == "Quarterly repurchases $600M < red floor $700M."


def test_subtraction_note_not_evaluable_and_counting(omc_cfg):
    rec, records = record_with(organic_growth_core_pct=2.0)
    _, notes = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, "8-K", None)
    assert ("Subtraction-growth streak: not evaluable this quarter (organic growth "
            "or total reported revenue change not reported).") in notes
    rec, records = record_with(organic_growth_core_pct=2.0,
                               total_reported_revenue_change_pct=-1.0)
    _, notes = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, "8-K", None)
    assert any(n.startswith("Subtraction-growth streak: 1 consecutive quarter(s)")
               and "(escalates to a yellow flag at 4)" in n for n in notes)


def test_informational_notes(omc_cfg):
    rec, records = record_with(shares_outstanding_m=280.0)
    baselines = {"shares_outstanding_m": 285.01, "baseline_asof": "2026-07-24",
                 "buyback": {"authorization_remaining_b": 5.0}}
    _, notes = ex.run_rules("2026-Q2", rec, records, baselines, omc_cfg, "8-K", None)
    assert "Share count: 280.0M vs 285.01M at baseline (-1.8%)." in notes
    assert ("Remaining buyback authorization: not stated in this filing (last known: "
            "$5.0B as of 2026-07-24).") in notes
    assert any(n.startswith("Market-value-vs-book gauge (impairment pressure): skipped")
               for n in notes)


@pytest.mark.parametrize("form, pe, test_date, expected", [
    ("10-Q", dt.date(2026, 6, 30), "06-30", "covers June 30"),
    ("10-Q", dt.date(2026, 3, 31), "06-30", None),
    ("10-Q", dt.date(2026, 9, 30), "06-30", None),
    ("8-K", dt.date(2026, 6, 30), "06-30", None),
    ("10-K", dt.date(2026, 12, 31), "10-01", "covers October 1"),
    ("10-Q/A", dt.date(2026, 4, 30), "02-15", "covers February 15"),  # Jan FYE Q1
    ("10-Q", dt.date(2026, 6, 30), None, None),
    ("10-Q", dt.date(2026, 6, 30), "June 30", None),
])
def test_goodwill_standing_note(omc_cfg, form, pe, test_date, expected):
    omc_cfg["profile"]["goodwill_test_date"] = test_date
    rec, records = record_with()
    _, notes = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, form, pe)
    standing = [n for n in notes if n.startswith("Standing note")]
    if expected is None:
        assert standing == []
    else:
        assert standing == [
            f"Standing note: this {form} {expected} — the company's annual goodwill "
            f"impairment test date. Early signs of a write-down usually show up in "
            f"this filing's goodwill footnote first."]


def test_market_cap_note_uses_ticker_argument(monkeypatch):
    rec, _ = record_with(shares_outstanding_m=200.0, book_equity_b=10.0)
    assert "missing a live price" in ex.market_cap_to_book_note(rec, "OMC")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key-not-real")
    assert "missing the ticker" in ex.market_cap_to_book_note(rec, "")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"c": 75.0}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return Resp()

    monkeypatch.setattr(ex.requests, "get", fake_get)
    note = ex.market_cap_to_book_note(rec, "ABC")
    assert seen["symbol"] == "ABC"
    assert note.startswith("Market value vs. book value: 1.50x")


@pytest.mark.parametrize("status", [401, 503, None])
def test_market_cap_note_masks_api_key_in_errors(monkeypatch, status):
    rec, _ = record_with(shares_outstanding_m=200.0, book_equity_b=10.0)
    key = "test-key-not-real"
    monkeypatch.setenv("FINNHUB_API_KEY", key)
    url = f"https://finnhub.io/api/v1/quote?symbol=ABC&token={key}"

    class Resp:
        status_code = status

        def raise_for_status(self):
            raise ex.requests.HTTPError(f"{status} Client Error: for url: {url}",
                                        response=self)

    def fake_get(url_, params=None, timeout=None):
        if status is None:
            raise ex.requests.ConnectionError(
                f"Max retries exceeded with url: /api/v1/quote?symbol=ABC&token={key}")
        return Resp()

    monkeypatch.setattr(ex.requests, "get", fake_get)
    note = ex.market_cap_to_book_note(rec, "ABC")
    assert note.startswith("Market-value-vs-book gauge: price lookup failed (")
    assert key not in note
    assert "token=***" in note


# ---------------------------------------------------------------------------
# Plain-English layer
# ---------------------------------------------------------------------------

def test_every_rule_id_has_a_plain_entry():
    assert set(ex.PLAIN) == set(ex.RULE_IDS)
    assert len(ex.RULE_IDS) == 11


def test_plain_text_is_neutral():
    # Explanations must stay company-agnostic and never speak to "your" plan.
    banned = ["your ", "you ", "thesis", "omnicom"]
    for headline, meaning in ex.PLAIN.values():
        text = f"{headline} {meaning}".lower()
        for word in banned:
            assert word not in text


def test_plain_text_formats_from_config(omc_cfg):
    headline, meaning = ex.plain_text("segment_lag", omc_cfg)
    assert headline.startswith("Media buying lagging")
    assert "Integrated Media (about 51.5% of Core Operations)" in meaning
    _, meaning = ex.plain_text("guidance_softened", omc_cfg)
    assert '("4% organic" and "$900M synergies in 2026")' in meaning
    _, meaning = ex.plain_text("margin_floor", omc_cfg)
    assert "Omnicom Group's adjusted EBITA margin came in under the 15% floor" in meaning


@pytest.mark.parametrize("cfg", [{}, None, {"company": {"name": "X"}, "profile": {}}])
def test_plain_text_never_crashes_with_missing_context(cfg):
    for rule_id in ex.RULE_IDS:
        headline, meaning = ex.plain_text(rule_id, cfg)
        assert headline and meaning
        assert not re.search(r"\{\w+\}", headline + meaning)
    _, meaning = ex.plain_text("guidance_softened", {})
    assert "Compared with earlier guidance, this filing" in meaning


def test_plain_overrides_replace_verbatim(omc_cfg, capsys):
    omc_cfg["profile"]["plain_overrides"] = {
        "leverage_high": ["Custom headline {company}", "Custom meaning."],
        "margin_floor": "not a pair",
    }
    assert ex.plain_text("leverage_high", omc_cfg) == ("Custom headline {company}",
                                                       "Custom meaning.")
    headline, _ = ex.plain_text("margin_floor", omc_cfg)
    assert headline == ex.PLAIN["margin_floor"][0]
    assert "WARNING: profile.plain_overrides.margin_floor" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Review email
# ---------------------------------------------------------------------------

def test_review_email_structure(omc_cfg):
    omc_cfg["profile"]["plain_overrides"] = {"leverage_high": ["Debt alarm", "Explained."]}
    rec, records = record_with(leverage_ratio_x=3.5, leverage_sentence="Leverage was 3.5x.")
    fired, notes = ex.run_rules("2026-Q2", rec, records, {}, omc_cfg, "10-Q",
                                dt.date(2026, 6, 30))
    f = filing("10-Q", "2026-07-30", "0000029989-26-000020")
    subject, body, severity = ex.build_review_email("2026-Q2", rec, fired, notes, f,
                                                    CIK, "OMC", omc_cfg)
    assert subject == "OMC 2026 Q2 review draft — 1 red trigger(s)"
    assert severity == "red"
    assert "== WHAT FIRED ==" in body and "== WORTH KNOWING ==" in body
    assert "\U0001f534 Debt alarm\n   Meaning: Explained." in body
    assert "- Filing index: https://www.sec.gov/Archives/edgar/data/29989/000002998926000020/" in body
    assert ("- Every extracted number, quote, and source: filings/history.json "
            "(company 0000029989, entry '2026-Q2').") in body
    assert body.endswith(
        "Values are LLM-extracted drafts. Verify each fired trigger against the "
        "filing before acting.\nThis is automated output, not investment advice.")


def test_review_email_without_triggers(omc_cfg):
    rec, records = record_with()
    subject, body, severity = ex.build_review_email(
        "2026-Q2", rec, [], [], filing("8-K", "2026-07-28"), CIK, "OMC", omc_cfg)
    assert severity == "info"
    assert subject.endswith("— no triggers")
    assert "your" not in body.lower()
