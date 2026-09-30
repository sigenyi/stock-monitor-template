"""Fiscal-quarter derivation and fiscal year-end resolution."""

import calendar
import datetime as dt

import pytest

from filings import extraction as ex
from filings import watcher


def filing(form, report_date="", filing_date="2026-08-01"):
    return {"form": form, "reportDate": report_date, "filingDate": filing_date,
            "accessionNumber": "0000000000-26-000001"}


# ---------------------------------------------------------------------------
# December fiscal year-end (calendar quarters, as before)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form, report_date, label, pe", [
    ("8-K", "2026-07-28", "2026-Q2", dt.date(2026, 6, 30)),
    ("10-Q", "2026-06-30", "2026-Q2", dt.date(2026, 6, 30)),
    ("8-K", "2026-01-15", "2025-Q4", dt.date(2025, 12, 31)),
    ("8-K/A", "2026-04-28", "2026-Q1", dt.date(2026, 3, 31)),
    ("10-K", "2025-12-31", "2025-Q4", dt.date(2025, 12, 31)),
    ("10-Q/A", "2026-09-30", "2026-Q3", dt.date(2026, 9, 30)),
    ("10-Q", "2026-06-27", "2026-Q2", dt.date(2026, 6, 30)),  # 52/53-week period
])
def test_december_fye(form, report_date, label, pe):
    assert ex.derive_quarter(filing(form, report_date), 12) == (label, pe)
    assert ex.derive_quarter(filing(form, report_date)) == (label, pe)  # default 12


def test_december_labels_match_calendar_quarters():
    for year in (2025, 2026):
        for month in range(1, 13):
            pe = dt.date(year, month, calendar.monthrange(year, month)[1])
            assert ex.quarter_label(pe, 12) == f"{year}-Q{(month - 1) // 3 + 1}"


def test_8k_dated_on_a_quarter_end_uses_the_previous_quarter():
    assert ex.derive_quarter(filing("8-K", "2026-06-30"), 12)[0] == "2026-Q1"


# ---------------------------------------------------------------------------
# January fiscal year-end (retailer)
# ---------------------------------------------------------------------------

def test_january_fye_10q_and_its_8k_share_a_label():
    ten_q = ex.derive_quarter(filing("10-Q", "2026-05-02"), 1)
    eight_k = ex.derive_quarter(filing("8-K", "2026-05-20"), 1)
    assert ten_q == eight_k == ("2027-Q1", dt.date(2026, 4, 30))


@pytest.mark.parametrize("form, report_date, label", [
    ("10-K", "2026-01-31", "2026-Q4"),   # fiscal year ending Jan 2026
    ("10-Q", "2026-05-02", "2027-Q1"),
    ("8-K", "2026-03-05", "2026-Q4"),    # Q4 earnings release
    ("10-Q", "2026-08-01", "2027-Q2"),   # 52/53-week quarter ending Aug 1
    ("10-Q", "2026-10-31", "2027-Q3"),
    ("8-K", "2026-11-20", "2027-Q3"),
])
def test_january_fye_labels(form, report_date, label):
    assert ex.derive_quarter(filing(form, report_date), 1)[0] == label


def test_quarter_label_fiscal_year_is_the_year_it_ends():
    assert ex.quarter_label(dt.date(2026, 6, 30), 6) == "2026-Q4"
    assert ex.quarter_label(dt.date(2026, 9, 30), 6) == "2027-Q1"
    assert ex.quarter_label(dt.date(2026, 12, 31), 9) == "2027-Q1"


# ---------------------------------------------------------------------------
# Missing dates
# ---------------------------------------------------------------------------

def test_missing_report_date_falls_back_to_filing_date():
    assert ex.period_end(filing("10-Q", "", "2026-07-28"), 12) == dt.date(2026, 6, 30)
    # A 10-K filed in late February still describes the year that ended in
    # December (not the nearer March quarter-end).
    assert ex.period_end(filing("10-K", "", "2026-02-20"), 12) == dt.date(2025, 12, 31)
    assert ex.period_end(filing("8-K", "", "2026-04-28"), 12) == dt.date(2026, 3, 31)


def test_missing_both_dates_raises_value_error():
    with pytest.raises(ValueError):
        ex.period_end(filing("10-Q", "", ""), 12)


# ---------------------------------------------------------------------------
# prev_quarter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quarter, previous", [
    ("2026-Q1", "2025-Q4"), ("2026-Q2", "2026-Q1"), ("2027-Q4", "2027-Q3"),
])
def test_prev_quarter(quarter, previous):
    assert ex.prev_quarter(quarter) == previous


# ---------------------------------------------------------------------------
# Fiscal year-end resolution (watcher) and extraction's default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("configured, submissions, month", [
    ("12-31", None, 12),
    ("12-31", "0131", 12),     # config wins over submissions
    (None, "0131", 1),
    (None, "0630", 6),
    (None, "0201", 1),         # 52/53-week year ending early February
    (None, "0103", 12),        # 52/53-week year ending early January
    (None, None, 12),
    ("bogus", "0930", 9),      # invalid config falls through
    ("13-01", None, 12),
    (None, "", 12),
])
def test_resolve_fye_month(configured, submissions, month):
    cfg = {"company": {"cik": "1", "name": "X"}}
    if configured is not None:
        cfg["company"]["fiscal_year_end"] = configured
    assert watcher.resolve_fye_month(cfg, submissions) == month


@pytest.mark.parametrize("cfg, month", [
    ({}, 12), (None, 12), ({"_fye_month": 1}, 1), ({"_fye_month": 13}, 12),
    ({"_fye_month": "1"}, 12), ({"_fye_month": True}, 12),
])
def test_extraction_fye_month_defaults_to_12(cfg, month):
    assert ex.fye_month_of(cfg) == month
