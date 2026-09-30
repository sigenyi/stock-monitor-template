"""Offline tests for price_alerts.alerts: config validation, the cron-aware
time gate, failure surfacing and the consolidated alert email.

No network: requests.get, send_email and the clock are monkeypatched.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

from price_alerts import alerts

UTC = dt.timezone.utc
API_KEY = "test-finnhub-key"

# Wednesdays well inside each DST season.
SUMMER = dt.date(2026, 7, 15)   # EDT, UTC-4
WINTER = dt.date(2026, 1, 14)   # EST, UTC-5


def utc(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload=None, status: int = 200, symbol: str = "X"):
        self.payload = payload
        self.status_code = status
        self.symbol = symbol

    def raise_for_status(self):
        if self.status_code >= 400:
            # Mirrors requests' real message, which includes the full URL.
            raise requests.HTTPError(
                f"{self.status_code} Client Error: for url: "
                f"{alerts.FINNHUB_QUOTE_URL}?symbol={self.symbol}&token={API_KEY}",
                response=self,
            )

    def json(self):
        return self.payload


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Configure a run: returns a function(config, quotes, now, event, schedule)
    that executes alerts.main() and returns (exit_code, calls, emails)."""
    monkeypatch.setenv("FINNHUB_API_KEY", API_KEY)
    monkeypatch.setenv("EMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "test-app-password")
    monkeypatch.setenv("EMAIL_TO", "recipient@example.com")
    monkeypatch.delenv("SCHEDULE", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(alerts, "SLEEP_BETWEEN_CALLS_S", 0)

    def _run(config, quotes=None, now=None, event="workflow_dispatch", schedule=None):
        path = tmp_path / "config.json"
        path.write_text(config if isinstance(config, str) else json.dumps(config))
        monkeypatch.setattr(alerts, "CONFIG_PATH", path)
        if event is None:
            monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        else:
            monkeypatch.setenv("GITHUB_EVENT_NAME", event)
        if schedule is None:
            monkeypatch.delenv("SCHEDULE", raising=False)
        else:
            monkeypatch.setenv("SCHEDULE", schedule)
        now = now or utc(SUMMER, 14, 50)
        monkeypatch.setattr(alerts, "_now_utc", lambda: now)

        calls: list[str] = []
        quotes = quotes or {}

        def fake_get(url, params=None, timeout=None):
            symbol = params["symbol"]
            calls.append(symbol)
            result = quotes.get(symbol)
            if result is None:
                raise AssertionError(f"unexpected quote request for {symbol}")
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(alerts.requests, "get", fake_get)

        emails: list[dict] = []
        monkeypatch.setattr(
            alerts, "send_email",
            lambda subject, body, severity=None: emails.append(
                {"subject": subject, "body": body, "severity": severity}),
        )
        return alerts.main(), calls, emails

    return _run


QUIET_QUOTE = {"c": 50.0, "dp": 0.1}
ONE_STOCK = {"stocks": {"AAA": [{"type": "price_above", "value": 1000}]}}


# --------------------------------------------------------------------------
# Config validation


@pytest.mark.parametrize(
    "stocks, expected",
    [
        ({"AAA": [{"type": "price_over", "value": 10}]}, "'type' must be one of"),
        ({"AAA": [{"type": "price_above", "value": "10"}]}, "'value' must be a number"),
        ({"AAA": [{"type": "price_above", "value": True}]}, "'value' must be a number"),
        ({"AAA": {"type": "price_above", "value": 10}}, "must be a list of triggers"),
        ({"AAA": [{"value": 10}]}, "'type' must be one of"),
        ({"AAA": [{"type": "price_above"}]}, "'value' must be a number"),
        ({"AAA": ["price_above 10"]}, "must be an object"),
        (["AAA"], "'stocks' must be an object"),
    ],
)
def test_invalid_config_exits_1_without_network(run, capsys, stocks, expected):
    code, calls, emails = run({"stocks": stocks})
    out = capsys.readouterr().out
    assert code == 1
    assert calls == [] and emails == []
    assert "FATAL: invalid price_alerts/config.json:" in out
    assert expected in out


def test_validation_reports_every_problem(run, capsys):
    config = {"stocks": {
        "AAA": [{"type": "price_above", "value": 10},
                {"type": "bogus", "value": 1}],
        "BBB": [{"type": "price_below", "value": False}],
        "CCC": "price_above 5",
    }}
    code, calls, _ = run(config)
    out = capsys.readouterr().out
    assert code == 1 and calls == []
    assert "stocks.AAA[1]" in out
    assert "stocks.BBB[0]" in out
    assert "stocks.CCC" in out
    assert "stocks.AAA[0]" not in out


def test_unhashable_type_is_reported_not_raised(run, capsys):
    config = {"stocks": {
        "AAA": [{"type": ["price_above"], "value": 1},
                {"type": {"x": 1}, "value": 1}],
        "BBB": 5,
    }}
    code, calls, emails = run(config)
    out = capsys.readouterr().out
    assert code == 1 and calls == [] and emails == []
    assert "FATAL: invalid price_alerts/config.json:" in out
    assert "stocks.AAA[0]: 'type' must be one of" in out
    assert "stocks.AAA[1]: 'type' must be one of" in out
    assert "stocks.BBB: must be a list of triggers" in out


def test_huge_integer_value_does_not_crash_validation(run, capsys):
    # json.loads parses this as an int too large for math.isfinite (OverflowError).
    huge = "1" + "0" * 400
    config = ('{"stocks": {"AAA": [{"type": "price_above", "value": ' + huge + '},'
              ' {"type": "price_below", "value": "x"}]}}')
    code, calls, _ = run(config)
    out = capsys.readouterr().out
    assert code == 1 and calls == []
    assert "stocks.AAA[1]: 'value' must be a number" in out
    assert "stocks.AAA[0]" not in out  # an int is a number, however large


def test_non_finite_float_values_rejected(run, capsys):
    config = ('{"stocks": {"AAA": [{"type": "price_above", "value": 1e400},'
              ' {"type": "price_below", "value": NaN}]}}')
    code, calls, _ = run(config)
    out = capsys.readouterr().out
    assert code == 1 and calls == []
    assert "stocks.AAA[0]: 'value' must be a number" in out
    assert "stocks.AAA[1]: 'value' must be a number" in out


def test_malformed_json_exits_1(run, capsys):
    code, calls, _ = run('{"stocks": {')
    assert code == 1 and calls == []
    assert "FATAL" in capsys.readouterr().out


def test_valid_numeric_values_accepted():
    stocks, problems = alerts.validate_config({"stocks": {
        "AAA": [{"type": "price_above", "value": 10},
                {"type": "percent_change_below", "value": -2.5}],
    }})
    assert problems == []
    assert list(stocks) == ["AAA"]


def test_shipped_config_is_valid():
    config = json.loads(alerts.CONFIG_PATH.read_text())
    _, problems = alerts.validate_config(config)
    assert problems == []


@pytest.mark.parametrize("config", [{"stocks": {}}, {}, {"stocks": None}])
def test_empty_stocks_returns_0(run, capsys, config):
    code, calls, emails = run(config)
    assert code == 0
    assert calls == [] and emails == []
    assert "nothing to do" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Cron-aware time gate


@pytest.mark.parametrize(
    "label, schedule, now, accepted",
    [
        # Morning pair.
        ("summer on time", "45 14 * * 1-5", utc(SUMMER, 14, 45), True),
        ("summer 25 min late", "45 14 * * 1-5", utc(SUMMER, 15, 10), True),
        ("summer at cap", "45 14 * * 1-5", utc(SUMMER, 15, 30), True),
        ("summer beyond cap", "45 14 * * 1-5", utc(SUMMER, 15, 31), False),
        ("summer wrong-season cron", "45 15 * * 1-5", utc(SUMMER, 15, 45), False),
        ("winter on time", "45 15 * * 1-5", utc(WINTER, 15, 45), True),
        ("winter wrong-season cron", "45 14 * * 1-5", utc(WINTER, 14, 45), False),
        # The old duplicate-send bug: the 09:45 EST fire delayed 70 minutes
        # lands inside the 10:45 window by wall clock alone.
        ("winter wrong-season delayed 70m", "45 14 * * 1-5", utc(WINTER, 15, 55), False),
        ("winter beyond cap", "45 15 * * 1-5", utc(WINTER, 16, 31), False),
        # Afternoon pair.
        ("summer pm on time", "5 20 * * 1-5", utc(SUMMER, 20, 5), True),
        ("summer pm at cap", "5 20 * * 1-5", utc(SUMMER, 20, 35), True),
        ("summer pm beyond cap", "5 20 * * 1-5", utc(SUMMER, 20, 36), False),
        ("summer pm wrong-season cron", "5 21 * * 1-5", utc(SUMMER, 21, 5), False),
        ("winter pm on time", "5 21 * * 1-5", utc(WINTER, 21, 5), True),
        ("winter pm wrong-season cron", "5 20 * * 1-5", utc(WINTER, 20, 5), False),
        ("winter pm wrong-season delayed 70m", "5 20 * * 1-5", utc(WINTER, 21, 15), False),
        ("winter pm beyond cap", "5 21 * * 1-5", utc(WINTER, 21, 36), False),
    ],
)
def test_cron_gate_matrix(run, label, schedule, now, accepted):
    code, calls, emails = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)},
                              now=now, event="schedule", schedule=schedule)
    assert code == 0
    assert calls == (["AAA"] if accepted else []), label
    assert emails == []


def test_delayed_wrong_season_fire_would_pass_wall_clock_check():
    """Sanity check that the matrix above exercises the real bug."""
    now_et = utc(WINTER, 15, 55).astimezone(alerts.EASTERN)
    assert alerts.in_run_window(now_et)
    allowed, _ = alerts.scheduled_run_allowed(dt.time(14, 45), utc(WINTER, 15, 55))
    assert not allowed


def test_intended_fire_rolls_back_a_day_after_midnight_utc():
    fire = alerts.intended_fire_utc(dt.time(21, 5), utc(dt.date(2026, 1, 15), 0, 10))
    assert fire == utc(dt.date(2026, 1, 14), 21, 5)
    allowed, _ = alerts.scheduled_run_allowed(dt.time(21, 5), utc(dt.date(2026, 1, 15), 0, 10))
    assert not allowed


@pytest.mark.parametrize(
    "schedule, expected",
    [
        ("45 14 * * 1-5", dt.time(14, 45)),
        ("5 21 * * 1-5", dt.time(21, 5)),
        ("", None),
        (None, None),
        ("*/5 * * * *", None),
        ("0,30 14 * * 1-5", None),
        ("45 14", None),
        ("75 14 * * 1-5", None),
        ("45 24 * * 1-5", None),
    ],
)
def test_parse_cron_time(schedule, expected):
    assert alerts.parse_cron_time(schedule) == expected


@pytest.mark.parametrize(
    "schedule, now, accepted",
    [
        (None, utc(SUMMER, 14, 45), True),       # 10:45 EDT: inside a window
        (None, utc(SUMMER, 13, 0), False),       # 09:00 EDT: outside
        ("*/5 * * * *", utc(SUMMER, 20, 10), True),
        ("garbage", utc(SUMMER, 18, 0), False),
    ],
)
def test_missing_or_unparseable_schedule_falls_back(run, capsys, schedule, now, accepted):
    code, calls, _ = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)},
                         now=now, event="schedule", schedule=schedule)
    assert code == 0
    assert calls == (["AAA"] if accepted else [])
    assert "falling back" in capsys.readouterr().out


def test_local_run_uses_wall_clock_window(run):
    _, calls, _ = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)},
                      now=utc(SUMMER, 3, 0), event=None)
    assert calls == []
    _, calls, _ = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)},
                      now=utc(SUMMER, 20, 10), event=None)
    assert calls == ["AAA"]


def test_workflow_dispatch_bypasses_gate(run):
    code, calls, _ = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)},
                         now=utc(SUMMER, 3, 0), event="workflow_dispatch",
                         schedule="45 15 * * 1-5")
    assert code == 0
    assert calls == ["AAA"]


def test_in_run_window_boundaries():
    def et(h, m):
        return dt.datetime.combine(SUMMER, dt.time(h, m), tzinfo=alerts.EASTERN)
    assert alerts.in_run_window(et(10, 45))
    assert alerts.in_run_window(et(11, 30))
    assert not alerts.in_run_window(et(11, 31))
    assert not alerts.in_run_window(et(10, 44))
    assert alerts.in_run_window(et(16, 35))
    assert not alerts.in_run_window(et(16, 36))


# --------------------------------------------------------------------------
# Failure surfacing


def test_all_fetches_failing_exits_1(run, capsys):
    config = {"stocks": {"AAA": [{"type": "price_above", "value": 1}],
                         "BBB": [{"type": "price_below", "value": 1}]}}
    quotes = {"AAA": requests.ConnectionError("boom"),
              "BBB": requests.Timeout("slow")}
    code, calls, emails = run(config, quotes=quotes)
    out = capsys.readouterr().out
    assert code == 1
    assert calls == ["AAA", "BBB"]
    assert emails == []
    assert "ERROR: quote fetch failed for every configured ticker" in out


def test_single_transient_failure_is_a_warning(run, capsys):
    config = {"stocks": {"AAA": [{"type": "price_above", "value": 1000}],
                         "BBB": [{"type": "price_below", "value": 1}]}}
    quotes = {"AAA": requests.ConnectionError("boom"),
              "BBB": FakeResponse(QUIET_QUOTE)}
    code, calls, emails = run(config, quotes=quotes)
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["AAA", "BBB"]
    assert emails == []
    assert "WARNING: quote fetch failed for 'AAA'" in out
    assert "ERROR" not in out


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_exits_1_after_sending_fired_alerts(run, capsys, status):
    config = {"stocks": {"AAA": [{"type": "price_above", "value": 1}],
                         "BBB": [{"type": "price_above", "value": 10}]}}
    quotes = {"AAA": FakeResponse({"error": "no"}, status=status, symbol="AAA"),
              "BBB": FakeResponse({"c": 20.0, "dp": 1.5})}
    code, calls, emails = run(config, quotes=quotes)
    out = capsys.readouterr().out
    assert code == 1
    assert calls == ["AAA", "BBB"]
    assert len(emails) == 1          # the BBB alert still goes out
    assert "BBB" in emails[0]["body"]
    assert "ERROR: Finnhub refused the request" in out
    assert API_KEY not in out        # token in the request URL is redacted


def test_non_auth_http_error_single_ticker_counts_as_all_failed(run, capsys):
    quotes = {"AAA": FakeResponse({}, status=500, symbol="AAA")}
    code, _, _ = run(ONE_STOCK, quotes=quotes)
    out = capsys.readouterr().out
    assert code == 1
    assert "Finnhub refused" not in out
    assert "every configured ticker" in out


def test_zero_quote_is_skipped_not_failed(run, capsys):
    config = {"stocks": {"ZZZ": [{"type": "price_below", "value": 5}]}}
    code, calls, emails = run(config, quotes={"ZZZ": FakeResponse({"c": 0, "dp": None})})
    assert code == 0
    assert calls == ["ZZZ"] and emails == []
    assert "no usable quote" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Alert email


def test_fired_triggers_send_one_consolidated_email(run):
    config = {"stocks": {
        "AAA": [{"type": "price_above", "value": 100},
                {"type": "percent_change_below", "value": -5}],
        "BBB": [{"type": "percent_change_above", "value": 2}],
        "CCC": [{"type": "price_below", "value": 1}],
    }}
    quotes = {"AAA": FakeResponse({"c": 120.5, "dp": 1.0}),
              "BBB": FakeResponse({"c": 10.0, "dp": 3.25}),
              "CCC": FakeResponse({"c": 5.0, "dp": 0.0})}
    code, calls, emails = run(config, quotes=quotes)
    assert code == 0
    assert calls == ["AAA", "BBB", "CCC"]
    assert len(emails) == 1
    email = emails[0]
    assert email["subject"] == "Stock Alert: 2 trigger(s) fired"
    assert email["severity"] == "red"
    assert "- AAA: price above 100 | current price 120.5 | change +1.00%" in email["body"]
    assert "- BBB: percent change above 2% | current price 10.0 | change +3.25%" in email["body"]
    assert "CCC" not in email["body"]
    assert "stateless" in email["body"]


def test_nothing_fired_sends_no_email(run, capsys):
    code, _, emails = run(ONE_STOCK, quotes={"AAA": FakeResponse(QUIET_QUOTE)})
    assert code == 0 and emails == []
    assert "No triggers fired" in capsys.readouterr().out
