"""Evaluation workflow.

    python3 code/evaluation/main.py            # rules-only and (if a key is set) LLM mode
    python3 code/evaluation/main.py --no-llm   # rules-only

1. Runs the full agent on the 32 public sample requests and compares with
   dataset/sample_output.csv.
2. Runs the agent on our own extra cases (code/evaluation/custom_cases/) and compares with
   the expected answers we wrote for them.
3. Reports case-level status accuracy, step-level precision/recall/F1, field-level accuracy,
   resource recall and exact-sequence accuracy, per mode, plus every failing case.
4. Writes the numbers into evaluation_report.md between the METRICS markers.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
REPO = os.path.dirname(CODE)
sys.path.insert(0, CODE)

from main import run                                   # noqa: E402
from evaluation.scoring import load_rows, score        # noqa: E402

START, END = "<!-- METRICS:START -->", "<!-- METRICS:END -->"


def evaluate(dataset, requests, expected, extra, use_llm, tag):
    out = os.path.join(CODE, "outputs", f"eval_{tag}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run(dataset, requests, out, use_llm=use_llm, extra_request_paths=extra, quiet=True)
    return score(load_rows(out), load_rows(expected))


def competitors_without_bases(ds, custom_req):
    import csv
    bases = {r.get("based_on") for r in csv.DictReader(open(custom_req, encoding="utf-8"))}
    rows = [r for r in csv.DictReader(open(os.path.join(ds, "sample_deletion_requests.csv"), encoding="utf-8"))
            if r["case_id"] not in bases]
    path = os.path.join(CODE, "outputs", "custom_competitors.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def fmt(m):
    return (f"| {m['cases']} | {m['status_accuracy']:.1%} | {m['step_precision']:.1%} | "
            f"{m['step_recall']:.1%} | {m['step_f1']:.1%} | {m['resource_recall']:.1%} | "
            f"{m['state_accuracy_on_aligned']:.1%} | {m['evidence_accuracy_on_aligned']:.1%} | "
            f"{m['sequence_accuracy']:.1%} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    ds = args.dataset
    sample_req = os.path.join(ds, "sample_deletion_requests.csv")
    sample_exp = os.path.join(ds, "sample_output.csv")
    eval_req = os.path.join(ds, "deletion_requests.csv")
    custom_req = os.path.join(HERE, "custom_cases", "custom_requests.csv")
    custom_exp = os.path.join(HERE, "custom_cases", "custom_expected.csv")

    from agent.llm import GeminiClient, load_dotenv
    load_dotenv(os.path.join(REPO, ".env"))
    modes = [("rules-only", False)]
    if not args.no_llm and GeminiClient().available:
        modes.append(("llm", True))

    table = ["| Mode | Suite | Cases | Status acc. | Step precision | Step recall | Step F1 | "
             "Resource recall | State acc. (aligned) | Evidence acc. (aligned) | Exact sequence |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    failures = []
    for name, use_llm in modes:
        ms = evaluate(ds, sample_req, sample_exp, [eval_req], use_llm, f"samples_{name}")
        # each custom case stands in for the sample case it was built from, so every other
        # sample request still competes for shared workflow activity
        mc = evaluate(ds, custom_req, custom_exp, [eval_req, competitors_without_bases(ds, custom_req)],
                      use_llm, f"custom_{name}")
        for suite, m in (("public samples", ms), ("custom cases", mc)):
            table.append(f"| {name} | {suite} " + fmt(m))
            print(f"{name:10s} {suite:15s} status={m['status_accuracy']:.3f} "
                  f"stepF1={m['step_f1']:.3f} seq={m['sequence_accuracy']:.3f}")
            for c in m["per_case"]:
                if not c["exact"]:
                    failures.append(f"| {name} | {c['case_id']} | {c['gold_status']} | {c['pred_status']} | "
                                    f"{c['matched_steps']}/{c['gold_steps']} | "
                                    f"{', '.join(c['missing_resources']) or '-'} | "
                                    f"{', '.join(c['extra_resources']) or '-'} |")
    block = ["", "### Latest automated results", "", *table, "",
             "Failing (non-exact) cases:", "",
             "| Mode | Case | Expected status | Predicted status | Steps matched | Missing resources | Extra resources |",
             "|---|---|---|---|---|---|---|", *(failures or ["| - | none | | | | | |"]), ""]
    report = os.path.join(HERE, "evaluation_report.md")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else f"{START}\n{END}\n"
    if START in text and END in text:
        a, b = text.index(START) + len(START), text.index(END)
        text = text[:a] + "\n".join(block) + "\n" + text[b:]
        open(report, "w", encoding="utf-8").write(text)
        print(f"updated {report}")


if __name__ == "__main__":
    main()
