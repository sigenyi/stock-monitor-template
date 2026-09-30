"""Offline tests for common.emailer: SMTP transport selection, env handling
and log hygiene. smtplib is replaced with fakes; nothing connects anywhere.
"""

from __future__ import annotations

import ssl

import pytest

from common import emailer

SENDER = "sender@example.com"
RECIPIENT = "recipient@example.com"
PASSWORD = "test-app-password"


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what happened."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None, ssl_mode=False):
        self.host, self.port, self.timeout = host, port, timeout
        self.context = context
        self.ssl_mode = ssl_mode
        self.starttls_context = None
        self.starttls_called = False
        self.logins: list[tuple[str, str]] = []
        self.sent: list = []
        self.events: list[str] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_called = True
        self.starttls_context = context
        self.events.append("starttls")

    def login(self, user, password):
        self.logins.append((user, password))
        self.events.append("login")

    def send_message(self, msg):
        self.sent.append(msg)
        self.events.append("send")


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []

    def make_ssl(host, port, timeout=None, context=None):
        return FakeSMTP(host, port, timeout=timeout, context=context, ssl_mode=True)

    def make_plain(host, port, timeout=None):
        return FakeSMTP(host, port, timeout=timeout, ssl_mode=False)

    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", make_ssl)
    monkeypatch.setattr(emailer.smtplib, "SMTP", make_plain)
    monkeypatch.setenv("EMAIL_ADDRESS", SENDER)
    monkeypatch.setenv("EMAIL_APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("EMAIL_TO", RECIPIENT)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    return FakeSMTP.instances


def test_defaults_use_gmail_ssl(smtp):
    emailer.send_email("Hello", "Body text")
    assert len(smtp) == 1
    conn = smtp[0]
    assert conn.ssl_mode is True
    assert (conn.host, conn.port, conn.timeout) == ("smtp.gmail.com", 465, 60)
    assert conn.starttls_called is False
    assert conn.logins == [(SENDER, PASSWORD)]
    assert len(conn.sent) == 1
    msg = conn.sent[0]
    assert msg["Subject"] == "Hello"
    assert msg["From"] == SENDER
    assert msg["To"] == RECIPIENT
    assert msg.get_content().strip() == "Body text"


def test_ssl_context_verifies_certificates(smtp):
    emailer.send_email("Hello", "Body")
    ctx = smtp[0].context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_port_465_uses_ssl_with_custom_host(smtp, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    emailer.send_email("Hello", "Body")
    conn = smtp[0]
    assert conn.ssl_mode is True
    assert (conn.host, conn.port) == ("smtp.example.com", 465)
    assert conn.starttls_called is False


@pytest.mark.parametrize("port", ["587", "25", "2525"])
def test_other_ports_use_starttls(smtp, monkeypatch, port):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", port)
    emailer.send_email("Hello", "Body")
    assert len(smtp) == 1
    conn = smtp[0]
    assert conn.ssl_mode is False
    assert (conn.host, conn.port, conn.timeout) == ("smtp.example.com", int(port), 60)
    assert conn.events == ["starttls", "login", "send"]
    assert conn.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.logins == [(SENDER, PASSWORD)]


@pytest.mark.parametrize("blank", ["", "   "])
def test_empty_smtp_vars_mean_defaults(smtp, monkeypatch, blank):
    # The workflows pass ${{ vars.SMTP_HOST }} / ${{ vars.SMTP_PORT }}, which
    # are empty strings when the repository variables are not defined.
    monkeypatch.setenv("SMTP_HOST", blank)
    monkeypatch.setenv("SMTP_PORT", blank)
    emailer.send_email("Hello", "Body")
    conn = smtp[0]
    assert conn.ssl_mode is True
    assert (conn.host, conn.port) == ("smtp.gmail.com", 465)


def test_host_only_override_keeps_default_port(smtp, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.example.org")
    monkeypatch.setenv("SMTP_PORT", "")
    emailer.send_email("Hello", "Body")
    assert (smtp[0].host, smtp[0].port, smtp[0].ssl_mode) == ("mail.example.org", 465, True)


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "-1"])
def test_invalid_port_exits_1_without_connecting(smtp, monkeypatch, capsys, bad):
    monkeypatch.setenv("SMTP_PORT", bad)
    with pytest.raises(SystemExit) as exc:
        emailer.send_email("Hello", "Body")
    assert exc.value.code == 1
    assert smtp == []
    assert "SMTP_PORT" in capsys.readouterr().err


def test_recipient_not_printed(smtp, capsys):
    emailer.send_email("Quarterly update", "Body", severity="info")
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert RECIPIENT not in output
    assert "Sending email:" in output
    assert "Quarterly update" in output
    assert "Email sent." in output


def test_severity_prefix(smtp):
    emailer.send_email("Alert", "Body", severity="red")
    assert smtp[0].sent[0]["Subject"] == f"{emailer.SEVERITY_EMOJI['red']} Alert"


def test_unknown_severity_warns_and_sends_unprefixed(smtp, capsys):
    emailer.send_email("Alert", "Body", severity="purple")
    assert smtp[0].sent[0]["Subject"] == "Alert"
    assert "WARNING: unknown severity" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "EMAIL_TO"])
def test_send_email_requires_credentials(smtp, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as exc:
        emailer.send_email("Hello", "Body")
    assert exc.value.code == 1
    assert smtp == []


def test_require_env_exits_1_when_missing(monkeypatch, capsys):
    monkeypatch.delenv("STOCK_MONITOR_TEST_VAR", raising=False)
    with pytest.raises(SystemExit) as exc:
        emailer.require_env("STOCK_MONITOR_TEST_VAR")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "FATAL" in err and "STOCK_MONITOR_TEST_VAR" in err


@pytest.mark.parametrize("blank", ["", "  \t"])
def test_require_env_exits_1_when_blank(monkeypatch, blank):
    monkeypatch.setenv("STOCK_MONITOR_TEST_VAR", blank)
    with pytest.raises(SystemExit) as exc:
        emailer.require_env("STOCK_MONITOR_TEST_VAR")
    assert exc.value.code == 1


def test_require_env_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("STOCK_MONITOR_TEST_VAR", "  value  ")
    assert emailer.require_env("STOCK_MONITOR_TEST_VAR") == "value"
