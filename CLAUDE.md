# CLAUDE.md — agent guide

This guide is for an agent maintaining a copy of this repository. Read `README.md` for setup and the full configuration reference.

The system is a two-part stock monitor that runs entirely on GitHub Actions. There is no server, no database and nothing to deploy. **Every commit to the default branch is live:** each scheduled run checks out the latest commit there.

## What the system does

**Part 1: Price alerts (`price_alerts/`).**
- Runs twice per US trading day (about 10:45 and 16:05 ET).
- Fetches Finnhub quotes for the tickers in `price_alerts/config.json` and evaluates their triggers.
- Sends one consolidated 🔴 email if any trigger fired.
- **Stateless:** no de-duplication, so a trigger that stays true alerts on every run until someone edits or deletes it.
- An empty `stocks` map is valid. The run logs "nothing to do" and exits 0.

**Part 2: Filings watcher (`filings/`).** It polls SEC EDGAR hourly on weekdays (UTC) for the company in `filings/filings_config.json`. New watched filings are handled oldest first:

| Filing | Handling |
|---|---|
| 8-K / 8-K/A with Item 2.06 | Instant 🔴 red alert. It is sent first and on its own, and never waits for extraction. |
| 8-K / 8-K/A with Item 2.02 (earnings) | ℹ️ notification + Claude extraction of the press-release exhibit. The exhibit lookup has three stages: EX-99 types from the index page, then a filename regex, then the primary document. |
| 10-Q / 10-K (and /A) | ℹ️ notification + Claude extraction of the primary document. The text is excerpted around anchor phrases and capped at ~200K chars. |
| Any other watched form, or an 8-K / 8-K/A without Item 2.02/2.06 | Plain ℹ️ notification. (An 8-K with 2.06 but not 2.02 gets only the red alert, no notification.) |

**Extraction** is skipped when `extraction.enabled` is `false`; those filings then get only the notification. Otherwise it:
1. Fills a 16-field checklist as strict JSON, with verbatim quotes, and normalizes the value types.
2. Merges the result into `filings/history.json`, keyed by CIK and then by fiscal quarter.
3. Runs the rules engine.
4. Emails a plain-English review draft containing only the fired rules, the notes and the links. Every value lives in `history.json`.

If stage 1 (download / Claude / parse) or stage 2 (quarter, history, rules, email build) fails, the watcher sends a 🟡 "…— review the filing manually" email with the `force_accession` retry value.

**State.**
- The workflow bot commits `state.json` (seen accessions, the one-shot `flags`, bound to the CIK) and `history.json` as `chore(filings): update state/history [skip ci]`.
- An optional ⏱️ time-stop reminder fires once `baselines.json → time_stop.review_after` arrives. It is tracked in `state.json → flags.time_stop_sent`.

Extraction output is a **draft**. Every review email says to verify against the filing and that it is not investment advice.

## File map: which file for which change

| Change | File |
|---|---|
| Price tickers / triggers | `price_alerts/config.json` |
| Price trigger types, run windows, grace, validation | `price_alerts/alerts.py` (`TRIGGER_DESCRIPTIONS`, `WINDOWS`, `GRACE`, `MAX_SPAN_FROM_START`) |
| Watched company, forms, SEC tool name, extraction model/tokens/temperature, company wording, rule thresholds | `filings/filings_config.json` (`company`, `watched_forms`, `user_agent_tool_name`, `extraction`, `profile`, `thresholds`) |
| Optional reference values (prior-quarter organic fallback, share count, buyback authorization/run-rate, time-stop) | `filings/baselines.json` |
| Filing classification, alert routing, seeding, CIK binding, backlog valve, force-reprocess, time-stop | `filings/watcher.py` |
| Document retrieval, excerpting, prompt/checklist, Claude call, parsing, fiscal quarters, history merge, rules, plain-English text (`PLAIN`), review email | `filings/extraction.py` |
| Email transport (SMTP, Gmail by default), severity emoji | `common/emailer.py` |
| Schedules, opt-in gating, secrets/vars passed to scripts, state commit | `.github/workflows/price-alerts.yml`, `.github/workflows/filings-watcher.yml` |
| Dependencies | `requirements.txt` (runtime), `requirements-dev.txt` (+ pytest) |
| Tests | `tests/` |
| **Don't hand-edit** | `filings/state.json`, `filings/history.json`: the bot owns them |

## Price-trigger schema

`price_alerts/config.json` maps each ticker to a list of triggers:

```json
{
  "stocks": {
    "AAPL": [
      { "type": "price_above", "value": 240 },
      { "type": "price_below", "value": 190 }
    ],
    "TSLA": [
      { "type": "percent_change_above", "value": 5 },
      { "type": "percent_change_below", "value": -5 }
    ]
  }
}
```

- `price_above` / `price_below` compare Finnhub's current price `c`. `percent_change_above` / `percent_change_below` compare the day % change `dp`; `-5` means −5%. All comparisons are strict (`>` / `<`).
- `value` must be a JSON number: not a string, and not a bool.
- **The whole file is validated before any API call.** One bad entry (unknown type, string value, non-list) makes the run exit 1 with `FATAL: invalid price_alerts/config.json:`, and **no** triggers are checked. So validate edits carefully.
- An unknown ticker makes Finnhub return zeros. That ticker is skipped with a warning and never fires a false alert.
- To clear all alerts, set `"stocks": {}`.

## Where the filing-watcher knobs live

**Thresholds** live only in `filings/filings_config.json → thresholds`. A missing or `null` key skips that rule; there are no code defaults.

| Key | Rule | Fires when |
|---|---|---|
| `buyback_red_floor_qtr_m` | `buyback_below_floor` 🔴 | quarterly buyback $M < value |
| `leverage_red_x` | `leverage_high` 🔴 | leverage ratio > value |
| `margin_yellow_pct` | `margin_floor` 🟡 | adjusted margin % < value |
| `subtraction_growth_escalate_at` | `subtraction_growth` 🟡 | total reported revenue has fallen while organic stayed positive for ≥ value consecutive quarters |

**Always-on rules:**
- `two_neg_organic` 🔴: core organic growth is negative this quarter and last. The prior quarter comes from history, then `baselines.organic_growth_history`.
- `buyback_suspended` 🔴: suspension / reduction language.
- `goodwill_impairment` 🔴: backstop to the Item 2.06 alert.
- `guidance_softened` 🟡
- `goodwill_cushion` 🟡: the smallest cushion is lower than the most recent earlier one within 8 quarters.
- `perimeter_changed` ⚪ watch

**Segment rule:** `segment_lag` 🟡 runs only when `profile.segment.name` is set.

**The profile** (`filings_config.json → profile`, every key optional) shapes the prompt and the email wording:
- `core_label`, `margin_label`, `perimeter_section_hint`
- `segment{name, plain_name, share_of_core_pct}`
- `guidance_reference_phrases`: without them, a generic "explicitly lowered/withdrawn" criterion applies
- `goodwill_test_date` (`MM-DD`): adds a standing note on the 10-Q/10-K covering it
- `extra_anchor_phrases`: added to `BASE_ANCHOR_PHRASES` in `extraction.py`
- `plain_overrides{rule_id: [headline, meaning]}`

**Baseline keys the code reads:**
- `baseline_asof`
- `organic_growth_history`
- `shares_outstanding_m`
- `buyback.authorization_remaining_b`
- `buyback.run_rate_qtr_m`
- `time_stop.review_after` / `note`

The whole file is optional (`{}` is valid). **Baselines are not bound to a CIK.**

**Extraction settings** are in `filings_config.json → extraction`:
- `enabled`: default `true`
- `model`: default `claude-sonnet-4-6`
- `max_tokens`: default 4000
- `temperature`: default 0. It's omitted from the request when `null`, because newer models reject non-default sampling parameters.

**Rule ids and severities** are in `RULE_IDS` / `run_rules` in `extraction.py`. Each rule id needs an entry in `PLAIN`, and a test enforces this. If you add a rule, add its `PLAIN` entry, a README alerts-table row and tests.

## Scheduling and the DST gate

- **price-alerts:** GitHub cron is UTC and ignores daylight saving, so there are four crons (`45 14`, `45 15`, `5 20`, `5 21`, Mon–Fri), one per window per season.
  - The workflow passes the cron that fired as `SCHEDULE`. `alerts.py` then continues only if that cron's fire time in ET is **exactly** a window start (10:45 or 16:05) **and** the run started inside the accepted span. The span is `min(end + 20 min, start + 45 min)`, i.e. 10:45–11:30 and 16:05–16:35 ET.
  - The wrong-season fire exits 0 with a "Skipping:" message, however late it runs.
  - If `SCHEDULE` is missing or unparseable, the script falls back to the wall-clock `in_run_window`.
  - `workflow_dispatch` bypasses the gate.
  - **To change run times**, change the crons and `WINDOWS` together. For each window, one cron must land exactly on its start in EDT and another in EST.
- **filings-watcher:** `0 * * * 1-5` is hourly Mon–Fri **UTC**, which is roughly Sunday evening to Friday evening ET. A late-Friday filing is noticed on Sunday evening; that's a known tradeoff. Runs are serialized by the `filings` concurrency group. The job has `contents: write` so it can push state; the top-level token permissions are `{}`.
- GitHub schedules are best-effort and often a few minutes late. They run only from the default branch. In public repos they auto-disable after 60 days without activity; re-enable them from the Actions tab.

## Opt-in and optional repository variables

- Scheduled jobs run only when `vars.ENABLE_PRICE_ALERTS == 'true'` / `vars.ENABLE_FILINGS_WATCHER == 'true'`. That's the job-level `if:` in each workflow. Manual **Run workflow** always runs.
- `SMTP_HOST` (default `smtp.gmail.com`) and `SMTP_PORT` (default 465) are optional. Port 465 uses implicit SSL; any other port uses STARTTLS. Empty counts as unset. An invalid `SMTP_PORT` is only detected when a run sends an email, and then exits 1 with a `FATAL:` message.

## Operating rules

- **Pull before editing.** The workflow bot commits state too, so a local clone can be behind. Always run `git pull` first.
- **Validate JSON before committing:** `python -m json.tool <file>`. A trailing comma breaks the run, and for price alerts one invalid trigger blocks all alerts.
- **Show the diff and wait for confirmation before committing.** This applies to code changes and config changes alike.
- **Write descriptive commit messages.** Examples:
  - `config: add TSLA -5% and <$300 triggers`
  - `thresholds: margin_yellow_pct 15 -> 14.5`
  - `watch: switch filings watcher to <Company> (CIK <n>)`
  - `code: <what changed>`
- **Never hand-edit `filings/state.json` or `filings/history.json`** unless the user explicitly asks, and even then confirm first. The reset contents are `{"flags": {}, "seeded": false, "seen_accessions": []}` and `{}`.
- **Never put credentials in files or commit them.** All credentials live in GitHub Actions repository secrets:
  - `FINNHUB_API_KEY`
  - `EMAIL_ADDRESS`
  - `EMAIL_APP_PASSWORD`
  - `EMAIL_TO`
  - `SEC_CONTACT_EMAIL`
  - `ANTHROPIC_API_KEY` (needed while extraction is enabled)
- **If the user names a company without a ticker or CIK**, resolve it and state which ticker and CIK you used.
- **When the user reports that a price alert fired**, offer to ratchet the trigger to a new level or delete it. Price alerts are stateless, so an untouched trigger alerts again on every run.
- **Changing the watched company** means editing `company` (name, ticker, cik, fiscal_year_end), reviewing `profile` and `thresholds`, and replacing `baselines.json` with `{}` or the new company's values. State re-seeds silently and automatically on the CIK change, and history is namespaced by CIK. Walk through README → "Watch a different company", then suggest a `force_accession` test run.
- **Don't weaken SEC etiquette.** Every EDGAR request sends `User-Agent: <user_agent_tool_name> <SEC_CONTACT_EMAIL>`, and document downloads have sleeps between them. The SEC blocks anonymous or abusive clients.
- **Keep the code style:** plain Python, `print()` logging with `WARNING:` / `ERROR:` / `FATAL:` prefixes, and module docstrings. Add no dependencies beyond `requirements.txt` without asking.

## Testing and manual runs

- **Unit tests** (offline: Finnhub, SEC, Anthropic and SMTP are monkeypatched):
  ```sh
  pip install -r requirements-dev.txt
  python -m pytest -q
  python -m pytest -q tests/test_price_alerts.py      # or test_emailer.py, test_filings_*.py
  ```
  Run them before proposing a code change, and add tests for new behavior. Tests must never touch the network, and must not write to the real `filings/*.json` files (monkeypatch `STATE_PATH` / `HISTORY_PATH` / `CONFIG_PATH` to `tmp_path`).
- **Price alerts end to end:** Actions → `price-alerts` → Run workflow (bypasses the time gate). To force an email, temporarily add a certainly-true trigger (e.g. `price_above: 1`), run it, confirm the email arrived, then revert.
- **Filings extraction end to end:** Actions → `filings-watcher` → Run workflow with `force_accession` = a past accession number. Dashes are optional; the value must match `\d{10}-?\d{2}-?\d{6}`. Only about the 1,000 most recent filings are reachable. The run re-classifies and re-extracts even if the filing was already seen, and writes to `history.json`.
- **Local runs** from the repo root need the env vars exported and do real API calls and sends.
  - Price alerts: `GITHUB_EVENT_NAME=workflow_dispatch python -m price_alerts.alerts` bypasses the gate.
  - Watcher: `python -m filings.watcher` (add `FORCE_ACCESSION=…` to re-process one filing). A local watcher run rewrites `state.json` / `history.json`, so don't commit those changes.

## Quirks worth knowing

- **Exit codes.**
  - price-alerts exits 1 on:
    - a missing secret
    - an unreadable or invalid config
    - any Finnhub 401/403
    - all tickers failing to fetch
    - the alert email can't be sent (SMTP error or invalid `SMTP_PORT`)
  - The watcher exits 1 on:
    - a missing secret
    - a missing config / `company.cik` / `company.name`
    - invalid JSON in `filings_config.json` / `state.json` / `baselines.json`
    - a failed submissions request
    - a malformed or unknown `force_accession`
    - the backlog valve
    - an invalid `SMTP_PORT` (when it first sends an email)
    - an email that can't be sent during a forced run
  - Everything else exits 0, including per-filing failures in a normal run (an SMTP outage there leaves the filing unseen for a retry).
- **Backlog valve.** More than 20 new watched filings in one normal run means `state.json` is stale for this CIK. The watcher then sends no emails and exits 1, with instructions to reset the state file. Forced runs and seeding skip the valve.
- **Retry semantics.** The extraction pipeline catches its own stage 1 and stage 2 failures and emails about them. Only a failure to *send* an email propagates. The watcher then leaves the filing unseen and retries it next hour, which can produce duplicate notification or 2.06 emails on the retry. The commit step runs even after a failed watcher step (`always()`), so partial progress is kept.
- **First-run seeding.** With `seeded: false`, or after a CIK change, the watcher records the entire current backlog without alerting. A legacy seeded state without a `cik` key adopts the current CIK.
- **Unseeded forced runs.** A forced run before seeding processes the filing and saves it as seen, but leaves `seeded: false`. The next normal run then seeds.
- **8-K with both items.** An 8-K that carries both Item 2.06 and 2.02 gets the red alert **and** the notification + extraction.
- **Fiscal quarters.**
  - 10-Q/10-K `reportDate` snaps to the nearest fiscal quarter-end.
  - 8-K numbers are attributed to the latest fiscal quarter that ended strictly before the 8-K's `reportDate`; an earnings 8-K dated 2026-07-28 → `2026-Q2` for a December year-end.
  - Labels use the calendar year in which the fiscal year ends.
  - Year-end month comes from `company.fiscal_year_end` (`MM-DD`), else SEC `fiscalYearEnd`, else December. A day ≤ 7 counts as the previous month (52/53-week years).
  - The watcher stores the resolved month on `cfg["_fye_month"]` at runtime.
- **History merge.** A found value overwrites an existing found value only when the incoming filing is not older. On the same date, a 10-Q/10-K beats an 8-K. A not-found value only fills an empty slot.
- **Adding a form to `watched_forms` later** emails any filings of that form that arrived after seeding. Only watched forms get marked seen after the seed, and the valve guards against a flood.
