# Rigorlight 🚦

**A traffic light for scientific rigor.**

Rigorlight screens scientific papers for signs of sloppiness and gives each
check a traffic light - 🟢 passed, 🟡 some flaws, 🔴 failed, ⚪ could not check -
always with the evidence behind it.

It does **not** judge scientific merit or impact; that remains a job for human
reviewers. Results are flags for human review, not verdicts.

## Planned checks

| Check | What it does |
|---|---|
| Main claims | Extracts the three main claims, each anchored to a verbatim quote |
| Grounding | Rates how well each claim is supported by the paper's evidence and citations |
| References | Verifies every reference exists (DOI/Crossref/OpenAlex), matches, isn't retracted, and is relevant |
| Prior work | Searches the literature for closely related work the paper doesn't cite |
| AI-generation traces | Looks for concrete traces (leftover chatbot phrases, tortured phrases, hidden prompt injections) |

## Status

Early development. Currently in place: project structure and a hard monthly
spending cap for all LLM calls.

## Getting started

```bash
cp .env.example .env          # then fill in your keys and MONTHLY_BUDGET_USD
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
docker compose up -d grobid   # PDF parser on http://localhost:8070
```

## Costs

Rigorlight refuses to call the Claude API unless a monthly budget is set.
See [docs/COSTS.md](docs/COSTS.md) - please also set a spend limit in the
Anthropic Console.

## License

MIT
