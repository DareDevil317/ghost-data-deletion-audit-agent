"""Does the request actually authorize deletion?

Primary path: an LLM reads each distinct request text (any language) and returns
DELETE or NOT_DELETE with a short reason.  Texts are de-duplicated and batched, and
answers are cached, so a full run costs a handful of calls.

Fallback path (no API key, or the call fails): multilingual keyword rules.  The rules
look for explicit non-deletion intents first ("not asking for deletion", "only",
"keep the account", copy/export/correction requests), then for deletion verbs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .llm import GeminiClient, LLMUnavailable

PROMPT = """You are a privacy-operations triage assistant for an online shop.
For each customer message below, decide whether the customer is asking for their
ACCOUNT / PERSONAL DATA TO BE DELETED (erased, closed permanently, removed).

Answer NOT_DELETE when the message asks for something else, even if it mentions the
account or personal data: access or a copy of data, data export/download, correcting
or changing details, login help, invoices, or unsubscribing from marketing only.
Answer NOT_DELETE when the customer explicitly says they do not want deletion.
Answer DELETE when the customer asks to delete/erase/close the account or remove their
personal data, including follow-ups asking you to finish or verify a deletion.
Messages may be in any language.

Return ONLY a JSON array, one object per message, in the same order:
[{{"id": "<id>", "intent": "DELETE" | "NOT_DELETE", "reason": "<max 12 words>"}}]

Messages:
{messages}
"""

# explicit statements that the customer does NOT want deletion (checked first)
VERIFY_PROMPT = """Two checks disagree about one customer message sent to an online shop's
privacy team. First check: {first}. Keyword check: {rule} ({why}).
Read the message carefully. Decide whether the customer authorizes deletion of their
account or personal data (DELETE) or asks for something else / explicitly refuses
deletion (NOT_DELETE). Follow-ups asking to finish or verify a deletion count as DELETE.
Message: {text}
Return ONLY JSON: {{"intent": "DELETE" | "NOT_DELETE", "reason": "<max 15 words>"}}
"""

NEGATIVE_PATTERNS = [
    r"not asking for deletion", r"not a request to (erase|delete)", r"do not (close|delete)",
    r"don'?t (close|delete)", r"without removing", r"leave (the|my) (account|profile)",
    r"keep the account", r"continue using", r"still (want|need|use) (it|the|this|my)",
    r"lists only", r"\bonly\b.*\bmarketing\b|\bmarketing\b.*\bonly\b",
    r"no (quiero|deseo) (eliminar|borrar)", r"ne (pas|veux pas) supprimer",
]
# deletion wording in the languages seen in the data (and a few more)
POSITIVE_PATTERNS = [
    r"delet", r"eras", r"remov", r"\bclos(e|ed|ing)\b", r"closure", r"\bgone\b",
    r"still exist", r"suppression", r"supprim", r"elimin", r"exclus", r"exclui", r"borrar",
    r"lösch", r"cancellazion", r"verwijder", r"削除", r"삭제", r"删除", r"注销",
]


def rule_intent(text: str) -> tuple[str, str]:
    t = text.lower()
    for p in NEGATIVE_PATTERNS:
        if re.search(p, t):
            return "NOT_DELETE", f"rule: explicit non-deletion '{p}'"
    for p in POSITIVE_PATTERNS:
        if re.search(p, t):
            return "DELETE", f"rule: deletion wording '{p}'"
    return "NOT_DELETE", "rule: no deletion wording"


class IntentClassifier:
    def __init__(self, client: GeminiClient | None, cache_path: str, batch_size: int = 40,
                 log=print):
        self.client = client
        self.cache_path = cache_path
        self.batch_size = batch_size
        self.log = log
        self.cache = {}
        if os.path.exists(cache_path):
            try:
                self.cache = json.load(open(cache_path, encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}
        self.source_counts = {"llm": 0, "cache": 0, "rules": 0, "llm_rule_disagreements": 0}

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]

    def classify_all(self, texts: list[str]) -> dict[str, dict]:
        """Return {text: {"intent", "reason", "source"}} for every text."""
        unique = list(dict.fromkeys(t.strip() for t in texts))
        todo = [t for t in unique if self._key(t) not in self.cache]
        fresh = set()
        if todo and self.client is not None and self.client.available:
            for i in range(0, len(todo), self.batch_size):
                batch = todo[i:i + self.batch_size]
                msgs = "\n".join(json.dumps({"id": self._key(t), "text": t}, ensure_ascii=False)
                                 for t in batch)
                try:
                    reply = self.client.generate_json(PROMPT.format(messages=msgs), purpose="intent")
                    got = {r.get("id"): r for r in reply if isinstance(r, dict)}
                    for t in batch:
                        r = got.get(self._key(t))
                        if r and r.get("intent") in ("DELETE", "NOT_DELETE"):
                            self.cache[self._key(t)] = {"intent": r["intent"],
                                                        "reason": str(r.get("reason", ""))[:120],
                                                        "source": "llm"}
                            fresh.add(self._key(t))
                except (LLMUnavailable, ValueError, TypeError) as e:
                    self.log(f"[intent] LLM batch failed, using rules for it: {e}")
            self._verify_disagreements([t for t in todo if self._key(t) in fresh])
            self._save()
        out = {}
        for t in unique:
            k = self._key(t)
            llm = self.cache.get(k) if (self.client is not None and self.client.available) else None
            rule, why = rule_intent(t)
            if llm:
                self.source_counts["llm" if k in fresh else "cache"] += 1
                if llm["intent"] != rule:
                    self.source_counts["llm_rule_disagreements"] += 1
                res = dict(llm)
                res["rule_intent"] = rule
            else:
                self.source_counts["rules"] += 1
                res = {"intent": rule, "reason": why, "source": "rules", "rule_intent": rule}
            out[t] = res
        return out

    def _verify_disagreements(self, texts):
        """Self-check: where the batch answer and the keyword rules disagree, ask the model
        again about that single message with a step-by-step instruction; that answer wins."""
        for t in texts:
            k = self._key(t)
            first = self.cache[k]["intent"]
            rule, why = rule_intent(t)
            if first == rule:
                continue
            prompt = VERIFY_PROMPT.format(text=json.dumps(t, ensure_ascii=False), first=first,
                                          rule=rule, why=why)
            try:
                r = self.client.generate_json(prompt, purpose="intent_verify")
                if isinstance(r, list) and r:
                    r = r[0]
                if isinstance(r, dict) and r.get("intent") in ("DELETE", "NOT_DELETE"):
                    self.cache[k] = {"intent": r["intent"], "reason": str(r.get("reason", ""))[:120],
                                     "source": "llm", "verified": True, "first_pass": first}
            except (LLMUnavailable, ValueError, TypeError) as e:
                self.log(f"[intent] verification failed, keeping first answer: {e}")

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
