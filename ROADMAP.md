# Rigorlight roadmap

Ordered to-do list. Tick items off as they are done (`- [x] ~~item~~`).
🚧 = in progress.

## A. Setup
- [x] ~~1. Create GitHub repository `chreissel/rigorlight` with README, `.gitignore` and MIT license~~
- [ ] 2. Install Docker Desktop and Python 3.12 on your laptop (done when `docker run hello-world` works)
- [ ] 3. Anthropic API key — **only needed from section E on.** Create a separate `rigorlight` workspace in the Console, buy a small prepaid credit ($10–20) with auto-reload **off** (see [docs/COSTS.md](docs/COSTS.md))
- [ ] 4. Request a free Semantic Scholar API key (approval can take days)
- [x] ~~5a. `.env.example` template, `.env` excluded via `.gitignore`~~
- [ ] 5b. Create your local `.env` from `.env.example` and fill in `CONTACT_EMAIL`, `MONTHLY_BUDGET_USD`
- [ ] 6. Ask your department / MIT IT services about cloud or LLM research credits
- [x] ~~Name chosen (Rigorlight) and availability checked (web, GitHub repos, trademarks)~~
- [ ] Register domain `rigorlight.org` (and `.com`) and claim the `rigorlight` GitHub organization if wanted
- [x] ~~Hard monthly spending cap for all Claude API calls (`budget.py`, `llm.py`, enforced by tests)~~

## B. First test papers
- [ ] 7. Collect 10 papers in `eval/papers/`: 5 solid, 2 retracted, 2 with fabricated references, 1 of your own
- [ ] 8. Create `eval/labels.csv`: file, DOI, expected light per check

## C. PDF parsing
- [x] ~~9. GROBID in `docker-compose.yml`~~ (still to try locally: `docker compose up -d grobid`, open `localhost:8070`)
- [ ] 🚧 10. `ingest/grobid.py`: PDF → title, abstract, sections, references
- [ ] 11. Run it on all 10 test papers; open an issue for each parsing failure
- [ ] 🚧 12. Input by DOI (Crossref metadata, arXiv PDF)

## D. Reference check (no AI, free)
- [ ] 🚧 13. Resolve references with a DOI via Crossref, compare titles
- [ ] 🚧 14. References without DOI: OpenAlex title search, fuzzy match ≥ 0.9
- [ ] 🚧 15. Label each: verified / mismatch / not_found / retracted / unverifiable
- [ ] 🚧 16. Check cited URLs; Wayback Machine fallback
- [ ] 🚧 17. Shared result format `{status, summary, evidence[]}`
- [ ] 🚧 18. Traffic-light rules; check against your 10 labelled papers
- [ ] 19. Review & merge the `reference-checker` branch, tag `v0.1` 🎉

## E. Claims and grounding (uses the API)
- [ ] 20. `prompts/claims_v1.md`: 3 claims with verbatim quote + section, JSON output
- [ ] 21. Verify in code that each quote really appears in the paper
- [ ] 22. `prompts/grounding_v1.md`: fixed rubric of 4–5 yes/no questions per claim
- [ ] 23. Fetch abstracts of papers cited next to each claim
- [ ] 24. Rubric answers → traffic light by rules in code
- [ ] 25. Prompt-injection defence: strip hidden text, pass paper as data
- [ ] 26. Run on test papers, compare with labels, tag `v0.2`

## F. Prior work and AI-generation traces
- [ ] 27. Claims → search queries for Semantic Scholar and OpenAlex
- [ ] 28. Rank results by similarity, keep top 10
- [ ] 29. LLM comparison → "closely related uncited work" (never red unless near-duplicate)
- [ ] 30. `ai_traces.py`: chatbot phrases, tortured phrases, hidden text
- [ ] 31. `eval/run_eval.py`: agreement of each light with labels, tag `v0.3`

## G. Web app
- [ ] 32. FastAPI: `POST /submit`, `GET /status/{id}`, `GET /report/{id}`
- [ ] 33. Background worker (RQ + Redis)
- [ ] 34. Store jobs and reports in Postgres
- [ ] 35. Extend `docker-compose.yml` with API, worker, Redis, Postgres (GROBID already in)
- [ ] 36. Simple page: input box, report with lights + evidence, "not an assessment of scientific merit"
- [ ] 37. Delete uploaded PDFs after processing
- [x] ~~38. GitHub Action running the tests on every push~~

## H. Go online
- [ ] 39. Rent a VPS (4 vCPU / 8 GB), unless MIT provides hosting
- [ ] 40. Domain pointing to the server
- [ ] 41. Deploy with `docker compose` + Caddy for HTTPS
- [ ] 42. Per-IP daily submission limit + spending alert

## I. Beta test
- [ ] 43. Expand test set to ~50 papers across ≥ 3 fields
- [ ] 44. Invite 5–10 colleagues; "disagree with this light?" button
- [ ] 45. Add disagreements to the test set; re-run eval after each prompt change
- [ ] 46. Decide on open-sourcing; GitHub/ORCID login
