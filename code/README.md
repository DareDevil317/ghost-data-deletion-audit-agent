# Ghost Data Still Exists — Deletion-Audit Agent

An agent that checks whether a customer's personal data is really gone, and if not,
produces the ordered findings → remediation → verification plan required by policy.

## Quick start

Requires Python 3.10+ and nothing else. `pypdf` and the `tesseract`/`pdftotext` tools are
optional fallbacks for reading PDFs and screenshots without a model.

```bash
# from the repository root
cp code/.env.example .env          # then put your Gemini API key in .env (never commit it)
python3 code/main.py               # writes ./output.csv and code/evaluation/usage_report.md
python3 code/evaluation/main.py    # scores the agent and updates evaluation_report.md
```

Offline mode, with no model calls, uses keyword rules for intent and local OCR/text extraction for media:

```bash
python3 code/main.py --no-llm
```

Optional extras for the offline fallbacks:

```bash
pip install pypdf                     # PDF text
sudo pacman -S tesseract tesseract-data-eng   # or: apt install tesseract-ocr
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (none) | Google AI Studio key. Without it the agent runs fully offline. |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Any Gemini model with JSON output and image input. |

## Architecture

```
deletion_requests.csv ──► 1. Intent ──────────────► NOT_APPLICABLE → []
                              (LLM batch + rules,
                               self-check on disagreement)
                                   │ DELETE
PDF / PNG evidence ─────► 2. Media extraction ──┐
                              (LLM vision, OCR    │
                               fallback, cached)  ▼
SQL / JSON / logs / txt ─► 3. Evidence parsers ─► normalized Observations
system_activity.csv ─────┘                          (source, time, resource, exists,
                                                     identifiers, flags)
                                   │
                                   ▼
                          4. Deterministic auditor
                             a. identity: current + historical email/phone/hash/user_id
                             b. link resources: identifier matches, data-catalog links,
                                deletion-workflow activity attributed to this request
                             c. state at audit_at: latest admissible evidence
                                (claims in activity logs are never proof)
                             d. policy: deletion_rules.csv per data category
                             e. steps: FINDING → REMEDIATION → VERIFICATION, sorted
                                by operation order, system_id, resource_id
                                   │
                                   ▼
                          output.csv  +  code/outputs/trace.jsonl
```

**Why hybrid?** The model handles the two genuinely fuzzy parts: multilingual intent, and reading
screenshots and PDFs. Everything with an exact answer is code: identity joins, timelines, policy,
ordering, and JSON shape. That keeps the output deterministic, cheap at about 0.05 model calls
per request, and explainable step by step.

### Key decisions

| Problem | Decision |
|---|---|
| Reported statuses lie | `reported_status` and activity `reported_result` are only claims. State comes from evidence observations: SQL rows, read-back logs, exports, screenshots. |
| Time travel | Only observations at or before `audit_at` count. Only files whose `captured_at` ≤ `audit_at` are cited. A fact observed before `audit_at` but exported in a later file can inform state, but is never cited. |
| Historical identities | Every old/new email, phone and email hash from `user_audit_log.csv` is a search token, and emails are case-normalized. Exact matching avoids look-alike decoys such as `name.jr@…`. |
| Resources with no identifiers | The deletion worker's own actions near `reported_at` are attributed to the request. Several requests can share a window, so the auditor excludes resources that evidence ties to someone else. It then matches the workflow's status claim to its profile-deletion result (`COMPLETE`↔`SUCCESS`, `PARTIALLY_COMPLETE`↔`PARTIAL`, `NOT_STARTED`↔`FAILED`) and prefers requests that still lack a resource in that system. |
| Proving absence | A primary-table snapshot that lacks the record, or the identity service's deleted-account reference, proves a profile is gone. A scoped inventory snapshot is the weaker fallback. |
| Category vs system | The evidence `record_type` wins. A support item the workflow tried to *delete* is an attachment, so it is deleted rather than redacted. |
| Same person, two requests | Records only ever seen before this request belong to the earlier deletion episode. |
| Retention | Security audit records and legal holds are verified `PRESENT`, never deleted. Backups are retained but restore must be `BLOCKED`. Tombstones keep `user_id` but lose name, email, phone and hash. |

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point / orchestration |
| `agent/evidence.py` | Parsers for every evidence format → `Observation` |
| `agent/identity.py` | Current + historical identifier resolution |
| `agent/auditor.py` | Resource linking, state reconstruction, policy, step ordering |
| `agent/intent.py` | Deletion-intent classifier: LLM prompts, rules fallback, self-check |
| `agent/media.py` | PDF/screenshot extraction: LLM vision prompt, OCR fallback |
| `agent/llm.py` | Minimal Gemini REST client with token accounting (standard library only) |
| `agent/report.py` | Generates `evaluation/usage_report.md` from the real run |
| `evaluation/main.py` | Evaluation workflow (samples + custom cases, both modes) |
| `evaluation/scoring.py` | Metrics |
| `evaluation/custom_cases/` | Our additional test cases and expected answers |
| `cache/` | Cached model answers, keyed by content hash |
| `outputs/trace.jsonl` | Per-case trace: subject, intent and its source/reason |
