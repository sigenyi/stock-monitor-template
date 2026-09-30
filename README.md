# Stock Monitor

Email alerts for stock prices and SEC filings, running entirely on GitHub Actions. **Part 1** checks your price triggers twice per US trading day. **Part 2** watches one company's SEC EDGAR filings every hour on weekdays. It alerts you right away when the company files an 8-K for a goodwill impairment. For earnings releases and 10-Q/10-K reports, it can have Claude pull a fixed checklist of fundamentals out of the filing, run a set of red/yellow rules against it and email you a plain-English review draft. You don't need a server or a database, and there is nothing to deploy. Copy the template, add a few secrets and turn the schedules on.

> [!WARNING]
> **Not investment advice.** Everything this project sends is automated output. The review drafts are extracted by a language model and can be wrong, incomplete or out of date. The software is provided as is, without warranty of any kind (see [LICENSE](LICENSE)). Check every alert and every extracted number against the filing before you act on it. This project never trades.

The repository ships with a working example configuration that watches **Omnicom Group (OMC)**. Keep it to try the system, or point it at another company (see [Watch a different company](#watch-a-different-company)).

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration reference](#configuration-reference)
- [Watch a different company](#watch-a-different-company)
- [Alerts reference](#alerts-reference)
- [Operational notes](#operational-notes)
- [Security and privacy](#security-and-privacy)
- [Development](#development)
- [Limitations and non-goals](#limitations-and-non-goals)
- [License](#license)

---

## What it does

### Part 1: Price alerts (`price_alerts/`)

- Runs twice per US trading day, at about 10:45 and 16:05 Eastern Time.
- Fetches a [Finnhub](https://finnhub.io) quote for each ticker in `price_alerts/config.json` and checks that ticker's triggers (price above/below, day % change above/below).
- Sends **one** consolidated email listing every trigger that fired. If nothing fired, it sends nothing.
- **Stateless by design:** it doesn't de-duplicate. A trigger that stays true alerts again on every run until you edit or delete it.

### Part 2: Filings watcher and LLM review drafts (`filings/`)

Runs every hour on weekdays. It polls SEC EDGAR for new filings from the company in `filings/filings_config.json` and handles each new filing whose form is in `watched_forms`, oldest first:

| New filing | What you get |
|---|---|
| 8-K (or 8-K/A) that lists **Item 2.06** (Material Impairments) | 🔴 An immediate red alert. It is sent first and on its own, and never waits for extraction. |
| 8-K (or 8-K/A) that lists **Item 2.02** (Results of Operations, i.e. earnings) | ℹ️ A notification, followed by an **LLM review draft** of the press-release exhibit (EX-99.x) |
| 10-Q / 10-K (and /A amendments) | ℹ️ A notification, followed by an **LLM review draft** of the main document |
| Any other watched form, or an 8-K (or 8-K/A) that lists neither Item 2.02 nor 2.06 (e.g. only 5.02, 7.01 or 8.01) | ℹ️ A plain notification |

If `extraction.enabled` is `false`, earnings 8-Ks and periodic reports get only the notification.

**The review draft.** The extraction step downloads the document and has Claude fill in a fixed 16-field checklist as strict JSON. The fields cover organic growth, margin, share count, buybacks, leverage, goodwill, book equity and guidance, and every value comes with a verbatim quote. The step then merges the result into a per-quarter history and evaluates the [rules](#alerts-reference). The email contains only what fired, explained in plain English, plus some informational notes and links. Every extracted value, with its quote and source filing, is stored in `filings/history.json`.

<details>
<summary>The extraction checklist (16 fields)</summary>

| Field | What the model is asked for |
|---|---|
| `organic_growth_core_pct` | Headline organic revenue growth for the quarter (uses `profile.core_label` when set) |
| `total_reported_revenue_change_pct` | Total reported revenue change vs. the prior-year quarter |
| `perimeter_changed` | Whether the organic-growth perimeter / disposal scope changed (true/false) |
| `adj_margin_pct` | Headline adjusted margin (named by `profile.margin_label`) |
| `segment_organic_pct` | Organic growth of `profile.segment` (**only asked when a segment is configured**) |
| `group_organic_pct` | Group/total organic growth |
| `shares_outstanding_m` | Shares outstanding from the 10-Q/10-K cover page, in millions |
| `buyback_dollars_qtr_m` | Share repurchases during the quarter, in $ millions |
| `buyback_remaining_authorization_m` | Remaining repurchase authorization, in $ millions |
| `buyback_suspension_language` | Any language suspending, pausing or reducing the buyback (true/false) |
| `leverage_sentence` | The verbatim sentence describing the credit-agreement leverage ratio |
| `leverage_ratio_x` | That leverage ratio as a number |
| `goodwill_impairment_charge` | `{impaired, amount_m}` |
| `goodwill_cushion_pct` | 10-K only: the smallest "fair value exceeded carrying value by X%" across reporting units |
| `book_equity_b` | Total shareholders' equity, in $ billions |
| `guidance_language` | `{softened, assessment}`, judged against `profile.guidance_reference_phrases` if set |

Each field is stored as `{found, value, quote, location}` along with the form, accession number and filing date it came from. When a value can't be read as the expected type, it is stored as not found and the original text is kept under `raw_value`.
</details>

### Emails you can receive

| Subject (example) | Severity | Sent when |
|---|---|---|
| `🔴 Stock Alert: 2 trigger(s) fired` | red | One or more price triggers fired |
| `🔴 Goodwill impairment 8-K filed — OMC (Item 2.06)` | red | An 8-K lists Item 2.06 |
| `ℹ️ OMC filing: 10-Q (2026-07-28)` | info | Any new watched filing, except an 8-K that lists Item 2.06 but not 2.02 (that one gets only the red alert) |
| `🔴 / 🟡 / ℹ️ OMC 2026 Q2 review draft — …` | highest fired | An extraction finished. It is red if any red rule fired, else yellow if any yellow rule fired, else info. |
| `🟡 OMC 10-Q: extraction failed — review the filing manually` | yellow | Downloading, the Claude call or parsing failed. The email includes the error, links and the `force_accession` value to retry. |
| `🟡 OMC 10-Q: review step failed — review the filing manually` | yellow | Quarter derivation, the history update, the rules or building the review email failed |
| `⏱️ Time-stop review date reached (…)` | clock | Optional one-shot reminder. See `time_stop` in [baselines](#filingsbaselinesjson-optional). |

Every review draft ends with: *"Values are LLM-extracted drafts. Verify each fired trigger against the filing before acting. This is automated output, not investment advice."*

---

## How it works

- **GitHub Actions only.** There are two workflows, `price-alerts` and `filings-watcher`. Each one checks out the latest commit on your default branch, installs `requirements.txt` on Python 3.12 and runs one script. Editing a config file and committing it is all the "deployment" there is.
- **Secrets are never stored in files.** API keys and email credentials live in GitHub Actions repository secrets and reach the scripts as environment variables. Every script stops early with a clear `FATAL:` message if a required secret is missing.
- **State lives in the repository.** The watcher records which filings it has already seen in `filings/state.json` and the extracted per-quarter records in `filings/history.json`. After each run, the workflow's bot (`github-actions[bot]`) commits changes to those two files with the message `chore(filings): update state/history [skip ci]`. Runs are serialized through a concurrency group, so these commits never race each other.
- **Schedules are opt-in.** Both scheduled jobs do nothing until you set a repository variable (see [Quick start, step 6](#6-turn-on-the-schedules)). Manual **Run workflow** runs always work.

```
.github/workflows/price-alerts.yml     Part 1 schedule (four UTC crons, opt-in)
.github/workflows/filings-watcher.yml  Part 2 schedule (hourly, opt-in) + state commit
price_alerts/alerts.py                 quotes, triggers, daylight-saving-aware time gate
price_alerts/config.json               your tickers and triggers
filings/watcher.py                     EDGAR polling, classification, alerts, seeding
filings/extraction.py                  document retrieval, Claude call, history, rules, review email
filings/filings_config.json            watched company, extraction settings, profile, thresholds
filings/baselines.json                 optional reference values
filings/state.json                     bot-owned: seen filings, bound to the watched CIK
filings/history.json                   bot-owned: extracted per-quarter records, keyed by CIK
common/emailer.py                      SMTP email (Gmail by default)
tests/                                 offline pytest suite
```

---

## Quick start

### 1. Create your own copy as a **private** repository

Click **Use this template** → **Create a new repository**, and set the visibility to **Private**.

> [!IMPORTANT]
> Use a private repository. In a public one, anyone can see your watchlist and price triggers, your thresholds, the extracted history the bot commits, and your Actions run logs (which print tickers, prices and filing details). Secrets stay masked either way, but everything else is visible.

Open the **Actions** tab. If GitHub asks, enable workflows. Two workflows, `price-alerts` and `filings-watcher`, should be listed.

### 2. Get your keys

| What | Needed for | How |
|---|---|---|
| **Finnhub API key** | Part 1. Optional for Part 2. | Sign up at <https://finnhub.io> and copy the key from your dashboard. The free tier (60 calls/minute) is far more than this project uses. |
| **Anthropic API key** | Part 2, only while `extraction.enabled` is `true` | Create one at <https://console.anthropic.com> under API keys. It's billed per use (see [cost notes](#cost)). |
| **Email sending credentials** | Both parts | **Gmail (default):** turn on 2-Step Verification, go to Google Account → Security → **App passwords**, create one and copy the 16-character password. Your normal Gmail password won't work for SMTP. **Other providers:** use that provider's SMTP password and set the `SMTP_HOST` / `SMTP_PORT` [repository variables](#repository-variables). |
| **SEC contact email** | Part 2 | Any real address you own. The SEC requires automated clients to identify themselves, so the watcher sends it in its `User-Agent` header (for example `StockMonitor you@example.com`). SEC blocks anonymous clients. |

### 3. Add the secrets

In your repository, open **Settings → Secrets and variables → Actions → Secrets → New repository secret**. The names must match exactly. Add only the secrets the parts you use need:

| Secret | Value | Part 1: price alerts | Part 2: filings watcher |
|---|---|:-:|:-:|
| `FINNHUB_API_KEY` | Finnhub API key | **required** | optional (enables the market-value-vs-book note) |
| `EMAIL_ADDRESS` | Sending account. It is both the SMTP login and the From address. | **required** | **required** |
| `EMAIL_APP_PASSWORD` | Gmail app password, or your provider's SMTP password | **required** | **required** |
| `EMAIL_TO` | Recipient address. It can be the same as `EMAIL_ADDRESS`. | **required** | **required** |
| `SEC_CONTACT_EMAIL` | Contact email for the SEC `User-Agent` header | — | **required** |
| `ANTHROPIC_API_KEY` | Anthropic API key | — | required while `extraction.enabled` is `true` |

If `ANTHROPIC_API_KEY` is missing while extraction is enabled, the watcher still sends the filing notification. It then sends an "extraction failed" email for that filing instead of a review draft.

### 4. Configure what to watch

- **Price alerts:** add tickers and triggers to [`price_alerts/config.json`](#price_alertsconfigjson). It ships as `{"stocks": {}}`, which is valid and does nothing.
- **Filings:** [`filings/filings_config.json`](#filingsfilings_configjson) ships watching Omnicom (OMC) as an example. Keep it to try things out, or follow [Watch a different company](#watch-a-different-company). To get notifications only (no Claude calls), set `"extraction": {"enabled": false}`.

Commit your changes to the default branch. You can do this in GitHub's web editor or from a clone.

### 5. Test with **Run workflow**

Manual runs work before you turn the schedules on.

- **Price alerts:** go to **Actions → price-alerts → Run workflow**. Manual runs bypass the time gate, so this works at any hour. To force an email, temporarily add a trigger that is certainly true, such as `{"type": "price_above", "value": 1}` on a ticker you know. Run it, confirm the email arrives, then remove the trigger.
- **Filings watcher, first run:** go to **Actions → filings-watcher → Run workflow** and leave `force_accession` empty. The first run **seeds** `filings/state.json` with every existing filing and sends **no** email; otherwise you'd get the company's whole backlog. You should see a bot commit `chore(filings): update state/history [skip ci]` appear.
- **Filings watcher, end-to-end extraction test:** run it again and put an accession number in `force_accession`. Use a recent earnings 8-K (Item 2.02) or 10-Q from the watched company, which you can find on its EDGAR filing list. Dashes are optional. That filing is re-classified and re-extracted even though it's already marked as seen. You should receive the ℹ️ notification and then the review draft. This is the quickest way to judge extraction quality for your company. A forced run writes to `filings/history.json` the same way a live run does.

### 6. Turn on the schedules

Scheduled runs are **off by default**, so template copies and forks stay inert. To turn them on, open **Settings → Secrets and variables → Actions → Variables → New repository variable** and create either or both of these:

| Variable | Value | Turns on |
|---|---|---|
| `ENABLE_PRICE_ALERTS` | `true` | The `price-alerts` schedule |
| `ENABLE_FILINGS_WATCHER` | `true` | The `filings-watcher` schedule |

To pause a schedule, delete its variable or set it to anything other than `true`. Until you enable them, scheduled runs may still show up in the Actions tab as skipped. That's expected, and skipped jobs don't use Actions minutes.

---

## Configuration reference

All configuration is JSON. A syntax error such as a trailing comma breaks the run, so check a file before you commit it:

```sh
python -m json.tool price_alerts/config.json
python -m json.tool filings/filings_config.json
```

### `price_alerts/config.json`

This file maps each ticker to a list of triggers:

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

| `type` | Fires when |
|---|---|
| `price_above` | Current price (Finnhub `c`) **>** `value` |
| `price_below` | Current price **<** `value` |
| `percent_change_above` | Day % change vs. the previous close (Finnhub `dp`) **>** `value` |
| `percent_change_below` | Day % change **<** `value`. A value of `-5` means −5%. |

- Comparisons are strict (`>` / `<`). `value` must be a JSON number: not a string, and not `true`/`false`.
- **The whole file is validated before any API call.** If any entry is invalid (unknown `type`, non-numeric `value`, a ticker whose triggers aren't a list, and so on), the run prints `FATAL: invalid price_alerts/config.json:` with one line per problem and exits 1. **No alerts are checked for any ticker** until you fix it.
- An empty `stocks` map, or no `stocks` key at all, is valid. The run logs "nothing to do" and exits 0.
- For an unknown or unsupported symbol, Finnhub returns zeros. That ticker is skipped with a warning and never causes a false alert.
- To add a stock, add a key. To remove one, delete its key. The next run picks up the change.

### `filings/filings_config.json`

This is the only required file for Part 2. Only `company.name` and `company.cik` are mandatory. Every other key is optional and falls back to the default described below; if a rule needs a key that is missing, that rule is skipped. Keys that start with `_` (such as `_about`) are ignored, so you can use them for notes.

The shipped example:

```json
{
  "company": { "name": "Omnicom Group", "ticker": "OMC", "cik": "0000029989", "fiscal_year_end": "12-31" },
  "watched_forms": ["8-K", "8-K/A", "10-Q", "10-K", "10-Q/A", "10-K/A"],
  "user_agent_tool_name": "StockMonitor",
  "extraction": { "enabled": true, "model": "claude-sonnet-4-6", "max_tokens": 4000, "temperature": 0 },
  "profile": {
    "core_label": "Core Operations",
    "margin_label": "adjusted EBITA margin",
    "perimeter_section_hint": "Definitions — Components of Revenue Change",
    "segment": { "name": "Integrated Media", "plain_name": "Media buying", "share_of_core_pct": 51.5 },
    "guidance_reference_phrases": ["4% organic", "$900M synergies in 2026"],
    "goodwill_test_date": "06-30",
    "extra_anchor_phrases": ["Components of Revenue Change", "Definitions"],
    "plain_overrides": {}
  },
  "thresholds": {
    "buyback_red_floor_qtr_m": 700,
    "leverage_red_x": 3.0,
    "margin_yellow_pct": 15.0,
    "subtraction_growth_escalate_at": 4
  }
}
```

#### `company`

| Key | Required | Meaning / default |
|---|:-:|---|
| `name` | yes | The company name, used in emails and explanations. |
| `cik` | yes | The SEC Central Index Key. Leading zeros are optional (`"29989"` works the same as `"0000029989"`). |
| `ticker` | no | Used in email subjects and for the market-value-vs-book price lookup. Without it (missing, `null` or empty), every email subject uses the company name instead, and the market-value note reports that the ticker is missing. |
| `fiscal_year_end` | no | `"MM-DD"`, used to label fiscal quarters. If omitted, the watcher uses the `fiscalYearEnd` from SEC's submissions data, and falls back to December if that's missing too. A day in the first week of a month (e.g. `"02-01"`) is treated as a 52/53-week year that ends in the previous month (January). |

Quarter labels are fiscal: `YYYY-Qn`, where `YYYY` is the calendar year in which the fiscal year **ends**. For a December year-end these are ordinary calendar quarters. A company that names its fiscal years differently (for example, a retailer that calls the year ending January 2027 "fiscal 2026") will see a different year in the label than it uses itself.

#### `watched_forms`

This is the list of form types that generate emails. If omitted, it defaults to `8-K, 8-K/A, 10-Q, 10-K, 10-Q/A, 10-K/A`. It deliberately leaves out noisy forms such as Form 4, so add forms with care. Only 8-K Items 2.02 and 2.06 and 10-Q/10-K forms get special handling; any other form you add, and any 8-K that lists neither of those items, gets a plain notification.

#### `user_agent_tool_name`

The tool name in the SEC `User-Agent` header, `"<tool name> <SEC_CONTACT_EMAIL>"`. It defaults to `StockMonitor`.

#### `extraction`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | When `false`, earnings 8-Ks and 10-Q/10-Ks get only the notification. No Claude calls are made and no `ANTHROPIC_API_KEY` is needed. |
| `model` | `claude-sonnet-4-6` | The Claude model used for extraction. |
| `max_tokens` | `4000` | Output-token cap. If the model's JSON gets cut off at this limit, you receive an "extraction failed" email telling you to raise it. |
| `temperature` | `0` | Sent to the API only when it is not `null`. **Newer Claude models reject non-default sampling parameters with an error.** If you switch `model` to one of them, set `"temperature": null` so the parameter is omitted. |

#### `profile`

The profile holds the company-specific wording for the extraction prompt and the email text. Every key is optional:

| Key | Used for | If omitted |
|---|---|---|
| `core_label` | The name of the company's headline organic-growth measure or scope (e.g. "Core Operations"). Used in the prompt, in rule titles and in explanations. | The prompt asks for "the company's headline organic (or constant-currency, excluding acquisitions/divestitures) revenue growth". Explanations say "core business". |
| `margin_label` | The name of the headline adjusted margin (e.g. "adjusted EBITA margin"). | "adjusted operating margin (the company's headline adjusted/non-GAAP operating margin)" |
| `perimeter_section_hint` | The section title the model should read to decide whether the organic-growth perimeter changed. | "the section defining organic growth (or its non-GAAP reconciliation)" |
| `segment` | `{name, plain_name, share_of_core_pct}` for one segment to track against the group. `name` is also added as an excerpt anchor. `plain_name` is used in the email headline (it defaults to `name`). `share_of_core_pct` adds "(about X% of …)" to the explanation. | `segment_organic_pct` is dropped from the checklist and the `segment_lag` rule is skipped. |
| `guidance_reference_phrases` | Exact phrases from the company's current guidance. The model judges "softened" **relative to these** (softened or dropped = true, intact = false). | A generic criterion: softened only if the document explicitly lowers, withdraws or narrows toward the low end of previously issued guidance. Having no guidance at all doesn't count as softening. |
| `goodwill_test_date` | `"MM-DD"` of the company's annual goodwill impairment test. A 10-Q/10-K whose quarter contains that date gets a standing note in its review draft. | No standing note. |
| `extra_anchor_phrases` | Extra phrases used to excerpt long 10-Q/10-K documents. They are added to the built-in anchors ("Issuer Purchases of Equity Securities", "Unregistered Sales of Equity Securities", "repurchase", "leverage ratio", "Goodwill", "Impairment", "Organic", "Total leverage"). | Built-in anchors only. |
| `plain_overrides` | `{"<rule id>": ["headline", "meaning"]}` replaces a rule's plain-English text word for word. Rule ids are in the [alerts reference](#alerts-reference). | Built-in neutral text. |

#### `thresholds`

Each numeric rule reads its threshold from here. If a threshold is missing or `null`, that rule is skipped; there are no hidden defaults in the code.

| Key | Rule | Fires when |
|---|---|---|
| `buyback_red_floor_qtr_m` | `buyback_below_floor` (red) | Quarterly repurchases in $ millions **<** this value |
| `leverage_red_x` | `leverage_high` (red) | The reported leverage ratio **>** this value |
| `margin_yellow_pct` | `margin_floor` (yellow) | The adjusted margin in % **<** this value |
| `subtraction_growth_escalate_at` | `subtraction_growth` (yellow) | Total reported revenue has fallen while organic growth stayed positive for this many consecutive quarters (or more) |

### `filings/baselines.json` (optional)

This file holds optional reference values that enrich the notes and serve as fallbacks. A missing file and `{}` are both valid. The shipped values belong to the example company (OMC).

| Key | Used for |
|---|---|
| `baseline_asof` | The date shown next to the last-known buyback authorization |
| `organic_growth_history` | `[{"quarter": "YYYY-Qn", "organic_core_pct": n}]`. It's the fallback for the prior quarter's organic growth in `two_neg_organic` (`history.json` is checked first). It uses the same fiscal quarter labels. |
| `shares_outstanding_m` | The share-count note compares the extracted share count with this value |
| `buyback.authorization_remaining_b` | Shown as "last known" when a filing doesn't state the remaining authorization |
| `buyback.run_rate_qtr_m` | Adds "(run-rate ~$…M)" to the `buyback_below_floor` detail |
| `time_stop.review_after` / `time_stop.note` | Optional `"YYYY-MM-DD"` date. Once it arrives (in Eastern Time), one ⏱️ reminder email is sent, including `note` if you set one. `null` turns it off. |

> [!CAUTION]
> Baselines are **not** tied to a CIK. When you switch companies, replace this file with `{}` or with your company's values. Otherwise the example values will be compared against the new company.

### `filings/state.json` and `filings/history.json`

The workflow bot owns both files. Don't edit them by hand in normal use.

- **`state.json`** holds `{"cik", "seeded", "seen_accessions", "flags"}`. It is **bound to the watched CIK**. If you change `company.cik`, the next run logs a warning, discards the old state and **re-seeds silently**, with no email. An older seeded file that has no `cik` key simply adopts the current CIK. To force a clean re-seed, reset it to `{"flags": {}, "seeded": false, "seen_accessions": []}` or delete the file.
- **`history.json`** holds `{"<10-digit CIK>": {"<YYYY-Qn>": record}}`. Each record keeps every extracted field with its quote, location and source filing, plus the list of filings that contributed to it. Records for different companies never mix. When a later filing reports the same field for a quarter, its value replaces the stored one only if that filing is **not older**. On the same filing date, a 10-Q/10-K beats an 8-K. So the 10-Q refines the earlier earnings 8-K, but re-processing an old 8-K never overwrites the 10-Q. To start over, reset the file to `{}`.

### Repository variables

Set these under **Settings → Secrets and variables → Actions → Variables**. They aren't secret.

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_PRICE_ALERTS` | unset (off) | `true` turns on the `price-alerts` schedule |
| `ENABLE_FILINGS_WATCHER` | unset (off) | `true` turns on the `filings-watcher` schedule |
| `SMTP_HOST` | `smtp.gmail.com` | The SMTP server for a non-Gmail provider |
| `SMTP_PORT` | `465` | `465` uses implicit SSL. Any other port (e.g. `587`) uses STARTTLS. An invalid value makes the run fail with a `FATAL:` message the first time it tries to send an email; a run that sends nothing doesn't check it. |

Empty values count as unset. The server's TLS certificate is always verified. `EMAIL_ADDRESS` is used both as the SMTP login and the From address, so your provider has to accept the sending address as the username.

---

## Watch a different company

1. **Find the CIK.** Search for the company in [EDGAR company search](https://www.sec.gov/edgar/searchedgar/companysearch); the CIK is shown next to its name. SEC also publishes a ticker-to-CIK map at <https://www.sec.gov/files/company_tickers.json>.
2. **Edit `company`** in `filings/filings_config.json`: set `name`, `ticker` and `cik`, and set `fiscal_year_end` or remove it to use SEC's value.
3. **Review `profile`** against the company's latest earnings release and 10-Q/10-K. Set each key to the company's own wording, or delete keys that don't apply:
   - `core_label`: what the company calls its organic-growth scope, if it reports one
   - `margin_label`: its headline adjusted margin
   - `perimeter_section_hint`: the section that defines organic growth
   - `segment`: one large segment to compare with the group, or remove it
   - `guidance_reference_phrases`: short exact phrases from its current guidance, or remove them
   - `goodwill_test_date`: from the 10-K's critical accounting estimates, or remove it
   - `extra_anchor_phrases`: section titles in its 10-Q where these numbers appear

   Replace or empty `plain_overrides` as well.
4. **Set `thresholds`** at levels that make sense for this company, or delete any rule's key to turn that rule off.
5. **Replace `filings/baselines.json`** with `{}` or with this company's values. Baselines aren't tied to a CIK, so leftover example values would otherwise be used.
6. **Leave `state.json` alone.** The CIK change re-seeds it automatically and silently on the next run. `history.json` can stay as it is, because old records stay under the old CIK, or you can reset it to `{}`.
7. **Validate the JSON and commit.** Then use **Run workflow** once (this seeds silently), and again with `force_accession` set to a recent earnings 8-K and a recent 10-Q to check extraction quality before a live quarter depends on it.

---

## Alerts reference

Rules run after each successful extraction, against the quarter's merged record and earlier quarters in `history.json`.

| Rule id | Severity | Fires when | Configured by |
|---|---|---|---|
| `two_neg_organic` | 🔴 red | Core organic growth is negative this quarter **and** last quarter. The prior quarter comes from `history.json`, then `baselines.organic_growth_history`. | always on |
| `buyback_suspended` | 🔴 red | The filing contains language suspending, pausing or reducing the buyback | always on |
| `buyback_below_floor` | 🔴 red | Quarterly repurchases in $M are below the floor | `thresholds.buyback_red_floor_qtr_m` |
| `leverage_high` | 🔴 red | The reported leverage ratio is above the ceiling | `thresholds.leverage_red_x` |
| `goodwill_impairment` | 🔴 red | A goodwill impairment charge is reported. This backs up the instant Item 2.06 alert. | always on |
| `segment_lag` | 🟡 yellow | Segment organic growth is below group organic growth this quarter **and** last quarter | `profile.segment` |
| `guidance_softened` | 🟡 yellow | The model judged guidance softened | `profile.guidance_reference_phrases` (optional; a generic criterion applies without it) |
| `margin_floor` | 🟡 yellow | The adjusted margin is below the floor | `thresholds.margin_yellow_pct` |
| `goodwill_cushion` | 🟡 yellow | The smallest goodwill cushion (10-K) is lower than the most recent earlier one found within two years | always on (needs two annual data points) |
| `subtraction_growth` | 🟡 yellow | Total reported revenue has declined while organic growth stayed positive for N consecutive quarters | `thresholds.subtraction_growth_escalate_at` |
| `perimeter_changed` | ⚪ watch | The organic-growth perimeter / disposal scope changed. This quarter's organic figure isn't directly comparable with earlier ones. | wording from `profile.core_label` / `perimeter_section_hint` |

"Watch" items appear in the email but don't raise its severity.

**Informational notes** appear under *WORTH KNOWING* in each review draft:

- If organic growth is negative but no prior-quarter figure is stored, a note that the two-quarter check couldn't be evaluated yet.
- The goodwill cushion (when extracted) vs. the previous one, if the cushion rule didn't fire.
- The length of the subtraction-growth streak, or "not evaluable".
- The share count vs. `baselines.shares_outstanding_m`.
- The remaining buyback authorization: stated in the filing, or the last known value from baselines.
- The market value vs. book value. This needs `FINNHUB_API_KEY`, `company.ticker`, and an extracted share count and book equity; otherwise the note says what is missing.
- For a 10-Q/10-K whose quarter contains `profile.goodwill_test_date`, a standing note that this filing covers the annual goodwill test.

---

## Operational notes

### Schedules and daylight saving

- **price-alerts:** GitHub cron runs in UTC and ignores US daylight saving, so the workflow has four crons (`45 14`, `45 15`, `5 20` and `5 21`, Mon–Fri). There's one per window for summer (EDT) and one for winter (EST). The workflow passes the cron that fired to the script as `SCHEDULE`. The script then continues only if that cron is the **correct-season** one for a window, meaning it lands exactly on 10:45 or 16:05 ET, **and** the run started within that window's accepted span. The accepted spans are 10:45–11:30 ET and 16:05–16:35 ET: 20 minutes of grace past each window's end, capped at 45 minutes after its start. The wrong-season fire exits cleanly, however late GitHub starts it, so a delayed wrong-season run can't send a duplicate email. If `SCHEDULE` is missing, the script falls back to a plain wall-clock check. Manual runs always bypass the gate.
- **filings-watcher:** `0 * * * 1-5` means hourly, Monday–Friday **in UTC**. In Eastern Time that covers roughly Sunday evening through Friday evening, so a filing made late on a Friday evening is noticed on Sunday evening.

### Scheduled runs are best-effort

GitHub often starts scheduled runs a few minutes late and occasionally skips one. Both scripts tolerate delays: the price gate has its grace period, and the watcher simply catches up on the next hour. Scheduled workflows only run from the default branch.

### First run seeds silently

With `"seeded": false` (as shipped), the first watcher run records **every** existing filing as seen and sends **nothing**. Only filings that appear after that generate emails. The same silent re-seed happens automatically when you change `company.cik`.

### Backlog safety valve

If a normal run finds **more than 20** new watched filings at once, `filings/state.json` doesn't match the watched company (for example, it was copied from somewhere else). The watcher then sends **no** emails, prints a `FATAL:` explanation and exits 1. To fix it, reset `state.json` to `{"flags": {}, "seeded": false, "seen_accessions": []}` (or delete it) and run again to re-seed. Forced runs and seeding don't go through this check.

### Failures are loud

- **When a scheduled run fails, GitHub emails you.** By default it notifies whoever created the workflow or last changed its schedule, subject to their GitHub notification settings.
- **price-alerts exits 1** in these cases:
  - a secret is missing
  - `config.json` can't be read or is invalid
  - Finnhub rejects the key (HTTP 401/403 for any ticker)
  - the quote fetch fails for **every** ticker
  - the alert email can't be sent (for example, the SMTP server is down or `SMTP_PORT` is invalid)

  Any alert email for the triggers that did fire is still sent first. If only some tickers fail, the run logs warnings and exits 0.
- **filings-watcher exits 1** in these cases:
  - a secret is missing
  - `filings_config.json` is missing, or lacks `company.cik` / `company.name`
  - `filings_config.json`, `state.json` or `baselines.json` contains invalid JSON
  - the SEC submissions request fails
  - `force_accession` is malformed or isn't among the recent filings
  - the backlog valve trips
  - `SMTP_PORT` is invalid (detected when the run first sends an email)
  - an email can't be sent during a forced (`force_accession`) run
- **Extraction problems arrive as emails**, not failed runs. You always get the filing notification first. If extraction fails, you then get an "extraction failed" or "review step failed" email with the error, links and the exact `force_accession` value to retry.
- **Retries:** if an email can't be sent at all during a normal run (for example, the SMTP server is down), the run still exits 0, the filing stays unseen and the next hourly run tries it again. You may then receive a duplicate notification. The state commit step runs even after a failed watcher run, so progress made before the failure is kept.

### SEC etiquette

Every request to SEC hosts sends `User-Agent: <user_agent_tool_name> <SEC_CONTACT_EMAIL>`. A quiet run makes a single request, for the company's submissions data. Extraction downloads a few documents and pauses between them. Keep it that way: the SEC blocks anonymous or aggressive clients.

### Cost

- **Finnhub:** the free tier is enough. Each price run makes one request per ticker, spaced 1.1 s apart. The watcher makes at most one extra request per review draft, for the market-value note.
- **SEC EDGAR:** free.
- **Anthropic:** pay per use, and only when an earnings 8-K or 10-Q/10-K arrives (or you force one). For a typical quarterly filer that's roughly 8–12 calls a year. Each call sends at most about 200,000 characters of document text and gets back at most `max_tokens` tokens, so the yearly total is usually small. Check current pricing for the model you configure.
- **GitHub Actions:** private repositories use your account's included Actions minutes. With both schedules on, expect roughly 520 watcher runs and 90 price runs a month. Each run is billed as at least one minute, so that's somewhere above 600 minutes a month. Compare that with your plan's allowance. Skipped runs (schedule not enabled) cost nothing.

### Schedules paused after inactivity

In **public** repositories, GitHub disables scheduled workflows after 60 days without any repository activity. The watcher commits only when state changes, which means only when a new filing arrives, so a quiet company may not keep the repository active. If a workflow shows as disabled in the Actions tab, open it and click **Enable workflow**.

### Branch protection

The bot pushes state commits straight to the branch the workflow ran on. If that branch requires pull requests or signed commits, the push fails after three attempts. In that case, allow the bot or relax the rule.

### Optional: what this project doesn't watch

News and credit ratings are deliberately **not** scraped. Official alert services are more reliable, and scraping is fragile and usually against terms of service. If you want broader coverage, set these up by hand:

1. The company's investor-relations email alerts (press releases, events).
2. Saved searches in [EDGAR full-text search](https://www.sec.gov/edgar/search/) for keywords such as `"Item 2.06"` or `impairment`, limited to the company.
3. News alerts (for example, Google Alerts) for the company name together with terms like `rating`, `outlook` or `downgrade`.
4. Calendar entries for expected earnings dates, so you know when review drafts should arrive.

---

## Security and privacy

- **Use a private repository.** See [Quick start, step 1](#1-create-your-own-copy-as-a-private-repository).
- **Credentials live only in Actions secrets.** Nothing is hard-coded, and `.env` is in `.gitignore` for local work. Never commit keys or passwords.
- **Workflows use least privilege.** `price-alerts` can only read repository contents. `filings-watcher` has no default token permissions; only its job gets `contents: write`, which it needs to push state. Checkout uses `persist-credentials: false`, so the token isn't left in the git config; the commit step uses it only to pull and push the state commit.
- **Third-party actions are pinned** to full commit SHAs (`actions/checkout`, `actions/setup-python`).
- **Workflow inputs are handled safely.** `force_accession` reaches the script through an environment variable, never interpolated into a shell command, and the script checks it against the accession-number format before using it.
- **Logs are kept quiet.** The emailer doesn't print the recipient address. The Finnhub key is masked in fetch-error messages (the price-alert logs and the review draft's market-value note), and GitHub masks secrets in logs as well.
- **Data leaves the repository only where it has to:**
  - your contact email goes to the SEC in the `User-Agent` header
  - public filing text goes to Anthropic when extraction is enabled
  - tickers go to Finnhub
  - alert emails go through your SMTP provider
- **Copies start inert.** Scheduled jobs do nothing until you set `ENABLE_PRICE_ALERTS` / `ENABLE_FILINGS_WATCHER`.
- **App passwords can be revoked.** A Gmail app password can be revoked at any time from your Google Account without changing your main password.

---

## Development

```sh
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest
```

The test suite runs offline. Finnhub, SEC, Anthropic and SMTP are all monkeypatched, so it needs no keys and sends nothing.

**Running the scripts locally.** Run from the repository root with the secrets exported in your shell. These are real runs: they call the APIs and send email.

```sh
# Price alerts. Outside GitHub the time gate uses the wall clock, so pretend to be a manual run to bypass it:
GITHUB_EVENT_NAME=workflow_dispatch python -m price_alerts.alerts

# Filings watcher (needs SEC_CONTACT_EMAIL, the EMAIL_* variables, and ANTHROPIC_API_KEY for extraction):
python -m filings.watcher
FORCE_ACCESSION=0000000000-00-000000 python -m filings.watcher   # re-process one filing
```

A local watcher run rewrites `filings/state.json` and `filings/history.json` in your working copy. The bot owns those files, so discard those changes rather than committing them unless you mean to. Set `SMTP_HOST` / `SMTP_PORT` locally the same way you would the repository variables.

---

## Limitations and non-goals

- **No trading or broker integration.** This project sends email. It never trades and it doesn't give advice.
- **Price alerts don't de-duplicate.** A condition that stays true alerts on every run. It also has no market-holiday calendar: on a weekday holiday it runs anyway against the last available quote.
- **One company per repository.** The filings watcher follows a single `company`. To watch several companies, use several copies.
- **US SEC filers only.** The watcher gives special handling only to 8-K Items 2.02 (earnings, extracted) and 2.06 (instant red alert, no extraction) and to 10-Q/10-K forms (extracted). Other forms, including foreign issuers' 6-K/20-F if you add them, only get plain notifications.
- **The checklist is fixed.** It suits companies that report organic growth, buybacks, a leverage ratio and goodwill. For other companies many fields come back "not found", and the rules that depend on them never fire.
- **LLM extraction is a draft.** Values can be missing or wrong. Every value comes with a quote so you can check it.
- **`force_accession` reaches only recent filings**: about the most recent 1,000 (what the SEC submissions API lists).
- **Scheduling is coarse.** The watcher checks hourly on weekdays (UTC), and GitHub schedules are best-effort.
- **No news or ratings scraping.** See [the optional manual list](#optional-what-this-project-doesnt-watch).

---

## License

[MIT](LICENSE)
