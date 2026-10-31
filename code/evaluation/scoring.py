"""Scoring helpers: compare predicted rows with expected rows.

Metrics
- status accuracy: deletion_status exact match per case
- step precision / recall / F1: a step matches when every field except step_no matches
- resource recall: did we find each (system_id, resource_id) the answer mentions?
- field accuracy: for steps aligned on (phase, operation, system_id, resource_id),
  how often state and evidence_id are right
- sequence accuracy: the whole steps_json array is exactly right (order included)
"""
from __future__ import annotations

import json

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _key(s):
    return tuple(s.get(f, "") for f in FIELDS)


def _align_key(s):
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def score(pred: dict, gold: dict) -> dict:
    """pred/gold: {case_id: (status, steps_list)} for the cases to score."""
    n = len(gold)
    status_ok = tp = n_pred = n_gold = seq_ok = 0
    res_found = res_total = 0
    aligned = state_ok = ev_ok = 0
    per_case = []
    for cid, (g_status, g_steps) in sorted(gold.items()):
        p_status, p_steps = pred.get(cid, ("MISSING", []))
        status_ok += p_status == g_status
        gk = [_key(s) for s in g_steps]
        pk = [_key(s) for s in p_steps]
        match = len(set(gk) & set(pk))
        tp += match
        n_pred += len(pk)
        n_gold += len(gk)
        exact = [s for s in gk] == [s for s in pk]
        seq_ok += exact
        g_res = {(s["system_id"], s["resource_id"]) for s in g_steps}
        p_res = {(s["system_id"], s["resource_id"]) for s in p_steps}
        res_found += len(g_res & p_res)
        res_total += len(g_res)
        pa = {_align_key(s): s for s in p_steps}
        for s in g_steps:
            q = pa.get(_align_key(s))
            if q:
                aligned += 1
                state_ok += q["state"] == s["state"]
                ev_ok += q["evidence_id"] == s["evidence_id"]
        per_case.append({"case_id": cid, "gold_status": g_status, "pred_status": p_status,
                         "exact": exact, "matched_steps": match, "gold_steps": len(gk),
                         "pred_steps": len(pk),
                         "missing_resources": sorted(r for _, r in g_res - p_res),
                         "extra_resources": sorted(r for _, r in p_res - g_res)})
    prec = tp / n_pred if n_pred else 0.0
    rec = tp / n_gold if n_gold else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n if n else 0.0,
        "step_precision": prec, "step_recall": rec, "step_f1": f1,
        "resource_recall": res_found / res_total if res_total else 0.0,
        "aligned_steps": aligned,
        "state_accuracy_on_aligned": state_ok / aligned if aligned else 0.0,
        "evidence_accuracy_on_aligned": ev_ok / aligned if aligned else 0.0,
        "sequence_accuracy": seq_ok / n if n else 0.0,
        "per_case": per_case,
    }


def load_rows(path):
    import csv
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        out[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"]))
    return out
