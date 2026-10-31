"""Ghost Data Still Exists — deletion-audit agent.

Usage (from the repository root):
    python3 code/main.py                     # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --no-llm            # fully offline: rules + OCR fallbacks only
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --output sample_pred.csv

Pipeline
    1. intent    : does each request authorize deletion?  (LLM, multilingual; rules fallback)
    2. media     : read PDFs / screenshots into records    (LLM vision; OCR fallback; cached)
    3. evidence  : parse SQL, JSON, logs, text, activity into normalized observations
    4. audit     : identity -> linked resources -> state at audit_at -> policy -> ordered steps
    5. output    : output.csv (+ a per-case trace and the token-usage report)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from agent.auditor import Auditor                     # noqa: E402
from agent.evidence import load_all_observations, load_manifest  # noqa: E402
from agent.intent import IntentClassifier             # noqa: E402
from agent.llm import GeminiClient, load_dotenv       # noqa: E402
from agent.media import MediaExtractor                # noqa: E402
from agent.report import write_usage_report           # noqa: E402


def case_sort_key(cid: str):
    tail = cid.split("_")[-1]
    return (0, int(tail)) if tail.isdigit() else (1, cid)


def read_requests(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def run(dataset_dir, requests_path, output_path, use_llm=True, extra_request_paths=(),
        usage_report_path=None, trace_path=None, quiet=False):
    log = (lambda *a: None) if quiet else print
    t0 = time.time()
    load_dotenv(os.path.join(REPO, ".env"))
    load_dotenv(os.path.join(HERE, ".env"))
    usage_log: list = []
    client = GeminiClient(usage_log=usage_log) if use_llm else None
    if use_llm and not client.available:
        log("[main] GEMINI_API_KEY not set -> running with deterministic fallbacks only")
    cache_dir = os.path.join(HERE, "cache")

    requests = read_requests(requests_path)
    # other known requests (e.g. the samples) compete for shared workflow activity
    others = []
    for p in extra_request_paths:
        if os.path.exists(p) and os.path.abspath(p) != os.path.abspath(requests_path):
            others += read_requests(p)

    # 1) intent ------------------------------------------------------------------
    intents = IntentClassifier(client, os.path.join(cache_dir, "intent_cache.json"), log=log)
    labels = intents.classify_all([r["request_text"] for r in requests + others])
    authorized = {r["case_id"]: labels[r["request_text"].strip()]["intent"] == "DELETE"
                  for r in requests + others}
    log(f"[intent] {sum(authorized[r['case_id']] for r in requests)}/{len(requests)} requests "
        f"authorize deletion  (sources: {intents.source_counts})")

    # 2) media -------------------------------------------------------------------
    manifest = load_manifest(dataset_dir)
    media = MediaExtractor(client, os.path.join(cache_dir, "media_records.json"), log=log)
    media_records = media.extract(dataset_dir, manifest)
    log(f"[media] {sum(len(v) for v in media_records.values())} records from "
        f"{len(media_records)} PDFs/images  (sources: {media.source_counts})")

    # 3) evidence ----------------------------------------------------------------
    observations, manifest = load_all_observations(dataset_dir, media_records)
    log(f"[evidence] {len(observations)} observations")

    # 4) audit -------------------------------------------------------------------
    competing = [r for r in requests + others if authorized[r["case_id"]]]
    auditor = Auditor(dataset_dir, observations, manifest, competing)
    rows, traces = [], []
    for req in sorted(requests, key=lambda r: case_sort_key(r["case_id"])):
        res = auditor.audit(req, authorized[req["case_id"]])
        rows.append({"case_id": res.case_id, "deletion_status": res.status,
                     "steps_json": json.dumps(res.steps, ensure_ascii=False, separators=(",", ":"))})
        lab = labels[req["request_text"].strip()]
        traces.append({"case_id": res.case_id, "user_id": res.user_id, "status": res.status,
                       "intent": lab["intent"], "intent_source": lab["source"],
                       "intent_reason": lab.get("reason", ""), "resources": res.notes})

    # 5) output ------------------------------------------------------------------
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if trace_path:
        os.makedirs(os.path.dirname(trace_path), exist_ok=True)
        with open(trace_path, "w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    if usage_report_path:
        write_usage_report(usage_report_path, usage_log, n_requests=len(requests),
                           llm_enabled=bool(client and client.available),
                           intent_sources=intents.source_counts, media_sources=media.source_counts,
                           runtime_s=time.time() - t0)
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    log(f"[main] wrote {len(rows)} rows to {output_path}  {counts}  ({time.time() - t0:.1f}s)")
    return rows, usage_log


def main():
    ap = argparse.ArgumentParser(description="Deletion-audit agent")
    ap.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    ap.add_argument("--requests", default=None, help="requests CSV (default dataset/deletion_requests.csv)")
    ap.add_argument("--output", default=os.path.join(REPO, "output.csv"))
    ap.add_argument("--no-llm", action="store_true", help="skip all model calls (rules + OCR only)")
    ap.add_argument("--usage-report", default=os.path.join(HERE, "evaluation", "usage_report.md"))
    args = ap.parse_args()
    requests_path = args.requests or os.path.join(args.dataset, "deletion_requests.csv")
    run(args.dataset, requests_path, args.output, use_llm=not args.no_llm,
        extra_request_paths=[os.path.join(args.dataset, "deletion_requests.csv"),
                             os.path.join(args.dataset, "sample_deletion_requests.csv")],
        usage_report_path=args.usage_report,
        trace_path=os.path.join(HERE, "outputs", "trace.jsonl"))


if __name__ == "__main__":
    main()
