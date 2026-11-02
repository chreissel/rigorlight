# Keeping costs under control

Rigorlight has **two independent spending caps**. Set both to the same number.

## 1. Anthropic Console limit (enforced by Anthropic - the real safety net)

Even if Rigorlight's code has a bug, Anthropic will stop serving requests once
this limit is hit.

1. Go to <https://console.anthropic.com> and create a **separate workspace**
   called `rigorlight` (Settings -> Workspaces).
2. In that workspace, set a **monthly spend limit** to about 10% of your total
   monthly API budget.
3. Create the API key for Rigorlight **inside that workspace**, and use it only
   for Rigorlight. Your other projects use keys from other workspaces, so
   Rigorlight can never eat into their budget.
4. Optionally add an email alert at ~80% of the limit.

The exact menu names may change; look for "Workspaces", "Limits" and
"Spend limit" in the Console settings.

## 2. Rigorlight's own budget guard (`backend/rigorlight/budget.py`)

- `MONTHLY_BUDGET_USD` in `.env` is **required**; without it, no API call is made.
- Before each call, the worst-case cost (all input + the full `max_tokens` output,
  at the most expensive rate the request could be billed at) is reserved. If it
  would exceed the monthly budget, the call is refused before anything is sent.
- After the call, the reservation is replaced by the real cost from the API response.
- All API calls must go through `backend/rigorlight/llm.py`; a test fails if any
  other module imports `anthropic`.
- Check usage any time:

  ```bash
  python -m rigorlight.budget
  ```

The local ledger lives in `data/spend_ledger.sqlite3` (not committed). Prices
are in `PRICES` in `budget.py`; re-check them when switching models.

## Note on Claude subscriptions

API usage is billed separately from a Claude.ai subscription (Pro/Max/Team).
The cap above controls what *Rigorlight* spends via the API. Your use of Claude
Code or the Claude apps while developing counts against your subscription's own
usage limits, which this code cannot control.
