"""Write evaluation/usage_report.md from the real token log of a run."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .llm import PRICES


def write_usage_report(path, usage_log, n_requests, llm_enabled, intent_sources, media_sources,
                       runtime_s):
    per_model = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "purposes": defaultdict(int)})
    for u in usage_log:
        m = per_model[(u["provider"], u["model"])]
        m["calls"] += 1
        m["in"] += u["input_tokens"]
        m["out"] += u["output_tokens"]
        m["purposes"][u["purpose"]] += 1
    lines = [
        "# Token Usage and Cost Report",
        "",
        f"Generated automatically by `code/main.py` at the end of the run "
        f"({dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}).",
        "",
        "## Run summary",
        "",
        f"- Requests audited: **{n_requests}**",
        f"- LLM enabled for this run: **{'yes' if llm_enabled else 'no (deterministic fallbacks only)'}**",
        f"- Intent decisions by source: {dict(intent_sources)}",
        f"- Media (PDF/image) extractions by source: {dict(media_sources)}",
        f"- Wall-clock runtime: {runtime_s:.1f} s",
        "",
        "Cached answers (`cache`) come from earlier runs and cost no tokens in this run.",
        "",
        "## Per-model usage",
        "",
        "| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (paid list price) |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    tot_calls = tot_in = tot_out = 0
    tot_cost = 0.0
    for (prov, model), m in sorted(per_model.items()):
        pin, pout = PRICES.get(model, (0.0, 0.0))
        cost = m["in"] / 1e6 * pin + m["out"] / 1e6 * pout
        lines.append(f"| {prov} | `{model}` | {m['calls']} | {m['in']:,} | {m['out']:,} | "
                     f"{m['in'] + m['out']:,} | ${cost:.4f} |")
        tot_calls += m["calls"]
        tot_in += m["in"]
        tot_out += m["out"]
        tot_cost += cost
    lines.append(f"| **Overall** | | **{tot_calls}** | **{tot_in:,}** | **{tot_out:,}** | "
                 f"**{tot_in + tot_out:,}** | **${tot_cost:.4f}** |")
    n = max(n_requests, 1)
    lines += [
        "",
        "## Per-request averages",
        "",
        f"- Model calls per request: {tot_calls / n:.3f}",
        f"- Average tokens per request: {(tot_in + tot_out) / n:,.1f} "
        f"(input {tot_in / n:,.1f}, output {tot_out / n:,.1f})",
        f"- Estimated cost per request at paid list prices: ${tot_cost / n:.6f}",
        "- Actual cost of this run: **$0.00** (Gemini API free tier).",
        "",
        "## Calls by purpose",
        "",
    ]
    for (prov, model), m in sorted(per_model.items()):
        for purpose, c in sorted(m["purposes"].items()):
            lines.append(f"- `{model}` / {purpose}: {c} calls")
    if not per_model:
        lines.append("- none (no model calls were made in this run)")
    lines += [
        "",
        "## Why so few tokens",
        "",
        "- Only the two fuzzy sub-problems use a model: request intent (multilingual free text) and "
        "reading PDFs/screenshots. Identity resolution, timeline reconstruction, policy and step "
        "ordering are deterministic code, so they cost nothing and cannot hallucinate.",
        "- Request texts are de-duplicated and classified in batches of 40.",
        "- Every model answer is cached by content hash, so re-runs only pay for new inputs.",
        "",
        f"Price assumptions (USD per 1M input/output tokens): "
        + ", ".join(f"`{k}` {v[0]}/{v[1]}" for k, v in PRICES.items()) + ".",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
