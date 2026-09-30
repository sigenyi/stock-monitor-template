"""Filings watcher: poll SEC EDGAR for new filings from the watched company,
classify them, alert, and dispatch extraction.

Run from the repo root as:  python -m filings.watcher

Configuration: filings/filings_config.json (required; only company.cik and
company.name are mandatory) and filings/baselines.json (optional).

Per run:
  * fetch the company submissions JSON (one request when nothing is new),
  * on the first-ever run for a company (or after the configured CIK
    changed), seed state with the entire current backlog WITHOUT alerting
    (only filings appearing after that generate emails),
  * otherwise process new watched filings oldest-first:
      - 8-K containing Item 2.06  -> immediate red alert (fast path; never
        waits on extraction),
      - 8-K containing Item 2.02  -> notify + extraction on the press-release
        exhibit (when extraction.enabled),
      - 10-Q / 10-K (and /A)      -> notify + extraction on the primary doc
        (when extraction.enabled),
      - any other watched form    -> plain info notification,
    refusing to run (exit 1, no emails) when more than MAX_NEW_PER_RUN new
    filings appear at once — that means state.json is stale,
  * check the optional time-stop date and send the one-shot reminder,
  * persist seen accession numbers to state.json, bound to the watched CIK
    (committed back by the workflow).

Manual re-processing: dispatch the workflow with a force_accession input
(exposed to this script as the FORCE_ACCESSION environment variable).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from common.emailer import require_env, send_email
from filings.extraction import (archive_base, run_extraction_pipeline,
                                sec_session, user_agent_tool_name)

FILINGS_DIR = Path(__file__).parent
CONFIG_PATH = FILINGS_DIR / "filings_config.json"
BASELINES_PATH = FILINGS_DIR / "baselines.json"
STATE_PATH = FILINGS_DIR / "state.json"

EASTERN = ZoneInfo("America/New_York")

# Used when filings_config.json has no watched_forms.
DEFAULT_WATCHED_FORMS = ["8-K", "8-K/A", "10-Q", "10-K", "10-Q/A", "10-K/A"]

# Backlog safety valve: more new watched filings than this in one run means
# state.json does not match the watched company (e.g. it was copied from
# elsewhere), so the run stops instead of emailing the whole backlog.
MAX_NEW_PER_RUN = 20

FORCE_ACCESSION_RE = r"\d{10}-?\d{2}-?\d{6}"


# ---------------------------------------------------------------------------
# State & submissions
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        text = path.read_text().strip()
        if text:
            return json.loads(text)
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def fresh_state() -> dict:
    return {"seeded": False, "seen_accessions": [], "flags": {}}


def load_state(cik: str) -> dict:
    """Load state.json and bind it to `cik`.

    State written for a different CIK is discarded (the next step re-seeds
    silently). A seeded legacy file without a "cik" key adopts the current
    CIK and is saved right away.
    """
    state = load_json(STATE_PATH, None)
    if not isinstance(state, dict):
        state = fresh_state()
    state.setdefault("seeded", False)
    state.setdefault("seen_accessions", [])
    state.setdefault("flags", {})

    bound = state.get("cik")
    if bound and str(bound).zfill(10) != cik:
        print(f"WARNING: state.json belongs to CIK {bound} but config watches "
              f"CIK {cik}; re-seeding silently.")
        return fresh_state()
    if not bound and state.get("seeded"):
        state["cik"] = cik
        save_json(STATE_PATH, state)
        print(f"state.json had no CIK; bound it to CIK {cik}.")
    return state


def fetch_recent_filings(cik: str, session: requests.Session
                         ) -> tuple[list[dict], str | None]:
    """Fetch the submissions API and flatten the parallel arrays in
    filings.recent into a list of per-filing dicts. Also returns the
    submissions' top-level fiscalYearEnd ("MMDD") or None."""
    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    print(f"Fetching submissions: {url}")
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    recent = (data.get("filings") or {}).get("recent") or {}
    keys = ["form", "accessionNumber", "filingDate", "reportDate", "items",
            "primaryDocument"]

    def column(key: str, i: int):
        # A short, missing, or null array yields "" instead of IndexError.
        values = recent.get(key)
        if isinstance(values, list) and i < len(values) and values[i] is not None:
            return values[i]
        return ""

    count = len(recent.get("accessionNumber") or [])
    filings = [{k: column(k, i) for k in keys} for i in range(count)]
    print(f"Submissions parsed: {count} recent filings.")
    return filings, (data.get("fiscalYearEnd") or None)


def _month_from(month: int, day: int) -> int:
    """Fiscal year-end month. A year that ends in the first week of a month
    belongs to a 52/53-week calendar anchored on the previous month-end
    (e.g. '0201' for a January retailer, '0103' for a December one)."""
    if day <= 7:
        return (month - 2) % 12 + 1
    return month


def resolve_fye_month(cfg: dict, submissions_fye: str | None) -> int:
    """company.fiscal_year_end ("MM-DD") -> else submissions fiscalYearEnd
    ("MMDD") -> else 12 (calendar year)."""
    configured = (cfg.get("company") or {}).get("fiscal_year_end")
    if configured:
        m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})\s*", str(configured))
        if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
            return _month_from(int(m.group(1)), int(m.group(2)))
        print(f"WARNING: company.fiscal_year_end {configured!r} is not MM-DD; "
              f"ignoring it.")
    if submissions_fye:
        m = re.fullmatch(r"(\d{2})(\d{2})", str(submissions_fye).strip())
        if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
            return _month_from(int(m.group(1)), int(m.group(2)))
    return 12


def item_numbers(filing: dict) -> set[str]:
    """8-K item numbers as a set, e.g. {'2.02', '9.01'}."""
    return {x.strip() for x in (filing.get("items") or "").split(",") if x.strip()}


def extraction_enabled(cfg: dict) -> bool:
    """filings_config.json > extraction.enabled (default: true)."""
    enabled = (cfg.get("extraction") or {}).get("enabled", True)
    return True if enabled is None else bool(enabled)


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def filing_links(filing: dict, cik: str) -> str:
    base = archive_base(cik, filing["accessionNumber"])
    return (f"Filing index: {base}\n"
            f"Primary document: {base}{filing.get('primaryDocument', '')}")


def filing_detail_block(filing: dict, cik: str) -> str:
    return (f"Form type: {filing.get('form')}\n"
            f"Filing date: {filing.get('filingDate')}\n"
            f"Report date: {filing.get('reportDate') or 'n/a'}\n"
            f"Accession: {filing.get('accessionNumber')}\n"
            f"{filing_links(filing, cik)}")


def company_label(company: dict) -> str:
    """Short name for email subjects: ticker, else company name, else CIK
    (same fallback as the extraction pipeline; a null ticker counts as absent)."""
    return company.get("ticker") or company.get("name") or str(company["cik"])


def send_impairment_red_alert(filing: dict, cfg: dict) -> None:
    company = cfg["company"]
    send_email(
        subject=f"Goodwill impairment 8-K filed — {company_label(company)} "
                f"(Item 2.06)",
        body=(f"{company.get('name')} filed an 8-K containing Item 2.06 "
              f"(Material Impairments) on {filing.get('filingDate')}.\n\n"
              f"This is the fastest legal tripwire for a goodwill impairment. "
              f"Read the filing now.\n\n"
              f"{filing_detail_block(filing, company['cik'])}"),
        severity="red",
    )


def send_notification(filing: dict, cfg: dict, note: str = "") -> None:
    company = cfg["company"]
    body = (f"New {filing.get('form')} filed by {company.get('name')}.\n\n"
            f"{filing_detail_block(filing, company['cik'])}")
    if note:
        body += f"\n\n{note}"
    send_email(
        subject=f"{company_label(company)} filing: {filing.get('form')} "
                f"({filing.get('filingDate')})",
        body=body,
        severity="info",
    )


# ---------------------------------------------------------------------------
# Classification & dispatch
# ---------------------------------------------------------------------------

def process_filing(filing: dict, cfg: dict, baselines: dict,
                   contact_email: str) -> None:
    form = filing.get("form", "")
    items = item_numbers(filing)
    print(f"Processing {form} {filing.get('accessionNumber')} "
          f"filed {filing.get('filingDate')} items={sorted(items) or 'n/a'}")

    is_8k = form.startswith("8-K")
    sent_206 = False
    if is_8k and "2.06" in items:
        # Fast path: this alert must never wait on extraction, and it is sent
        # first and separately even if extraction of the same filing follows.
        send_impairment_red_alert(filing, cfg)
        sent_206 = True

    extract = extraction_enabled(cfg)
    if is_8k and "2.02" in items:
        if extract:
            send_notification(
                filing, cfg,
                note="Earnings 8-K — running LLM extraction on the press-release "
                     "exhibit; a review draft email follows.")
            run_extraction_pipeline(filing, cfg, baselines, contact_email)
        else:
            send_notification(
                filing, cfg,
                note="Earnings 8-K (LLM extraction disabled in "
                     "filings_config.json).")
    elif form.startswith(("10-Q", "10-K")):
        if extract:
            send_notification(
                filing, cfg,
                note="Periodic report — running LLM extraction on the primary "
                     "document; a review draft email follows.")
            run_extraction_pipeline(filing, cfg, baselines, contact_email)
        else:
            send_notification(
                filing, cfg,
                note="Periodic report (LLM extraction disabled in "
                     "filings_config.json).")
    elif not sent_206:
        # Any other watched form (or an 8-K without 2.02/2.06).
        send_notification(filing, cfg)


def check_time_stop(state: dict, baselines: dict) -> bool:
    """Send the one-shot time-stop reminder if the review date has arrived.
    Returns True if state changed."""
    ts = baselines.get("time_stop") or {}
    review_after = ts.get("review_after")
    if not review_after:
        return False
    flags = state.setdefault("flags", {})
    if flags.get("time_stop_sent"):
        return False
    today_et = dt.datetime.now(EASTERN).date()
    if today_et < dt.date.fromisoformat(review_after):
        return False
    body = (f"The time-stop review date {review_after} has been reached "
            f"(today, ET: {today_et}).")
    if ts.get("note"):
        body += f"\n\nStored note:\n{ts['note']}"
    send_email(
        subject=f"Time-stop review date reached ({review_after})",
        body=body,
        severity="clock",
    )
    flags["time_stop_sent"] = True
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    contact_email = require_env("SEC_CONTACT_EMAIL")
    # Fail fast on email secrets before doing any work.
    require_env("EMAIL_ADDRESS"); require_env("EMAIL_APP_PASSWORD"); require_env("EMAIL_TO")

    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        print("FATAL: filings/filings_config.json missing/empty.", file=sys.stderr)
        return 1
    company = cfg.get("company") or {}
    if not company.get("cik") or not company.get("name"):
        print("FATAL: filings_config.json must set company.cik and "
              "company.name.", file=sys.stderr)
        return 1
    baselines = load_json(BASELINES_PATH, {}) or {}  # optional file
    cik = str(company["cik"]).zfill(10)
    cfg["company"]["cik"] = cik  # normalized everywhere downstream
    watched_forms = set(cfg.get("watched_forms") or DEFAULT_WATCHED_FORMS)

    force = os.environ.get("FORCE_ACCESSION", "").strip()
    if force and not re.fullmatch(FORCE_ACCESSION_RE, force):
        print(f"FATAL: force_accession {force!r} is not an accession number "
              f"(expected 0000000000-00-000000, dashes optional).",
              file=sys.stderr)
        return 1

    state = load_state(cik)

    # Time-stop reminder piggybacks on the hourly schedule.
    try:
        if check_time_stop(state, baselines):
            save_json(STATE_PATH, state)
    except Exception:
        print("ERROR: time-stop check failed (continuing):")
        traceback.print_exc()

    session = sec_session(contact_email, user_agent_tool_name(cfg))
    filings, submissions_fye = fetch_recent_filings(cik, session)
    cfg["_fye_month"] = resolve_fye_month(cfg, submissions_fye)
    print(f"Fiscal year ends in month {cfg['_fye_month']}.")

    # --- Manual re-processing of one accession (workflow_dispatch input) ---
    if force:
        target = force.replace("-", "")
        match = next(
            (f for f in filings if f["accessionNumber"].replace("-", "") == target),
            None,
        )
        if not match:
            print(f"ERROR: force_accession {force!r} not found in the recent "
                  f"submissions block (it only covers the most recent ~1000 "
                  f"filings).", file=sys.stderr)
            return 1
        print(f"FORCE mode: re-processing {match['accessionNumber']} even if "
              f"already seen.")
        process_filing(match, cfg, baselines, contact_email)
        seen_list = state.setdefault("seen_accessions", [])
        if match["accessionNumber"] not in seen_list:
            seen_list.append(match["accessionNumber"])
            save_json(STATE_PATH, state)
        return 0

    # --- First-ever run: seed state silently, no backlog spam --------------
    if not state.get("seeded"):
        state["seen_accessions"] = sorted({f["accessionNumber"] for f in filings})
        state["seeded"] = True
        state["cik"] = cik
        save_json(STATE_PATH, state)
        print(f"First run for CIK {cik}: seeded state with "
              f"{len(state['seen_accessions'])} existing filings WITHOUT "
              f"alerting. Only filings from now on generate emails.")
        return 0

    # --- Normal path: new watched filings, oldest first --------------------
    seen_list = state.setdefault("seen_accessions", [])
    seen = set(seen_list)
    new = [f for f in filings
           if f["form"] in watched_forms and f["accessionNumber"] not in seen]
    new.sort(key=lambda f: (f.get("filingDate", ""), f["accessionNumber"]))

    if not new:
        print("No new watched filings.")
        return 0

    if len(new) > MAX_NEW_PER_RUN:
        print(f"FATAL: {len(new)} new watched filings for CIK {cik} in one run "
              f"(limit {MAX_NEW_PER_RUN}). filings/state.json looks stale for "
              f"this company, so no emails were sent. To re-seed silently, "
              f"reset filings/state.json to "
              f'{{"flags": {{}}, "seeded": false, "seen_accessions": []}} '
              f"(or delete it) and run again.", file=sys.stderr)
        return 1

    print(f"{len(new)} new watched filing(s) to process.")
    for filing in new:
        try:
            process_filing(filing, cfg, baselines, contact_email)
        except Exception:
            # Even the notification failed (e.g. SMTP outage). Do NOT mark
            # this filing as seen — the next hourly run retries it.
            print(f"ERROR: processing failed for {filing['accessionNumber']}; "
                  f"leaving unseen so the next run retries it.")
            traceback.print_exc()
            continue
        seen_list.append(filing["accessionNumber"])
        save_json(STATE_PATH, state)  # persist progress after each filing

    return 0


if __name__ == "__main__":
    sys.exit(main())
