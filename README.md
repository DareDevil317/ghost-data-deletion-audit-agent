# Ghost Data Deletion-Audit Agent

An AI agent that checks whether a customer's personal data is **really** gone after a
deletion request, and if it isn't, writes the ordered plan to finish the job.

Built for **HackerRank Orchestrate (October 2026)**, challenge *"Ghost Data Still Exists"*.
**Final rank: #856 of 4,094.**

\---

## The problem

A deletion workflow can report *"Complete"* while copies of the person's data survive in
replica databases, search indexes, support tickets, analytics, data exports, backups and
active sync jobs, sometimes under an email the customer used *before* changing it.

For each of 518 deletion requests, the agent must decide:

* `COMPLETE`, `INCOMPLETE`, or `NOT\_APPLICABLE` (the person didn't actually ask for deletion);
* which records still expose the person, and which processes could recreate deleted data;
* the ordered **findings → remediation → verification** steps, following a deletion policy
that says what to delete, redact, anonymize or keep.

Full challenge text: [`problem\_statement.md`](./problem_statement.md).

## How it works

A hybrid design: the language model handles the parts that need judgment, and plain
Python handles everything that has one correct answer.

|Step|Done by|What happens|
|-|-|-|
|1. Intent|Gemini (+ keyword rules)|Is this message really a deletion request? Works in any language. If model and rules disagree, a second focused model call decides.|
|2. Media|Gemini vision (+ OCR fallback)|Reads screenshots and PDFs. The model must transcribe the image first; any record whose ID isn't in that transcript is rejected, which guards against invented IDs.|
|3. Evidence|Python|Parses SQL snapshots, JSON records, five log formats, text files and the activity stream into one common format.|
|4. Audit|Python|Resolves current **and historical** identifiers, links every record to the person, reconstructs each record's state at audit time (workflow "SUCCESS" messages are treated as claims, not proof), applies the policy table, and orders the steps.|

Why not let the model do everything? The output is graded exactly, and policy decisions
must be consistent and traceable to a rule. Code can't hallucinate an ID; during
testing the model did exactly that once, and the transcript check caught it.

## Results

On the 32 public sample cases (scored by the included evaluation workflow):

|Metric|Gemini mode|Rules-only (no model)|
|-|-:|-:|
|Status accuracy|**100%**|96.9%|
|Step F1|**98.2%**|96.8%|
|Exact step sequence|**81.2%**|78.1%|

The full run makes about 25 model calls for all 518 requests on Gemini's free tier.
Details, the failures found along the way and known limitations are in
[`code/evaluation/evaluation\_report.md`](./code/evaluation/evaluation_report.md).

## Run it

Requires Python 3.10+, with no other packages.

```bash
# optional: put a Google AI Studio key in a .env file in the repo root
echo GEMINI\_API\_KEY=your\_key > .env

python code/main.py            # writes output.csv
python code/main.py --no-llm   # fully offline (rules + local OCR only)
python code/evaluation/main.py # scores the agent on the samples + custom test cases
```

More detail: [`code/README.md`](./code/README.md).

## Repository layout

```
code/
  main.py              pipeline entry point
  agent/               evidence parsers, identity resolution, auditor, LLM client, intent, media
  evaluation/          scoring, custom test cases, evaluation and token-usage reports
dataset/               challenge data (provided by HackerRank)
output.csv             the agent's answers for all 518 requests
```

## How this was built

This was my first hackathon. I built it with the help of an AI coding assistant (Claude). I set up and ran the pipeline on my own machine with the live Gemini API.
Those runs exposed real problems: PDFs coming back empty, the model inventing a record ID,
and a date-handling bug. I then verified each fix with the evaluation workflow.

## Credits

Challenge, dataset and starter repository by [HackerRank](https://github.com/interviewstreet/hackerrank-orchestrate-october26).

