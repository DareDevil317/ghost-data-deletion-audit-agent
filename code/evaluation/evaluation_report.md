# Evaluation Report

## How we evaluate

`python3 code/evaluation/main.py` runs the **complete agent**: intent, media, evidence, audit
and output. It runs on two suites and scores the result automatically.

1. **Public samples:** the 32 requests in `dataset/sample_deletion_requests.csv`, compared
   with `dataset/sample_output.csv`. The other 518 requests are passed in as well. They are
   not scored, but they compete for shared deletion-workflow activity exactly as in the real run.
2. **Custom cases:** five cases we wrote ourselves (below), each with an expected answer.

Each suite runs in **rules-only mode** with no model calls. When `GEMINI_API_KEY` is set, it
also runs in **LLM mode**, which gives a model-vs-rules comparison.

### Metrics (`code/evaluation/scoring.py`)

| Metric | Definition |
|---|---|
| Status accuracy | `deletion_status` exactly right, per case |
| Step precision / recall / F1 | A predicted step counts as correct only if *all* of `phase, system_id, resource_id, operation, state, evidence_id` match an expected step |
| Resource recall | Share of expected `(system_id, resource_id)` pairs that appear anywhere in our steps, which measures discovery |
| State accuracy (aligned) | For steps matched on `(phase, operation, system_id, resource_id)`, how often `state` is right (field level) |
| Evidence accuracy (aligned) | Same alignment, how often `evidence_id` is right (field level) |
| Exact sequence | The whole `steps_json` array is identical, order included |

<!-- METRICS:START -->
### Latest automated results

| Mode | Suite | Cases | Status acc. | Step precision | Step recall | Step F1 | Resource recall | State acc. (aligned) | Evidence acc. (aligned) | Exact sequence |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| rules-only | public samples | 32 | 96.9% | 98.9% | 94.8% | 96.8% | 93.7% | 100.0% | 99.5% | 78.1% |
| rules-only | custom cases | 5 | 100.0% | 95.7% | 95.7% | 95.7% | 90.9% | 100.0% | 100.0% | 80.0% |
| llm | public samples | 32 | 100.0% | 99.5% | 96.9% | 98.2% | 94.6% | 100.0% | 100.0% | 81.2% |
| llm | custom cases | 5 | 100.0% | 95.7% | 95.7% | 95.7% | 90.9% | 100.0% | 100.0% | 80.0% |

Failing (non-exact) cases:

| Mode | Case | Expected status | Predicted status | Steps matched | Missing resources | Extra resources |
|---|---|---|---|---|---|---|
| rules-only | case_07 | INCOMPLETE | COMPLETE | 0/4 | support_001 | - |
| rules-only | case_18 | INCOMPLETE | INCOMPLETE | 13/14 | audit_record_6645 | - |
| rules-only | case_20 | INCOMPLETE | INCOMPLETE | 7/8 | audit_record_5569 | - |
| rules-only | case_21 | COMPLETE | COMPLETE | 2/3 | analytics_5335 | - |
| rules-only | case_23 | INCOMPLETE | INCOMPLETE | 7/8 | account_410 | account_7015 |
| rules-only | case_25 | COMPLETE | COMPLETE | 2/3 | analytics_1895 | - |
| rules-only | case_26 | COMPLETE | COMPLETE | 5/6 | analytics_4566 | - |
| rules-only | custom_02 | INCOMPLETE | INCOMPLETE | 13/14 | account_7012 | account_5482 |
| llm | case_18 | INCOMPLETE | INCOMPLETE | 13/14 | audit_record_6645 | - |
| llm | case_20 | INCOMPLETE | INCOMPLETE | 7/8 | audit_record_5569 | - |
| llm | case_21 | COMPLETE | COMPLETE | 2/3 | analytics_5335 | - |
| llm | case_23 | INCOMPLETE | INCOMPLETE | 7/8 | account_410 | account_7015 |
| llm | case_25 | COMPLETE | COMPLETE | 2/3 | analytics_1895 | - |
| llm | case_26 | COMPLETE | COMPLETE | 5/6 | analytics_4566 | - |
| llm | custom_02 | INCOMPLETE | INCOMPLETE | 13/14 | account_7012 | account_5482 |

<!-- METRICS:END -->

## Additional evaluation cases created by us

Requests are in `code/evaluation/custom_cases/custom_requests.csv`, and expected answers are in
`custom_expected.csv`. Each case is built on a real subject from the dataset, so the evidence
is real. The `based_on` column records which sample it stands in for.

| Case | Built on | What it tests | Expected |
|---|---|---|---|
| `custom_01` | case_29 subject | **Non-deletion request in Spanish.** The customer wants a copy of their data and explicitly says "do not delete or close my account". Nothing may be deleted even though the subject has many leftover records. | `NOT_APPLICABLE`, `[]` |
| `custom_02` | case_29 | **Historical identity, upper-cased.** `NOAH.F438085@OUTLOOK.COM` is the subject's *previous* email, in capitals. It must resolve to the same user and produce the same 14-step plan. | Same as case_29 (`INCOMPLETE`, 14 steps) |
| `custom_03` | case_08 | **Keyword trap.** A real deletion request containing "download your data" and "only", which are words our non-deletion rules look for. | Same as case_08 (`INCOMPLETE`) |
| `custom_04` | case_12 | **Korean-language request** for a subject whose email changed before deletion, with the leftover record only findable via the old email's hash. | Same as case_12 (`INCOMPLETE`, delete analytics record) |
| `custom_05` | case_24 subject | **Polite refusal.** "Please do not delete anything yet. I only want to know which systems still hold my profile." | `NOT_APPLICABLE`, `[]` |

## Failures found and improvements made

We built the auditor iteratively and re-ran the sample evaluation after every change.
Numbers are on the 32 public samples.

| # | Failure found | Improvement | Status acc. | Step F1 | Exact seq. |
|---|---|---|---:|---:|---:|
| 1 | First version linked every deletion-worker action between `requested_at` and `reported_at`+3h. That swept in other customers' resources, so precision was 33%. | — | 93.8% | 48.2% | 3.1% |
| 2 | The workflow acts in a window around `reported_at`, and many requests share that window. | ±2 h window around `reported_at`. All requests compete for each action. Resources that evidence ties to another person are excluded. | 93.8% | 91.2% | 50.0% |
| 3 | Concurrent requests got the wrong profile record. Tombstones were treated as absent because `account_status=DELETED`. Absent profiles had no evidence to cite. | Two changes, measured together. (a) The workflow's status claim comes from its own profile-deletion result, so we match `COMPLETE`↔`SUCCESS`, `PARTIALLY_COMPLETE`↔`PARTIAL`, `NOT_STARTED`↔`FAILED`, and skip requests that already have a resource in that system. (b) `DELETED` means absent only for profiles, and absence is proven by a primary-table snapshot that lacks the record or by the subject's deleted-account reference. | 96.9% | 93.8% | 53.1% |
| 4 | Retained audit records disappeared: an *older* snapshot without the record was used as an absence proof, while the newer positive fact sat in a file exported after `audit_at`. Our first fix used that newer fact *everywhere*, which blanked valid citations (F1 fell to 81.0%). | Facts observed before `audit_at` but exported in a later file now set the state only when nothing citable exists, and are never cited. | 96.9% | 95.2% | 65.6% |
| 5 | A subject who asked twice got the first request's records. Some profiles have no ID anywhere in the evidence. | Records only seen before the new request belong to the earlier episode. A naming-convention fallback (`account_NNN` ↔ `user_NN`) is used only when the ID is not tied to anyone else. | 96.9% | 96.0% | 71.9% |
| 6 | Adding scoped inventories as absence proofs cited a later, weaker file instead of the account snapshot (F1 dropped to 95.5%). | Tiered proofs: the latest of primary snapshot or deleted-account reference first, inventories only as a fallback. | 96.9% | 96.0% | 71.9% |
| 7 | A PDF support attachment was redacted instead of deleted. | Media extraction plus a rule that a support record which *is* an uploaded document is an attachment. The earlier-episode rule was generalized to all systems. | 100% | 98.2% | 81.2% |
| 8 | **First live Gemini run** (on a Windows machine with no local OCR/PDF tools): the model got empty PDF text and returned no records for all 4 PDFs, returned nothing for several screenshots, and invented one id (`support_7` for `support_1034`). case_07 failed in LLM mode. | PDFs are sent to Gemini as files; the model must return a full transcript plus records; a record is kept only if its id (and email) appear in the transcript; the deterministic line parser also runs over the transcript. Result on that machine: 21 records instead of 11, the invented id gone. | 96.9% | 96.8% | 78.1% |
| 9 | case_07 still failed: Gemini reported the date *printed inside* the PDF (form submitted 18 Jun) as the observation time, so the record looked older than the customer's earlier deletion episode and was dropped. It also typed the attachment as a support message. | A screenshot or document shows state **at its capture time**, so media observations always use the file's `captured_at`. A support record whose evidence is an uploaded PDF is always an attachment, whatever the model says. | **100%** | **98.2%** | **81.2%** |

The intent rules were tested against every request text. The first version flagged four genuine
deletion requests as non-deletion, because "copy of", "download" and "only" appeared inside them.
The rules were rewritten so that explicit refusals are checked first and request-type words are
no longer decisive. That gave zero mismatches. When the model is enabled, any disagreement
between model and rules triggers a second, focused model call, and that answer wins.

## Known limitations (honest)

- **Unknowable IDs:** `audit_record_6645` (case_18) and `audit_record_5569` (case_20) appear in
  no participant-visible file, so no evidence-based method can find them.
- **Ambiguous batches:** when several verification-only requests share one workflow batch and
  the anonymized analytics records carry no identifiers (cases 21, 25, 26), the data does not say
  which record is whose. We only assign when the batch is unambiguous or the counts match exactly,
  and then we match by order. `custom_02` and `case_23` show that order matching can pick the
  wrong one of two equally likely records.
- **Rules-only mode depends on local OCR.** On a machine without `tesseract`/`pdftotext`/`pypdf`
  the rules-only mode cannot read any PDF or screenshot, so case_07 (evidence only in a PDF) fails
  there. That gap is exactly what the LLM mode closes, and it is why the final run uses the model.
- **Model vs rules on intent:** on all 398 distinct request texts, Gemini and the keyword rules
  agreed (0 disagreements), so the self-check call was never triggered on this dataset. The rules
  are tuned on this dataset; the model is the part expected to generalize to unseen phrasing.
