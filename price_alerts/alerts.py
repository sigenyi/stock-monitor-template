"""Price alerts: twice per trading day, fetch Finnhub quotes for configured
stocks, evaluate numeric triggers, and send one consolidated email if any
fired. Stateless by design — no de-duplication; a condition that stays true
alerts on every run.

Run from the repo root as:  python -m price_alerts.alerts
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from common.emailer import require_env, send_email

CONFIG_PATH = Path(__file__).with_name("config.json")
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
EASTERN = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

# Target windows (ET, Mon-Fri). GitHub cron is UTC and ignores US daylight
# saving, so the workflow schedules four crons: for each window, one that
# lands on the window start during EDT (summer) and one that lands on it
# during EST (winter). On any given day, one cron of each pair is the
# "wrong-season" fire, exactly 60 minutes off. The script gates itself so
# only the correct-season fire does any work.
#
# Cron-aware gate (scheduled runs). The workflow passes the cron expression
# that triggered the run as the SCHEDULE environment variable
# (${{ github.event.schedule }}). From it we compute the intended fire time
# (today, UTC, at the cron's H:M — or yesterday if that is still in the
# future), convert it to ET, and proceed only if BOTH hold:
#   (a) the fire time in ET is exactly the start of one of WINDOWS, i.e. this
#       is the correct-season cron. The wrong-season cron resolves to 09:45,
#       11:45, 15:05 or 17:05 ET and is rejected however late it runs. This
#       matters because GitHub often starts scheduled runs late: without
#       this check, a winter 14:45 UTC fire (09:45 EST) delayed by an hour
#       would land inside the 10:45 window and send a duplicate email.
#   (b) "now" (ET) is inside that window's accepted span on the fire date.
#
# Grace: scheduled runs are best-effort and often minutes late, so we accept
# up to ~20 minutes past each window's end — BUT the total accepted span is
# hard-capped at 45 minutes past the window's start, so a very late run
# (long after the market moment it was meant for) is skipped.
#   Morning 10:45-11:15 (30 min) -> grace capped to 15 min -> accept 10:45-11:30
#   Afternoon 16:05-16:15 (10 min) -> full 20 min grace   -> accept 16:05-16:35
#
# Fallback: if SCHEDULE is missing or unparseable (or the run is local), we
# fall back to a pure wall-clock check, in_run_window(now_et). The 45-minute
# cap keeps an on-time wrong-season fire (exactly 60 minutes off) outside
# every window, but a wrong-season fire delayed into a window would pass.
# Manual runs (workflow_dispatch) always bypass the gate.
WINDOWS = [
    (dt.time(10, 45), dt.time(11, 15)),
    (dt.time(16, 5), dt.time(16, 15)),
]
GRACE = dt.timedelta(minutes=20)
MAX_SPAN_FROM_START = dt.timedelta(minutes=45)

SLEEP_BETWEEN_CALLS_S = 1.1  # far under Finnhub's 60 calls/minute free limit

TRIGGER_DESCRIPTIONS = {
    "price_above": "price above {value}",
    "price_below": "price below {value}",
    "percent_change_above": "percent change above {value}%",
    "percent_change_below": "percent change below {value}%",
}

AUTH_FAILURE_STATUSES = (401, 403)


def _now_utc() -> dt.datetime:
    """Current time as an aware UTC datetime (tests monkeypatch this)."""
    return dt.datetime.now(UTC)


def _window_latest(start: dt.datetime, end: dt.datetime) -> dt.datetime:
    """Latest accepted time for a window: end + grace, capped from start."""
    return min(end + GRACE, start + MAX_SPAN_FROM_START)


def in_run_window(now_et: dt.datetime) -> bool:
    """True if now_et falls inside a target window (including capped grace)."""
    today = now_et.date()
    for start_t, end_t in WINDOWS:
        start = dt.datetime.combine(today, start_t, tzinfo=EASTERN)
        end = dt.datetime.combine(today, end_t, tzinfo=EASTERN)
        if start <= now_et <= _window_latest(start, end):
            return True
    return False


def parse_cron_time(schedule: str | None) -> dt.time | None:
    """Return the fixed UTC H:M of a cron like "M H * * ...", else None.

    Only plain integer minute/hour fields are understood; anything else
    (ranges, steps, lists, wrong field count) returns None so the caller
    falls back to the wall-clock window check.
    """
    fields = (schedule or "").split()
    if len(fields) != 5:
        return None
    minute_s, hour_s = fields[0], fields[1]
    if not (minute_s.isdigit() and hour_s.isdigit()):
        return None
    minute, hour = int(minute_s), int(hour_s)
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return None
    return dt.time(hour, minute)


def intended_fire_utc(cron_time: dt.time, now_utc: dt.datetime) -> dt.datetime:
    """The most recent moment (<= now) the cron at cron_time (UTC) was due."""
    fire = dt.datetime.combine(now_utc.date(), cron_time, tzinfo=UTC)
    if fire > now_utc:
        fire -= dt.timedelta(days=1)
    return fire


def scheduled_run_allowed(cron_time: dt.time, now_utc: dt.datetime) -> tuple[bool, str]:
    """Cron-aware gate for scheduled runs. Returns (allowed, reason)."""
    fire_et = intended_fire_utc(cron_time, now_utc).astimezone(EASTERN)
    now_et = now_utc.astimezone(EASTERN)
    for start_t, end_t in WINDOWS:
        if fire_et.time() != start_t:
            continue
        start = dt.datetime.combine(fire_et.date(), start_t, tzinfo=EASTERN)
        end = dt.datetime.combine(fire_et.date(), end_t, tzinfo=EASTERN)
        latest = _window_latest(start, end)
        if start <= now_et <= latest:
            return True, (f"cron {cron_time:%H:%M} UTC is the {start_t:%H:%M} ET "
                          f"run and it started within its window.")
        return False, (f"cron {cron_time:%H:%M} UTC is the {start_t:%H:%M} ET run, "
                       f"but it started at {now_et:%Y-%m-%d %H:%M %Z}, outside "
                       f"{start:%H:%M}-{latest:%H:%M} ET on {start:%Y-%m-%d} "
                       f"(GitHub delayed it too long).")
    return False, (f"cron {cron_time:%H:%M} UTC fired at {fire_et:%H:%M %Z}, which is "
                   f"not the start of a run window; this is the other daylight-"
                   f"saving season's cron.")


def validate_config(config: object) -> tuple[dict, list[str]]:
    """Check config.json's shape. Returns (stocks, problems)."""
    if not isinstance(config, dict):
        return {}, [f"top level must be a JSON object, got {type(config).__name__}"]
    stocks = config.get("stocks")
    if stocks is None:
        return {}, []
    if not isinstance(stocks, dict):
        return {}, [f"'stocks' must be an object mapping ticker -> list of "
                    f"triggers, got {type(stocks).__name__}"]

    problems: list[str] = []
    valid_types = ", ".join(TRIGGER_DESCRIPTIONS)
    for symbol, triggers in stocks.items():
        if not isinstance(triggers, list):
            problems.append(f"stocks.{symbol}: must be a list of triggers, "
                            f"got {type(triggers).__name__}")
            continue
        for i, trigger in enumerate(triggers):
            where = f"stocks.{symbol}[{i}]"
            if not isinstance(trigger, dict):
                problems.append(f"{where}: must be an object like "
                                f'{{"type": "price_above", "value": 100}}, '
                                f"got {type(trigger).__name__}")
                continue
            ttype = trigger.get("type")
            # isinstance first: a list/object "type" is unhashable and would
            # crash the dict lookup instead of being reported.
            if not isinstance(ttype, str) or ttype not in TRIGGER_DESCRIPTIONS:
                problems.append(f"{where}: 'type' must be one of {valid_types}; "
                                f"got {ttype!r}")
            value = trigger.get("value")
            # Only floats can be non-finite (1e400 parses as inf); calling
            # math.isfinite on a huge JSON integer raises OverflowError.
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or (isinstance(value, float) and not math.isfinite(value))):
                problems.append(f"{where}: 'value' must be a number "
                                f"(not a string or true/false); got {value!r}")
    return stocks, problems


def evaluate_trigger(trigger: dict, quote: dict) -> bool:
    ttype = trigger.get("type")
    value = trigger.get("value")
    c, dp = quote.get("c"), quote.get("dp")
    if ttype == "price_above":
        return c is not None and c > value
    if ttype == "price_below":
        return c is not None and c < value
    if ttype == "percent_change_above":
        return dp is not None and dp > value
    if ttype == "percent_change_below":
        return dp is not None and dp < value
    print(f"WARNING: unknown trigger type {ttype!r}; skipping.")
    return False


def fetch_quote(symbol: str, api_key: str) -> dict | None:
    resp = requests.get(
        FINNHUB_QUOTE_URL,
        params={"symbol": symbol, "token": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    quote = resp.json()
    # Finnhub returns zeros for unknown/unsupported symbols. Treat a zero/empty
    # current price as "no data" and skip rather than fire a false trigger.
    if not quote or not quote.get("c"):
        print(f"WARNING: no usable quote for {symbol!r} "
              f"(unknown symbol or empty data): {quote!r}. Skipping.")
        return None
    return quote


def _is_auth_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return (isinstance(exc, requests.HTTPError) and response is not None
            and getattr(response, "status_code", None) in AUTH_FAILURE_STATUSES)


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    now_utc = _now_utc()
    now_et = now_utc.astimezone(EASTERN)
    print(f"Run start. ET time: {now_et:%Y-%m-%d %H:%M:%S %Z}; event: {event or 'local'}")

    if event == "workflow_dispatch":
        print("Manual dispatch: bypassing the time gate.")
    else:
        cron_time = parse_cron_time(os.environ.get("SCHEDULE")) if event == "schedule" else None
        if cron_time is not None:
            allowed, reason = scheduled_run_allowed(cron_time, now_utc)
            if not allowed:
                print(f"Skipping: {reason} Exiting cleanly; no API calls, no email.")
                return 0
            print(f"Time gate passed: {reason}")
        else:
            if event == "schedule":
                print(f"WARNING: SCHEDULE is missing or not a plain 'M H * * ...' cron "
                      f"({os.environ.get('SCHEDULE')!r}); falling back to the "
                      f"wall-clock window check.")
            if not in_run_window(now_et):
                print("Outside run windows (10:45-11:30 / 16:05-16:35 ET incl. grace). "
                      "Exiting cleanly; no API calls, no email.")
                return 0

    api_key = require_env("FINNHUB_API_KEY")
    # Validate email secrets up front so a config problem surfaces even on
    # runs where no trigger fires.
    require_env("EMAIL_ADDRESS"); require_env("EMAIL_APP_PASSWORD"); require_env("EMAIL_TO")

    try:
        config = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError) as exc:
        print(f"FATAL: could not read price_alerts/config.json: {exc}")
        return 1
    stocks, problems = validate_config(config)
    if problems:
        print("FATAL: invalid price_alerts/config.json:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if not stocks:
        print("WARNING: no stocks configured in config.json; nothing to do.")
        return 0

    fired: list[dict] = []
    fetch_failures: list[str] = []
    auth_failures: list[str] = []
    for i, (symbol, triggers) in enumerate(stocks.items()):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_CALLS_S)
        try:
            quote = fetch_quote(symbol, api_key)
        except Exception as exc:  # one bad ticker must not abort the run
            fetch_failures.append(symbol)
            if _is_auth_failure(exc):
                auth_failures.append(symbol)
            # requests puts the full URL (including the token) in the message.
            detail = str(exc).replace(api_key, "***")
            print(f"WARNING: quote fetch failed for {symbol!r}: {detail}. Continuing.")
            continue
        if quote is None:
            continue
        c, dp = quote.get("c"), quote.get("dp")
        print(f"{symbol}: c={c} dp={dp}")
        for trigger in triggers:
            try:
                if evaluate_trigger(trigger, quote):
                    desc = TRIGGER_DESCRIPTIONS.get(
                        trigger["type"], trigger["type"]
                    ).format(value=trigger.get("value"))
                    fired.append(
                        {"symbol": symbol, "description": desc,
                         "price": c, "percent_change": dp}
                    )
                    print(f"  TRIGGER FIRED: {symbol} {desc}")
            except Exception as exc:
                print(f"ERROR: evaluating trigger {trigger!r} for {symbol}: {exc}")

    if not fired:
        print("No triggers fired. No email sent.")
    else:
        stamp = _now_utc().astimezone(EASTERN).strftime("%Y-%m-%d %I:%M %p %Z")
        lines = [f"Stock alert run at {stamp}", "", f"{len(fired)} trigger(s) fired:", ""]
        for f in fired:
            dp_txt = f"{f['percent_change']:+.2f}%" if f["percent_change"] is not None else "n/a"
            lines.append(f"- {f['symbol']}: {f['description']} | "
                         f"current price {f['price']} | change {dp_txt}")
        lines += ["", "This alert is stateless: conditions that remain true will "
                      "alert again on the next run."]

        send_email(
            subject=f"Stock Alert: {len(fired)} trigger(s) fired",
            body="\n".join(lines),
            severity="red",
        )

    # Surface failures that would otherwise look like "nothing fired".
    exit_code = 0
    if auth_failures:
        print(f"ERROR: Finnhub refused the request (HTTP 401/403) for "
              f"{', '.join(auth_failures)}. Check the FINNHUB_API_KEY secret; a 403 "
              f"for a single symbol can also mean it needs a paid Finnhub plan.")
        exit_code = 1
    if len(fetch_failures) == len(stocks):
        print(f"ERROR: quote fetch failed for every configured ticker "
              f"({len(fetch_failures)} of {len(stocks)}); no triggers could be checked.")
        exit_code = 1
    elif fetch_failures:
        print(f"WARNING: quote fetch failed for {len(fetch_failures)} of "
              f"{len(stocks)} ticker(s): {', '.join(fetch_failures)}.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
