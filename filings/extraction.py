"""Extraction pipeline for the filings watcher.

Given a newly classified filing (earnings 8-K or 10-Q/10-K), this module:
  1. retrieves the right document from EDGAR (press-release exhibit for
     earnings 8-Ks, primary document for 10-Q/10-K),
  2. converts HTML to plain text and — for large 10-Q/10-K documents — builds
     a targeted excerpt around anchor phrases,
  3. calls Claude to extract a fixed fundamentals checklist as strict JSON and
     normalizes the value types,
  4. merges the result into quarter-keyed history, namespaced by CIK, with
     per-field provenance,
  5. runs the red/yellow/watch rules engine,
  6. sends the pre-filled quarterly review email.

Company-specific wording (labels, segment, guidance phrases, extra anchors)
comes from `profile` in filings_config.json and rule thresholds from
`thresholds`; every profile/threshold key is optional. Without a key the
prompt falls back to generic wording, and a rule that needs a missing
threshold (or segment) is skipped.

Failure handling: any failure in steps 1-3 sends an explicit "extraction
failed — review the filing manually" email; a failure in steps 4-6 (before
the review email is sent) sends a "review step failed" email. Either way the
fact that a filing exists is never silently suppressed (the watcher has
already sent the plain notification before calling into this module). Only a
failure to send email at all propagates, so the watcher retries the filing.

The Claude output is a DRAFT. The review email says so in its footer.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import os
import re
import time
import traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from common.emailer import send_email

HISTORY_PATH = Path(__file__).with_name("history.json")

# Defaults for filings_config.json > extraction (each key is optional there).
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TEMPERATURE = 0

# Default for filings_config.json > user_agent_tool_name.
DEFAULT_TOOL_NAME = "StockMonitor"

SEC_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
SLEEP_BETWEEN_DOWNLOADS_S = 0.8

# ---- Length control for 10-Q / 10-K excerpting -----------------------------
COVER_CHARS = 6_000
ANCHOR_WINDOW = 4_000
TOTAL_CAP = 200_000

# Anchors used for every company. profile.extra_anchor_phrases and
# profile.segment.name are appended by anchor_phrases().
BASE_ANCHOR_PHRASES = [
    "Issuer Purchases of Equity Securities",
    # Some filers title Part II Item 2 with the official item name and say
    # "repurchase" rather than the phrase above (the example company's 10-Qs
    # do); these anchors capture the buyback table either way.
    "Unregistered Sales of Equity Securities",
    "repurchase",
    "leverage ratio",
    "Goodwill",
    "Impairment",
    "Organic",
    "Total leverage",
]

# The full checklist. Order here is the order in the prompt and in history.
SCHEMA_KEYS = [
    "organic_growth_core_pct",
    "total_reported_revenue_change_pct",
    "perimeter_changed",
    "adj_margin_pct",
    "segment_organic_pct",
    "group_organic_pct",
    "shares_outstanding_m",
    "buyback_dollars_qtr_m",
    "buyback_remaining_authorization_m",
    "buyback_suspension_language",
    "leverage_sentence",
    "leverage_ratio_x",
    "goodwill_impairment_charge",
    "goodwill_cushion_pct",
    "book_equity_b",
    "guidance_language",
]

# Value types, used by parse_extraction_json() to normalize model output.
NUMERIC_KEYS = {
    "organic_growth_core_pct",
    "total_reported_revenue_change_pct",
    "adj_margin_pct",
    "segment_organic_pct",
    "group_organic_pct",
    "shares_outstanding_m",
    "buyback_dollars_qtr_m",
    "buyback_remaining_authorization_m",
    "leverage_ratio_x",
    "goodwill_cushion_pct",
    "book_equity_b",
}
BOOL_KEYS = {"perimeter_changed", "buyback_suspension_language"}

DEFAULT_MARGIN_LABEL = "adjusted operating margin"

SCHEMA_HEADER = """\
Return ONE JSON object with EXACTLY these keys. Each key maps to an object:
  { "found": true|false, "value": ..., "quote": "short verbatim supporting quote"|null, "location": "brief location hint"|null }
If something is not stated in the document, use {"found": false, "value": null, "quote": null, "location": null}.
NEVER guess, infer, or compute values that are not stated in the text.
"""

SYSTEM_PROMPT = (
    "You are a careful financial-filings extraction assistant. You extract "
    "only facts that are explicitly stated in the provided document text. "
    "You respond with ONLY a single valid JSON object — no markdown code "
    "fences, no commentary, no explanation before or after. Every extracted "
    "value must be accompanied by a short verbatim supporting quote from the "
    "document and a brief location hint. Anything not present in the "
    "document must be reported as found=false with a null value. Never "
    "guess, estimate, or compute values that are not stated in the text."
)


# ============================================================================
# Config / profile helpers
# ============================================================================

def _profile(cfg: dict | None) -> dict:
    profile = (cfg or {}).get("profile")
    return profile if isinstance(profile, dict) else {}


def _thresholds(cfg: dict | None) -> dict:
    thresholds = (cfg or {}).get("thresholds")
    return thresholds if isinstance(thresholds, dict) else {}


def _segment(profile: dict) -> dict | None:
    """profile.segment when it is configured with a name, else None."""
    seg = (profile or {}).get("segment")
    if isinstance(seg, dict) and isinstance(seg.get("name"), str) and seg["name"].strip():
        return seg
    return None


def _str_list(value) -> list[str]:
    """A config value that should be a list of strings (a lone string is
    accepted as a one-item list; blanks and non-strings are dropped)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _join_quoted(phrases: list[str]) -> str:
    """'"a"', '"a" and "b"', '"a", "b" and "c"'."""
    quoted = [f'"{p}"' for p in phrases]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def user_agent_tool_name(cfg: dict | None) -> str:
    return (cfg or {}).get("user_agent_tool_name") or DEFAULT_TOOL_NAME


def fye_month_of(cfg: dict | None) -> int:
    """Fiscal year-end month resolved by the watcher (cfg["_fye_month"]);
    12 (calendar year) when absent or invalid."""
    month = (cfg or {}).get("_fye_month")
    if isinstance(month, int) and not isinstance(month, bool) and 1 <= month <= 12:
        return month
    return 12


# ============================================================================
# Profile-driven prompt
# ============================================================================

def anchor_phrases(profile: dict | None) -> list[str]:
    """Excerpt anchors: BASE_ANCHOR_PHRASES + profile.extra_anchor_phrases +
    the segment name, de-duplicated case-insensitively, order preserved."""
    profile = profile or {}
    candidates = list(BASE_ANCHOR_PHRASES) + _str_list(profile.get("extra_anchor_phrases"))
    seg = _segment(profile)
    if seg:
        candidates.append(seg["name"].strip())
    phrases, seen = [], set()
    for phrase in candidates:
        if phrase.lower() not in seen:
            seen.add(phrase.lower())
            phrases.append(phrase)
    return phrases


def schema_keys(profile: dict | None) -> list[str]:
    """The checklist for this profile: segment_organic_pct only when a
    segment is configured."""
    if _segment(profile or {}):
        return list(SCHEMA_KEYS)
    return [k for k in SCHEMA_KEYS if k != "segment_organic_pct"]


def build_schema_spec(profile: dict | None, keys: list[str]) -> str:
    """The numbered extraction instructions for `keys`, worded from the
    profile. Missing profile keys fall back to generic wording."""
    profile = profile or {}
    number = {k: i for i, k in enumerate(keys, 1)}

    def ref(key: str) -> str:
        return f"key {number[key]}" if key in number else f'"{key}"'

    core_label = (profile.get("core_label") or "").strip()
    margin_label = (profile.get("margin_label") or "").strip()
    hint = (profile.get("perimeter_section_hint") or "").strip()
    seg = _segment(profile)
    phrases = _str_list(profile.get("guidance_reference_phrases"))

    core_desc = (f"organic revenue growth for {core_label} for this quarter"
                 if core_label else
                 "the company's headline organic (or constant-currency, excluding "
                 "acquisitions/divestitures) revenue growth for this quarter")
    section = (f'the "{hint}" section (or equivalent)' if hint else
               "the section defining organic growth (or its non-GAAP reconciliation)")
    margin_desc = margin_label or (f"{DEFAULT_MARGIN_LABEL} (the company's headline "
                                   f"adjusted/non-GAAP operating margin)")
    guidance_value = ('value: {"softened": true|false, "assessment": "one short '
                      'sentence"}')
    if phrases:
        noun = "these reference phrases" if len(phrases) > 1 else "this reference phrase"
        guidance_rule = (f" judged RELATIVE TO {noun}: {_join_quoted(phrases)} "
                         f"(softened / dropped counts as true; intact counts as false).")
    else:
        guidance_rule = (" — softened=true ONLY if the document explicitly lowers, "
                         "withdraws, or narrows toward the low end previously issued "
                         "guidance; absence of guidance is NOT softening.")

    items = {
        "organic_growth_core_pct":
            f"{core_desc}, in percent (the headline figure, usually on page 1). "
            f"value: number.",
        "total_reported_revenue_change_pct":
            "total REPORTED revenue change vs the prior-year quarter, in percent "
            "(as reported, not organic). value: number.",
        "perimeter_changed":
            f"judgment: does {section} indicate that the "
            f"{core_label or 'organic-growth'} perimeter / disposal scope CHANGED "
            f"versus the prior presentation? value: true|false. Quote the "
            f"definition language you relied on.",
        "adj_margin_pct":
            f"{margin_desc}, in percent. value: number.",
        "segment_organic_pct":
            (f"organic growth for the {seg['name'].strip()} segment/discipline, "
             f"in percent. value: number.") if seg else
            "organic growth for the company's largest reported segment, in "
            "percent. value: number.",
        "group_organic_pct":
            f"group/total organic growth, in percent (may equal "
            f"{ref('organic_growth_core_pct')}). value: number.",
        "shares_outstanding_m":
            "shares outstanding from the 10-Q/10-K cover page, in MILLIONS. "
            "value: number.",
        "buyback_dollars_qtr_m":
            "dollars of issuer share repurchases during the quarter (10-Q Part II "
            "Item 2 \"Issuer Purchases of Equity Securities\" table), in MILLIONS "
            "of dollars. value: number.",
        "buyback_remaining_authorization_m":
            "remaining repurchase authorization, in MILLIONS of dollars. "
            "value: number.",
        "buyback_suspension_language":
            "is there any language suspending, pausing, or reducing the share "
            "repurchase program? value: true|false, with the quote.",
        "leverage_sentence":
            "the VERBATIM sentence describing the credit-agreement leverage ratio "
            "/ covenant. value: the full sentence as a string.",
        "leverage_ratio_x":
            "the leverage ratio from that sentence parsed as a plain number "
            "(e.g. 2.5). value: number.",
        "goodwill_impairment_charge":
            "is a goodwill impairment charge reported in this filing? value: "
            '{"impaired": true|false, "amount_m": number-in-millions-or-null}.',
        "goodwill_cushion_pct":
            "(10-K annual report only) the disclosed goodwill cushion: \"fair "
            "value exceeded carrying value by X%\" per reporting unit. Report the "
            "MINIMUM X% across reporting units. value: number.",
        "book_equity_b":
            "total shareholders' equity from the balance sheet, in BILLIONS of "
            "dollars. value: number.",
        "guidance_language":
            "current guidance phrasing. quote: the guidance sentence(s). "
            + guidance_value + guidance_rule,
    }

    lines = []
    for i, key in enumerate(keys, 1):
        text = items.get(key, "value as stated in the document.")
        lines.append(f"{f'{i}.':<4}\"{key}\" — {text}")
    footer = (f"Fields not applicable to this form type (e.g. "
              f"{ref('goodwill_cushion_pct')} outside a 10-K, or "
              f"{ref('buyback_dollars_qtr_m')} in a press release) should simply "
              f'be {{"found": false, ...}}.')
    return SCHEMA_HEADER + "\n" + "\n".join(lines) + "\n\n" + footer


# ============================================================================
# EDGAR document retrieval
# ============================================================================

def sec_session(contact_email: str, tool_name: str = DEFAULT_TOOL_NAME) -> requests.Session:
    """requests session with the User-Agent the SEC asks automated clients to
    send: a tool name plus a contact email."""
    s = requests.Session()
    s.headers.update({"User-Agent": f"{tool_name or DEFAULT_TOOL_NAME} {contact_email}"})
    return s


def archive_base(cik: str, accession: str) -> str:
    return f"{SEC_ARCHIVE_BASE}/{int(cik)}/{accession.replace('-', '')}/"


def _ex99_from_index_page(base: str, accession: str,
                          session: requests.Session) -> list[str]:
    """Authoritative exhibit lookup: the filing index page's 'Document Format
    Files' table maps each document to its official Type (e.g. EX-99.1),
    regardless of filename fashion. Returns HTML EX-99 document names in
    sequence order; [] on any failure (callers fall back)."""
    url = f"{base}{accession}-index.html"
    print(f"Fetching filing index page: {url}")
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    names: list[str] = []
    for table in soup.find_all("table"):
        if "document" not in (table.get("summary") or "").lower():
            continue
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cells) < 4 or not cells[3].upper().startswith("EX-99"):
                continue
            link = row.find("a")
            if not link or not link.get("href"):
                continue
            name = link["href"].rstrip("/").rsplit("/", 1)[-1]
            if name.lower().endswith((".htm", ".html")):
                names.append(name)
    return names


def choose_documents(filing: dict, cik: str, session: requests.Session) -> list[dict]:
    """Return [{name, url}] of the document(s) to extract from.

    Earnings 8-K: the checklist lives in the press-release exhibit (EX-99.1),
    not the 8-K shell. Three-stage heuristic, logged:
      1. parse the filing index page for documents officially typed EX-99.x
         (handles filers whose exhibit filenames — e.g.
         'a2026q1earningsrelease.htm' — don't contain 'ex99');
      2. fall back to index.json filenames matching /ex.?99/i;
      3. fall back to the primary document.
    10-Q / 10-K: the primary document.
    """
    base = archive_base(cik, filing["accessionNumber"])
    form = filing.get("form", "")
    primary = filing.get("primaryDocument", "")

    if form.startswith("8-K"):
        try:
            names = _ex99_from_index_page(base, filing["accessionNumber"], session)
        except Exception as exc:
            print(f"WARNING: index-page exhibit lookup failed ({exc}); "
                  f"trying filename heuristic.")
            names = []
        if names:
            print(f"EX-99 exhibits from index page: {names}")
            return [{"name": n, "url": base + n} for n in names]

        time.sleep(SLEEP_BETWEEN_DOWNLOADS_S)
        index_url = base + "index.json"
        print(f"Fetching file inventory: {index_url}")
        resp = session.get(index_url, timeout=60)
        resp.raise_for_status()
        items = resp.json().get("directory", {}).get("item", [])
        pattern = re.compile(r"ex.?99", re.IGNORECASE)
        matches = sorted(
            it["name"] for it in items
            if pattern.search(it.get("name", ""))
            and it.get("name", "").lower().endswith((".htm", ".html"))
        )
        if matches:
            print(f"Press-release exhibit filename heuristic matched: {matches}")
            return [{"name": n, "url": base + n} for n in matches]
        print(f"No EX-99 HTML exhibit found; falling back to primary "
              f"document {primary!r}.")

    print(f"Using primary document: {primary!r}")
    return [{"name": primary, "url": base + primary}]


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)        # collapse runs of spaces
    text = re.sub(r"\n\s*\n+", "\n\n", text)       # collapse blank-line runs
    return text.strip()


def build_excerpt(text: str, phrases: list[str]) -> str:
    """Targeted excerpt for large 10-Q/10-K documents: the first ~6,000 chars
    (cover page) plus ±4,000-char windows around each anchor phrase, with
    overlapping windows merged, each labeled with its approximate position,
    capped at ~200,000 chars total."""
    n = len(text)
    intervals: list[tuple[int, int]] = [(0, min(COVER_CHARS, n))]
    lower = text.lower()
    for phrase in phrases:
        p = phrase.lower()
        if not p:
            continue
        start = 0
        while True:
            idx = lower.find(p, start)
            if idx == -1:
                break
            intervals.append((max(0, idx - ANCHOR_WINDOW),
                              min(n, idx + len(p) + ANCHOR_WINDOW)))
            start = idx + len(p)

    intervals.sort()
    merged: list[list[int]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    parts, used = [], 0
    for lo, hi in merged:
        if used >= TOTAL_CAP:
            parts.append("\n[... excerpt cap reached; remaining sections omitted ...]")
            break
        chunk = text[lo:hi]
        if used + len(chunk) > TOTAL_CAP:
            chunk = chunk[: TOTAL_CAP - used]
        parts.append(f"\n===== EXCERPT (chars {lo:,}-{hi:,} of {n:,}) =====\n{chunk}")
        used += len(chunk)
    excerpt = "".join(parts)
    print(f"Excerpt built: {len(merged)} merged window(s), "
          f"{len(excerpt):,} chars from a {n:,}-char document.")
    return excerpt


def prepare_document_text(filing: dict, cik: str, session: requests.Session,
                          phrases: list[str] | None = None) -> str:
    docs = choose_documents(filing, cik, session)
    texts = []
    for i, doc in enumerate(docs):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_DOWNLOADS_S)
        print(f"Downloading {doc['url']}")
        resp = session.get(doc["url"], timeout=120)
        resp.raise_for_status()
        texts.append(f"\n########## DOCUMENT: {doc['name']} ##########\n"
                     + html_to_text(resp.text))
    full = "".join(texts)
    print(f"Plain text length: {len(full):,} chars.")

    if filing.get("form", "").startswith(("10-Q", "10-K")):
        return build_excerpt(full, phrases if phrases is not None else BASE_ANCHOR_PHRASES)
    # Earnings releases are small — send full text (defensive cap regardless).
    if len(full) > TOTAL_CAP:
        print(f"WARNING: document exceeds {TOTAL_CAP:,} chars; truncating.")
        full = full[:TOTAL_CAP]
    return full


# ============================================================================
# Claude extraction call
# ============================================================================

def call_claude(document_text: str, form: str, cfg: dict | None,
                keys: list[str]) -> dict:
    import anthropic  # imported here so the watcher's non-extraction paths
                      # never depend on the SDK being importable

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing — cannot run extraction.")

    ext = (cfg or {}).get("extraction")
    ext = ext if isinstance(ext, dict) else {}
    model = ext.get("model") or DEFAULT_MODEL
    max_tokens = int(ext.get("max_tokens") or DEFAULT_MAX_TOKENS)
    temperature = ext.get("temperature", DEFAULT_TEMPERATURE)

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = (
        f"The document below is from a {form} filing. Fields not expected in "
        f"this form type may be absent — report those as found=false.\n\n"
        f"{build_schema_spec(_profile(cfg), keys)}\n\n"
        f"----- DOCUMENT TEXT BEGINS -----\n{document_text}\n"
        f"----- DOCUMENT TEXT ENDS -----"
    )
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    # Newer models reject non-default sampling parameters (a 400 error), so
    # temperature is only sent when configured; set extraction.temperature
    # to null in filings_config.json to omit it.
    if temperature is not None:
        request["temperature"] = temperature
    print(f"Calling {model} (prompt ~{len(user_prompt):,} chars) ...")
    response = client.messages.create(**request)

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise RuntimeError(
            f"model output was cut off at max_tokens={max_tokens} before the JSON "
            f"was complete — raise extraction.max_tokens in "
            f"filings/filings_config.json.")
    if stop_reason == "refusal":
        raise RuntimeError("model declined the request (stop_reason=refusal)")

    raw = "".join(b.text for b in response.content if b.type == "text")
    print(f"Model returned {len(raw):,} chars.")
    return parse_extraction_json(raw, keys)


# ============================================================================
# Parse + type normalization
# ============================================================================

_NUM_RE = re.compile(
    r"(?P<sign>[+-]?)\s*\$?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"\s*[%xX]?")

# Accounting-style negatives: the parentheses may wrap the whole value
# ("(3.1%)", "($786.1)") or sit inside a leading "$" / trailing unit
# ("(3.1)%", "$(786.1)", "(2.4)x"), as filings usually print them.
_PAREN_NEG_RE = re.compile(r"(?P<dollar>\$?)\s*\((?P<inner>.*)\)\s*(?P<unit>[%xX]?)")


def _num(value):
    """Coerce a model-supplied number. int/float pass through (bool is NOT a
    number); strings such as "3.9%", "-2%", "$786.1", "1,234.5", "2.4x",
    "(3.1)", "(3.1)%" or "$(786.1)" (parentheses = negative) become floats.
    Values a float cannot hold (inf, nan, or an integer beyond float range)
    are rejected. Anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # json.loads keeps a huge integer literal (1 followed by 400 zeros) as
        # an int, and math.isfinite raises OverflowError converting it; the
        # later :g / :.1f formatting would too. Treat it like 1e400 (inf).
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
    if not isinstance(value, str):
        return None
    s = value.strip().replace("−", "-")  # unicode minus sign
    negative = False
    paren = _PAREN_NEG_RE.fullmatch(s)
    if paren:
        negative = True
        s = paren.group("dollar") + paren.group("inner").strip() + paren.group("unit")
    m = _NUM_RE.fullmatch(s)
    if not m:
        return None
    number = float(m.group("num").replace(",", ""))
    if not math.isfinite(number):  # a 309+ digit string parses to inf
        return None
    if negative or m.group("sign") == "-":
        number = -number
    return number


def _bool(value):
    """Coerce a model-supplied boolean: bools pass through; "true"/"yes" and
    "false"/"no" (any case) are accepted. Anything else -> None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes"):
            return True
        if s in ("false", "no"):
            return False
    return None


def _text(value):
    """quote / location: a string or None."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        return " / ".join(str(v) for v in value)
    return str(value)


_INVALID = object()  # marker: value present but not coercible


def _coerce_value(key: str, value):
    """Return the normalized value for `key`, None, or _INVALID."""
    if value is None:
        return None
    if key in NUMERIC_KEYS:
        number = _num(value)
        return _INVALID if number is None else number
    if key in BOOL_KEYS:
        flag = _bool(value)
        return _INVALID if flag is None else flag
    if key == "goodwill_impairment_charge":
        if isinstance(value, dict):
            return {"impaired": _bool(value.get("impaired")) or False,
                    "amount_m": _num(value.get("amount_m"))}
        flag = _bool(value)
        return _INVALID if flag is None else {"impaired": flag, "amount_m": None}
    if key == "guidance_language":
        if isinstance(value, dict):
            assessment = value.get("assessment")
            return {"softened": _bool(value.get("softened")) or False,
                    "assessment": "" if assessment is None else str(assessment)}
        if isinstance(value, str):
            return {"softened": False, "assessment": value}
        flag = _bool(value)
        return _INVALID if flag is None else {"softened": flag, "assessment": ""}
    if key == "leverage_sentence":
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return _INVALID
    return value


def _normalize_entry(key: str, entry) -> dict:
    if not isinstance(entry, dict):
        entry = {}
    out = {"found": _bool(entry.get("found")) or False,
           "value": None,
           "quote": _text(entry.get("quote")),
           "location": _text(entry.get("location"))}
    raw = entry.get("value")
    value = _coerce_value(key, raw)
    if value is _INVALID:
        out["found"] = False
        out["raw_value"] = raw
    else:
        out["value"] = value
    if out["found"] and out["value"] is None:
        out["found"] = False
    return out


def parse_extraction_json(raw: str, keys: list[str] | None = None) -> dict:
    """Defensive parse: strip stray code fences, isolate the outermost JSON
    object, json.loads, then normalize every key in `keys` to
    {found, value, quote, location} with typed values (plus raw_value when
    the model's value could not be coerced). Raises ValueError on failure."""
    keys = SCHEMA_KEYS if keys is None else keys
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response.")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Model response is not a JSON object.")
    return {key: _normalize_entry(key, data.get(key)) for key in keys}


# ============================================================================
# Fiscal quarters
# ============================================================================

def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _months_before(d: dt.date, months: int) -> dt.date:
    """d minus `months` calendar months; a month-end stays a month-end."""
    year, month0 = divmod(d.year * 12 + d.month - 1 - months, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    is_month_end = d.day == calendar.monthrange(d.year, d.month)[1]
    return dt.date(year, month, last if is_month_end else min(d.day, last))


def _quarter_ends_around(d: dt.date, fye_month: int) -> list[dt.date]:
    """Fiscal quarter-end month-ends in the years around d (quarter-end
    months are fye_month, fye_month-3, -6, -9 mod 12)."""
    months = {(fye_month - 1 - 3 * k) % 12 + 1 for k in range(4)}
    return sorted(_month_end(y, m) for y in (d.year - 1, d.year, d.year + 1)
                  for m in months)


def period_end(filing: dict, fye_month: int = 12) -> dt.date:
    """Fiscal quarter-end date the filing's numbers describe.

    10-Q/10-K(/A): reportDate is the period end -> snapped to the nearest
    fiscal quarter-end month-end (handles 52/53-week calendars that end a few
    days off the month-end).
    8-K(/A): reportDate is the press-release/event date, so the numbers
    describe the latest fiscal quarter that ended strictly before it.
    Without a reportDate the filingDate is used; a filing date always comes
    after the period it reports on, so the "strictly before" rule applies.
    Raises ValueError when both dates are missing.
    """
    report_date = (filing.get("reportDate") or "").strip()
    filing_date = (filing.get("filingDate") or "").strip()
    if not report_date and not filing_date:
        raise ValueError(f"filing {filing.get('accessionNumber')} has neither a "
                         f"reportDate nor a filingDate; cannot derive its quarter.")
    d = dt.date.fromisoformat(report_date or filing_date)
    candidates = _quarter_ends_around(d, fye_month)
    if filing.get("form", "").startswith("8-K") or not report_date:
        return max(c for c in candidates if c < d)
    return min(candidates, key=lambda c: (abs((c - d).days), c))


def quarter_label(pe: dt.date, fye_month: int = 12) -> str:
    """'YYYY-Qn' where YYYY is the fiscal year (the calendar year in which it
    ends) and n the fiscal quarter. For fye_month=12 these are calendar
    quarters."""
    n = ((pe.month - fye_month - 1) % 12) // 3 + 1
    year = pe.year if pe.month <= fye_month else pe.year + 1
    return f"{year}-Q{n}"


def derive_quarter(filing: dict, fye_month: int = 12) -> tuple[str, dt.date]:
    """(quarter label, period-end date) for the filing."""
    pe = period_end(filing, fye_month)
    quarter = quarter_label(pe, fye_month)
    print(f"Derived quarter {quarter} (period end {pe}, fiscal year ends month "
          f"{fye_month}) from form={filing.get('form')} "
          f"reportDate={filing.get('reportDate') or 'n/a'}.")
    return quarter, pe


def prev_quarter(quarter: str) -> str:
    year, q = quarter.split("-Q")
    year, q = int(year), int(q) - 1
    if q == 0:
        year, q = year - 1, 4
    return f"{year}-Q{q}"


# ============================================================================
# History (namespaced by CIK, quarter-keyed, per-field provenance)
# ============================================================================
# File shape: {"<10-digit CIK>": {"<YYYY-Qn>": record, ...}, ...}

def load_history() -> dict:
    """The whole history file; {} when missing, empty, or whitespace-only."""
    if HISTORY_PATH.exists():
        text = HISTORY_PATH.read_text().strip()
        if text:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            print("WARNING: history.json is not a JSON object; starting fresh.")
    return {}


def company_history(history: dict, cik: str) -> dict:
    """The quarter-keyed records for one company (created if missing)."""
    return history.setdefault(cik, {})


def save_history(history: dict) -> None:
    HISTORY_PATH.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")


def _is_periodic(form: str | None) -> bool:
    return (form or "").startswith(("10-Q", "10-K"))


def _recency(filing_date: str | None, form: str | None) -> tuple[str, int]:
    """Sort key for 'which filing is newer': filing date, then a 10-Q/10-K
    beats an 8-K filed the same day."""
    return (filing_date or "", 1 if _is_periodic(form) else 0)


def _empty_entry() -> dict:
    return {"found": False, "value": None, "quote": None, "location": None}


def merge_into_history(records: dict, quarter: str, extraction: dict,
                       filing: dict, keys: list[str] | None = None) -> dict:
    """Merge one filing's extraction into the quarter's record.

    Policy: a found=true value overwrites an existing found=true value only
    when the incoming filing is not older (filing date; on the same date a
    10-Q/10-K beats an 8-K), so the 10-Q refining an earlier earnings 8-K
    wins, but re-processing an old 8-K never clobbers the 10-Q. found=false
    only fills a slot that has nothing yet. Per-field provenance records
    which filing supplied each value.
    """
    keys = SCHEMA_KEYS if keys is None else keys
    record = records.setdefault(quarter, {"quarter": quarter, "fields": {},
                                          "sources": []})
    provenance = {"source_form": filing.get("form"),
                  "accession": filing.get("accessionNumber"),
                  "filing_date": filing.get("filingDate")}
    incoming = _recency(filing.get("filingDate"), filing.get("form"))
    for key in keys:
        new = dict(extraction.get(key) or _empty_entry(), **provenance)
        old = record["fields"].get(key)
        if new.get("found"):
            if old and old.get("found"):
                if incoming < _recency(old.get("filing_date"), old.get("source_form")):
                    print(f"History merge: keeping newer {old.get('source_form')} "
                          f"{old.get('accession')} value for {quarter}/{key} "
                          f"(incoming {provenance['source_form']} "
                          f"{provenance['accession']} is older).")
                    continue
                print(f"History merge: {quarter}/{key} overwritten by "
                      f"{provenance['source_form']} {provenance['accession']} "
                      f"(was from {old.get('source_form')}).")
            record["fields"][key] = new
        elif old is None:
            record["fields"][key] = new
    src = {"form": filing.get("form"), "accession": filing.get("accessionNumber"),
           "filingDate": filing.get("filingDate"),
           "reportDate": filing.get("reportDate")}
    if src not in record["sources"]:
        record["sources"].append(src)
    return record


def field_value(record: dict, key: str):
    entry = (record or {}).get("fields", {}).get(key)
    if entry and entry.get("found"):
        return entry.get("value")
    return None


def field_quote(record: dict, key: str) -> str | None:
    entry = (record or {}).get("fields", {}).get(key)
    return entry.get("quote") if entry else None


def prior_organic(records: dict, baselines: dict, quarter: str):
    """Previous quarter's core organic growth: history first, then the
    optional baselines.organic_growth_history list."""
    pq = prev_quarter(quarter)
    val = _num(field_value(records.get(pq), "organic_growth_core_pct"))
    if val is not None:
        return pq, val, "history"
    for row in (baselines or {}).get("organic_growth_history") or []:
        if isinstance(row, dict) and row.get("quarter") == pq:
            val = _num(row.get("organic_core_pct"))
            if val is not None:
                return pq, val, "baselines"
    return pq, None, None


# ============================================================================
# Rules engine
# ============================================================================
# Stable rule ids (keys of PLAIN below):
#   red:    two_neg_organic, buyback_suspended, buyback_below_floor,
#           leverage_high, goodwill_impairment
#   yellow: segment_lag, guidance_softened, margin_floor, goodwill_cushion,
#           subtraction_growth
#   watch:  perimeter_changed
RULE_IDS = [
    "two_neg_organic", "buyback_suspended", "buyback_below_floor",
    "leverage_high", "goodwill_impairment", "segment_lag", "guidance_softened",
    "margin_floor", "goodwill_cushion", "perimeter_changed", "subtraction_growth",
]


def _as_bool(value) -> bool:
    if isinstance(value, dict):  # {"softened": ...} / {"impaired": ...}
        return bool(_bool(value.get("softened")) or _bool(value.get("impaired")))
    return _bool(value) is True


def _threshold(cfg: dict, key: str):
    """A numeric threshold from cfg["thresholds"], or None (rule skipped)."""
    raw = _thresholds(cfg).get(key)
    value = _num(raw)
    if raw is not None and value is None:
        print(f"WARNING: thresholds.{key} = {raw!r} is not a number; "
              f"that rule is skipped.")
    return value


def _parse_month_day(raw) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})\s*", raw) if isinstance(raw, str) else None
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        dt.date(2000, month, day)  # leap year, so 02-29 is accepted
    except ValueError:
        return None
    return month, day


def _goodwill_test_note(profile: dict, form: str, pe: dt.date | None) -> str | None:
    """Standing note when the annual goodwill test date falls inside the
    quarter a 10-Q/10-K covers: (period_end - 3 months, period_end]."""
    raw = profile.get("goodwill_test_date")
    if not raw or pe is None or not _is_periodic(form):
        return None
    md = _parse_month_day(raw)
    if md is None:
        print(f"WARNING: profile.goodwill_test_date {raw!r} is not MM-DD; "
              f"standing note skipped.")
        return None
    month, day = md
    start = _months_before(pe, 3)
    for year in (pe.year - 1, pe.year):
        test_day = dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))
        if start < test_day <= pe:
            return (f"Standing note: this {form} covers "
                    f"{calendar.month_name[month]} {day} — the company's annual "
                    f"goodwill impairment test date. Early signs of a write-down "
                    f"usually show up in this filing's goodwill footnote first.")
    return None


def run_rules(quarter: str, record: dict, records: dict, baselines: dict,
              cfg: dict, form: str, period_end: dt.date | None
              ) -> tuple[list[dict], list[str]]:
    """Return (fired, notes). fired: [{id, severity, rule, detail, quote}]
    with severity in {'red','yellow','watch'}. notes: informational lines.

    Numeric thresholds come only from cfg["thresholds"]; a missing/null
    threshold skips that rule. records: this company's quarter-keyed history.
    """
    baselines = baselines or {}
    profile = _profile(cfg)
    core_label = (profile.get("core_label") or "").strip()
    margin_label = (profile.get("margin_label") or "").strip() or DEFAULT_MARGIN_LABEL
    seg = _segment(profile)
    buyback_base = baselines.get("buyback")
    buyback_base = buyback_base if isinstance(buyback_base, dict) else {}

    fired: list[dict] = []
    notes: list[str] = []
    f = lambda key: field_value(record, key)
    n = lambda key, rec=record: _num(field_value(rec, key))
    q = lambda key: field_quote(record, key)

    def fire(rule_id: str, severity: str, rule: str, detail: str = "",
             quote: str | None = None) -> None:
        fired.append({"id": rule_id, "severity": severity, "rule": rule,
                      "detail": detail, "quote": quote})

    # --- RED: two consecutive quarters of negative core organic growth ------
    cur_org = n("organic_growth_core_pct")
    pq, prev_org, prev_src = prior_organic(records, baselines, quarter)
    if cur_org is not None and cur_org < 0:
        if prev_org is not None and prev_org < 0:
            fire("two_neg_organic", "red",
                 "Two consecutive quarters of negative core organic growth",
                 f"{quarter}: {cur_org}% ; {pq}: {prev_org}% (from {prev_src})",
                 q("organic_growth_core_pct"))
        elif prev_org is None:
            notes.append(f"Core organic growth was negative this quarter "
                         f"({cur_org}%), but no {pq} figure is stored to compare "
                         f"against, so the two-consecutive-quarters rule could "
                         f"not be evaluated. Next quarter decides it.")

    # --- RED: buyback suspended (unconditional) or below floor --------------
    if _as_bool(f("buyback_suspension_language")):
        fire("buyback_suspended", "red", "Buyback suspended or reduced",
             "", q("buyback_suspension_language"))
    floor = _threshold(cfg, "buyback_red_floor_qtr_m")
    bb = n("buyback_dollars_qtr_m")
    if floor is not None and bb is not None and bb < floor:
        run_rate = buyback_base.get("run_rate_qtr_m")
        detail = f"Quarterly repurchases ${bb}M < red floor ${floor}M"
        detail += f" (run-rate ~${run_rate}M)." if run_rate is not None else "."
        fire("buyback_below_floor", "red", "Quarterly buyback below floor",
             detail, q("buyback_dollars_qtr_m"))

    # --- RED: leverage above threshold --------------------------------------
    red_x = _threshold(cfg, "leverage_red_x")
    lev = n("leverage_ratio_x")
    if red_x is not None and lev is not None and lev > red_x:
        fire("leverage_high", "red", "Leverage above threshold",
             f"Leverage {lev}x > {red_x}x.",
             q("leverage_sentence") or q("leverage_ratio_x"))

    # --- RED: goodwill impairment charge (backstop to the Item 2.06 path) ---
    gi = f("goodwill_impairment_charge")
    if _as_bool(gi):
        amount = _num(gi.get("amount_m")) if isinstance(gi, dict) else None
        fire("goodwill_impairment", "red", "Goodwill impairment charge reported",
             f"Amount: ${amount}M" if amount is not None
             else "Charge reported (amount not parsed).",
             q("goodwill_impairment_charge"))

    # --- YELLOW: segment lagging group, two quarters running ----------------
    if seg:
        seg_name = seg["name"].strip()
        prev_rec = records.get(pq)
        im, grp = n("segment_organic_pct"), n("group_organic_pct")
        p_im = n("segment_organic_pct", prev_rec)
        p_grp = n("group_organic_pct", prev_rec)
        if None not in (im, grp, p_im, p_grp) and im < grp and p_im < p_grp:
            fire("segment_lag", "yellow",
                 f"{seg_name} lagging group organic growth for two consecutive "
                 f"quarters",
                 f"{quarter}: {seg_name} {im}% < group {grp}% ; "
                 f"{pq}: {seg_name} {p_im}% < group {p_grp}%.",
                 q("segment_organic_pct"))

    # --- YELLOW: guidance softened ------------------------------------------
    if _as_bool(f("guidance_language")):
        fire("guidance_softened", "yellow", "Guidance softened",
             "", q("guidance_language"))

    # --- YELLOW: margin below threshold -------------------------------------
    margin_floor = _threshold(cfg, "margin_yellow_pct")
    margin = n("adj_margin_pct")
    if margin_floor is not None and margin is not None and margin < margin_floor:
        fire("margin_floor", "yellow",
             f"{margin_label[0].upper() + margin_label[1:]} below floor",
             f"{margin}% < {margin_floor}%.", q("adj_margin_pct"))

    # --- YELLOW: goodwill cushion compression (annual) ----------------------
    cushion = n("goodwill_cushion_pct")
    if cushion is not None:
        prior_cushion, prior_q = None, None
        walk = prev_quarter(quarter)
        for _ in range(8):  # scan up to two years back
            val = n("goodwill_cushion_pct", records.get(walk))
            if val is not None:
                prior_cushion, prior_q = val, walk
                break
            walk = prev_quarter(walk)
        if prior_cushion is not None and cushion < prior_cushion:
            fire("goodwill_cushion", "yellow", "Goodwill cushion compression",
                 f"Minimum cushion {cushion}% < prior {prior_cushion}% ({prior_q}).",
                 q("goodwill_cushion_pct"))
        else:
            notes.append(f"Goodwill write-down safety buffer (thinnest unit): {cushion}%"
                         + (f" (was {prior_cushion}% in {prior_q})"
                            if prior_cushion is not None
                            else " — first year on record, nothing to compare against yet."))

    # --- WATCH: perimeter changed -------------------------------------------
    if _as_bool(f("perimeter_changed")):
        fire("perimeter_changed", "watch",
             f"{core_label or 'Organic-growth'} perimeter changed",
             "", q("perimeter_changed"))

    # --- YELLOW: subtraction-growth counter ---------------------------------
    def subtraction(rec: dict | None) -> bool | None:
        tot = n("total_reported_revenue_change_pct", rec)
        org = n("organic_growth_core_pct", rec)
        if tot is None or org is None:
            return None
        return tot < 0 and org > 0

    current = subtraction(record)
    count = 0
    if current:
        count = 1
        walk = pq
        while True:
            hit = subtraction(records.get(walk))
            if not hit:  # None (no data) or False both end the streak
                break
            count += 1
            walk = prev_quarter(walk)
    escalate_at = _threshold(cfg, "subtraction_growth_escalate_at")
    if escalate_at is not None and count >= escalate_at:
        fire("subtraction_growth", "yellow", "Subtraction growth (escalated)",
             f"{count} consecutive quarter(s) of total reported revenue declining "
             f"while organic is positive (escalation threshold: {escalate_at:g}).")
    elif current is None:
        notes.append("Subtraction-growth streak: not evaluable this quarter "
                     "(organic growth or total reported revenue change not "
                     "reported).")
    else:
        notes.append(f"Subtraction-growth streak: {count} consecutive quarter(s) "
                     f"of total reported revenue falling while organic growth "
                     f"stayed positive"
                     + (f" (escalates to a yellow flag at {escalate_at:g})."
                        if escalate_at is not None else "."))

    # --- INFO: share count / authorization / market-cap-to-book -------------
    base_shares = _num(baselines.get("shares_outstanding_m"))
    shares = n("shares_outstanding_m")
    if shares is not None and base_shares:
        delta_pct = (shares - base_shares) / base_shares * 100
        notes.append(f"Share count: {shares}M vs {base_shares}M at baseline "
                     f"({delta_pct:+.1f}%).")
    auth = n("buyback_remaining_authorization_m")
    if auth is not None:
        notes.append(f"Remaining buyback authorization: ${auth}M.")
    else:
        base_auth = buyback_base.get("authorization_remaining_b")
        if base_auth is not None:
            asof = baselines.get("baseline_asof")
            notes.append(f"Remaining buyback authorization: not stated in this "
                         f"filing (last known: ${base_auth}B"
                         + (f" as of {asof})." if asof else ")."))
    ticker = ((cfg or {}).get("company") or {}).get("ticker") or ""
    notes.append(market_cap_to_book_note(record, ticker))

    # --- Standing note: quarter containing the annual goodwill test ---------
    note = _goodwill_test_note(profile, form, period_end)
    if note:
        notes.append(note)

    return fired, notes


def market_cap_to_book_note(record: dict, ticker: str) -> str:
    """Impairment-pressure gauge: live Finnhub quote x extracted shares /
    extracted book equity. Informational only; degrades gracefully."""
    shares = _num(field_value(record, "shares_outstanding_m"))
    book_b = _num(field_value(record, "book_equity_b"))
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    missing = [name for name, v in
               [("a live price", api_key), ("the ticker", ticker),
                ("the share count", shares),
                ("the book-equity number (arrives with the 10-Q)", book_b)]
               if not v]
    if missing:
        return ("Market-value-vs-book gauge (impairment pressure): skipped — "
                "missing " + ", ".join(missing) + ".")
    try:
        resp = requests.get("https://finnhub.io/api/v1/quote",
                            params={"symbol": ticker, "token": api_key}, timeout=30)
        resp.raise_for_status()
        price = resp.json().get("c")
        if not price:
            raise ValueError(f"empty quote for {ticker}")
        mcap_m = price * shares
        ratio = mcap_m / (book_b * 1000.0)
        return (f"Market value vs. book value: {ratio:.2f}x "
                f"(market ${mcap_m/1000:.2f}B vs book ${book_b}B). The closer "
                f"this falls toward 1.0x, the harder a goodwill write-down "
                f"becomes to avoid.")
    except Exception as exc:
        # requests puts the full URL (including token=<key>) in its error
        # messages, and this note is emailed: mask the key.
        detail = str(exc).replace(api_key, "***")
        return f"Market-value-vs-book gauge: price lookup failed ({detail}); skipped."


# ============================================================================
# Plain-English layer
# ============================================================================
# Rule id -> (headline template, meaning template). Templates are filled from
# plain_context(cfg): {company}, {core_label}, {segment_name}, {segment_plain},
# {margin_label}, {margin_floor}, plus two optional clauses that include their
# own punctuation and are empty when not configured: {segment_share} (e.g.
# " (about 51.5% of Core Operations)") and {phrases} (e.g.
# ' ("4% organic" and "$900M synergies in 2026")'). A missing key renders as
# an empty string, never an error. profile.plain_overrides[rule_id] =
# [headline, meaning] replaces an entry verbatim.
PLAIN = {
    "two_neg_organic": (
        "Organic growth negative two quarters in a row",
        "Even after stripping out currency swings and acquisitions or "
        "disposals, {company}'s {core_label} got smaller for two straight "
        "quarters. One negative quarter can be noise; two in a row usually "
        "points to a real slowdown in demand rather than a timing quirk."),
    "buyback_suspended": (
        "Share buyback paused or cut",
        "The filing contains language suspending, pausing, or reducing the "
        "share repurchase program. Companies usually protect buybacks until "
        "cash gets tight, so a pause often signals weaker cash flow or new "
        "priorities for that cash (such as paying down debt)."),
    "buyback_below_floor": (
        "Buybacks slowed below the configured floor",
        "{company} spent less on share repurchases this quarter than the "
        "floor set in filings_config.json. A sharp slowdown can mean less "
        "spare cash, a shift in capital allocation, or simply timing — the "
        "filing's buyback discussion usually says which."),
    "leverage_high": (
        "Debt above the configured ceiling",
        "Debt relative to earnings (the leverage ratio the company reports) "
        "is above the threshold set in filings_config.json. Higher leverage "
        "leaves less room for dividends and buybacks and makes a weak "
        "stretch harder to absorb."),
    "goodwill_impairment": (
        "Write-down of past acquisitions",
        "{company} reported a goodwill impairment: it formally recognized "
        "that businesses it acquired are worth less than their carrying "
        "value. The charge is non-cash, but it confirms that the returns "
        "expected from those deals are not materializing."),
    "segment_lag": (
        "{segment_plain} lagging the rest of the company — two quarters running",
        "{segment_name}{segment_share} grew more slowly than the company as a "
        "whole for two straight quarters. A persistent gap in a large segment "
        "can reflect client or market-share losses that reach the headline "
        "numbers only later."),
    "guidance_softened": (
        "Guidance softened",
        "Compared with earlier guidance{phrases}, this filing lowers, narrows, "
        "drops, or stops repeating it. Companies rarely withdraw targets "
        "loudly; vaguer language is often the first sign a target may be "
        "missed."),
    "margin_floor": (
        "Margin below the configured floor",
        "{company}'s {margin_label} came in under the {margin_floor}% floor set "
        "in filings_config.json, so less of each revenue dollar reached "
        "operating profit. Persistently thin margins can reflect pricing "
        "pressure, rising costs, or integration expenses."),
    "goodwill_cushion": (
        "Goodwill safety buffer got thinner",
        "In its annual goodwill test, {company} discloses how far each "
        "reporting unit's estimated fair value exceeds its carrying value. "
        "The smallest of those buffers shrank versus the prior test, so a "
        "write-down moved a step closer."),
    "perimeter_changed": (
        "Definition of '{core_label}' changed",
        "The scope of what counts toward {core_label} changed versus the prior "
        "presentation (for example, businesses moved in or out). This "
        "quarter's organic growth figure is therefore not directly comparable "
        "with earlier quarters, and trend-based rules such as the "
        "two-negative-quarters check deserve a manual re-check."),
    "subtraction_growth": (
        "Organic growth positive while total revenue keeps falling",
        "For several consecutive quarters total reported revenue declined "
        "while the organic growth figure stayed positive. The gap usually "
        "comes from disposals, currency, or changed definitions rather than "
        "the underlying business; once it persists this long it is worth "
        "checking what the organic figure excludes."),
}


class _BlankMissing(dict):
    def __missing__(self, key):
        return ""


def _fill(template: str, ctx: dict) -> str:
    try:
        return template.format_map(_BlankMissing(ctx))
    except (ValueError, IndexError, AttributeError):
        return template


def plain_context(cfg: dict | None) -> dict:
    """Values for the PLAIN templates, built from filings_config.json."""
    company = (cfg or {}).get("company") or {}
    profile = _profile(cfg)
    core_label = (profile.get("core_label") or "").strip() or "core business"
    ctx = {"company": company.get("name") or "the company",
           "core_label": core_label,
           "margin_label": (profile.get("margin_label") or "").strip()
                           or DEFAULT_MARGIN_LABEL}
    seg = _segment(profile)
    if seg:
        ctx["segment_name"] = seg["name"].strip()
        ctx["segment_plain"] = (seg.get("plain_name") or "").strip() or ctx["segment_name"]
        share = _num(seg.get("share_of_core_pct"))
        if share is not None:
            ctx["segment_share"] = f" (about {share:g}% of {core_label})"
    margin_floor = _num(_thresholds(cfg).get("margin_yellow_pct"))
    if margin_floor is not None:
        ctx["margin_floor"] = f"{margin_floor:g}"
    phrases = _str_list(profile.get("guidance_reference_phrases"))
    if phrases:
        ctx["phrases"] = f" ({_join_quoted(phrases)})"
    return ctx


def plain_text(rule_id: str, cfg: dict | None, fallback_rule: str = "") -> tuple[str, str]:
    """(headline, meaning) for a fired rule."""
    override = _profile(cfg).get("plain_overrides")
    override = override.get(rule_id) if isinstance(override, dict) else None
    if (isinstance(override, (list, tuple)) and len(override) == 2
            and all(isinstance(s, str) for s in override)):
        return override[0], override[1]
    if override is not None:
        print(f"WARNING: profile.plain_overrides.{rule_id} must be "
              f"[headline, meaning]; using the default text.")
    template = PLAIN.get(rule_id)
    if template is None:
        return fallback_rule or rule_id, "(no plain-language note for this rule)"
    ctx = plain_context(cfg)
    return _fill(template[0], ctx), _fill(template[1], ctx)


# ============================================================================
# Review email
# ============================================================================

def build_review_email(quarter: str, record: dict, fired: list[dict],
                       notes: list[str], filing: dict, cik: str,
                       ticker: str, cfg: dict | None = None
                       ) -> tuple[str, str, str]:
    """Return (subject_without_emoji, body, severity).

    Body is deliberately short and plain: fired triggers translated into
    plain English, the informational block, and links. The full extracted
    checklist (every field, value, quote, and source) is NOT emailed — it
    lives in filings/history.json in the repo for auditing.
    """
    reds = [x for x in fired if x["severity"] == "red"]
    yellows = [x for x in fired if x["severity"] == "yellow"]
    watches = [x for x in fired if x["severity"] == "watch"]

    if reds:
        severity, tail = "red", f"{len(reds)} red trigger(s)"
    elif yellows:
        severity, tail = "yellow", f"{len(yellows)} yellow trigger(s)"
    else:
        severity, tail = "info", "no triggers"
    subject = f"{ticker} {quarter.replace('-', ' ')} review draft — {tail}"

    sev_emoji = {"red": "\U0001f534", "yellow": "\U0001f7e1", "watch": "⚪"}
    lines = [f"Quarterly review draft — {ticker} {quarter}",
             f"From: {filing.get('form')} filed {filing.get('filingDate')}.", ""]

    lines.append("== WHAT FIRED ==")
    if fired:
        for x in reds + yellows + watches:
            headline, meaning = plain_text(x.get("id", ""), cfg, x.get("rule", ""))
            lines.append(f"{sev_emoji[x['severity']]} {headline}")
            lines.append(f"   Meaning: {meaning}")
            if x.get("detail"):
                lines.append(f"   The number: {x['detail']}")
            if x.get("quote"):
                lines.append(f"   From the filing: \"{x['quote']}\"")
            lines.append("")
    else:
        lines.append("No red, yellow, or watch triggers fired this quarter.")
        lines.append("")

    lines.append("== WORTH KNOWING ==")
    lines.extend(f"- {n}" for n in notes)
    lines.append("")

    base = archive_base(cik, filing["accessionNumber"])
    lines.append("== LINKS ==")
    lines.append(f"- Filing index: {base}")
    lines.append(f"- Document: {base}{filing.get('primaryDocument', '')}")
    lines.append(f"- Every extracted number, quote, and source: "
                 f"filings/history.json (company {cik}, entry '{quarter}').")
    lines.append("")
    lines.append("Values are LLM-extracted drafts. Verify each fired trigger "
                 "against the filing before acting.")
    lines.append("This is automated output, not investment advice.")
    return subject, "\n".join(lines), severity


# ============================================================================
# Pipeline entry point (called by the watcher)
# ============================================================================

def _failure_links(filing: dict, base: str) -> str:
    return (f"Filing index: {base}\n"
            f"Primary document: {base}{filing.get('primaryDocument', '')}\n\n"
            f"To retry automatically: Actions > filings-watcher > Run "
            f"workflow with force_accession = {filing.get('accessionNumber')}")


def run_extraction_pipeline(filing: dict, cfg: dict, baselines: dict,
                            contact_email: str) -> None:
    """Extract, merge history, run rules, email the review draft.

    Never raises except when an email cannot be sent: a failure while
    extracting (stage 1) or while building the review (stage 2) is reported
    by a yellow "review the filing manually" email instead, so the filing
    alert chain is never silently broken. If sending that email — or the
    review email itself (stage 3) — fails, the exception propagates and the
    watcher leaves the filing unseen so the next run retries it.
    """
    company = (cfg or {}).get("company") or {}
    cik = str(company.get("cik", "")).zfill(10)
    ticker = company.get("ticker") or ""
    label = ticker or company.get("name") or cik
    form = filing.get("form", "")
    try:
        base = archive_base(cik, filing.get("accessionNumber", ""))
    except Exception:
        base = f"{SEC_ARCHIVE_BASE}/"

    # --- Stage 1: retrieve + Claude + parse ---------------------------------
    try:
        profile = _profile(cfg)
        keys = schema_keys(profile)
        session = sec_session(contact_email, user_agent_tool_name(cfg))
        text = prepare_document_text(filing, cik, session, anchor_phrases(profile))
        extraction = call_claude(text, form, cfg, keys)
    except Exception as exc:
        print("ERROR: extraction pipeline failed:")
        traceback.print_exc()
        send_email(
            subject=f"{label} {form}: extraction failed — review the filing manually",
            body=(f"A new {form} was filed on {filing.get('filingDate')} "
                  f"(report date {filing.get('reportDate') or 'n/a'}), but the "
                  f"LLM extraction step failed:\n\n  {exc}\n\n"
                  f"The filing still needs a manual review.\n\n"
                  f"{_failure_links(filing, base)}"),
            severity="yellow",
        )
        return

    # --- Stage 2: quarter, history, rules, email body -----------------------
    try:
        quarter, pe = derive_quarter(filing, fye_month_of(cfg))
        history = load_history()
        records = company_history(history, cik)
        record = merge_into_history(records, quarter, extraction, filing, keys)
        save_history(history)
        print(f"History updated for {cik}/{quarter} "
              f"({len(record['sources'])} source filings).")
        fired, notes = run_rules(quarter, record, records, baselines, cfg, form, pe)
        subject, body, severity = build_review_email(
            quarter, record, fired, notes, filing, cik, label, cfg)
    except Exception as exc:
        print("ERROR: review step failed after extraction:")
        traceback.print_exc()
        send_email(
            subject=f"{label} {form}: review step failed — review the filing manually",
            body=(f"The LLM extraction for the {form} filed on "
                  f"{filing.get('filingDate')} (report date "
                  f"{filing.get('reportDate') or 'n/a'}) succeeded, but a later "
                  f"step (quarter derivation, history update, rules, or building "
                  f"the review email) failed:\n\n  {exc}\n\n"
                  f"The filing still needs a manual review.\n\n"
                  f"{_failure_links(filing, base)}"),
            severity="yellow",
        )
        return

    # --- Stage 3: send the review draft (SMTP failure propagates) -----------
    send_email(subject=subject, body=body, severity=severity)
