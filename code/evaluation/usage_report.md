# Token Usage and Cost Report

Generated automatically by `code/main.py` at the end of the run (2026-10-25 10:13 UTC).

## Run summary

- Requests audited: **518**
- LLM enabled for this run: **yes**
- Intent decisions by source: {'llm': 0, 'cache': 398, 'rules': 0, 'llm_rule_disagreements': 0}
- Media (PDF/image) extractions by source: {'llm': 0, 'cache': 15, 'fallback': 0, 'unreadable': 0, 'llm_records_rejected': 0}
- Wall-clock runtime: 10.1 s

Cached answers (`cache`) come from earlier runs and cost no tokens in this run.

## Per-model usage

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (paid list price) |
|---|---|---:|---:|---:|---:|---:|
| **Overall** | | **0** | **0** | **0** | **0** | **$0.0000** |

## Per-request averages

- Model calls per request: 0.000
- Average tokens per request: 0.0 (input 0.0, output 0.0)
- Estimated cost per request at paid list prices: $0.000000
- Actual cost of this run: **$0.00** (Gemini API free tier).

## Calls by purpose

- none (no model calls were made in this run)

## Why so few tokens

- Only the two fuzzy sub-problems use a model: request intent (multilingual free text) and reading PDFs/screenshots. Identity resolution, timeline reconstruction, policy and step ordering are deterministic code, so they cost nothing and cannot hallucinate.
- Request texts are de-duplicated and classified in batches of 40.
- Every model answer is cached by content hash, so re-runs only pay for new inputs.

Price assumptions (USD per 1M input/output tokens): `gemini-3.5-flash-lite` 0.3/2.5, `gemini-3.1-flash-lite` 0.25/1.5, `gemini-3.6-flash` 0.75/3.75, `gemini-3.8-flash` 0.75/3.75.
