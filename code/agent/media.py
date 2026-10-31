"""Read PDFs and screenshots into structured records.

Primary path: Gemini vision reads the image (or the PDF's text) and returns the business
records it shows (resource id, owning system, identifiers visible, state words).
Fallback path: local text extraction (pypdf or `pdftotext` for PDFs, `tesseract` for
images) followed by line-level regex parsing.

Results are cached per file content hash in code/cache/media_records.json, so the
model is called once per file, ever.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess

from .evidence import EMAIL_RE, RESOURCE_RE, USER_RE, norm_phone, system_of
from .llm import GeminiClient, LLMUnavailable

SYSTEMS = ("account_service, search_service, analytics_platform, support_platform, export_service, "
           "integration_service, backup_service, security_service, identity_service, legal_service, "
           "marketing_platform, object_storage, email_delivery_service, cache_service")

PROMPT_VERSION = "v2"
PROMPT = """You are reading an internal business artifact from an online shop ({kind}: {name},
captured {captured_at}).

Step 1 - "transcript": transcribe ALL visible text, one table row or field per line,
exactly as written (keep ids like support_1034, account_5762, user_13 and email addresses
character-for-character).

Step 2 - "records": list every customer-data record that has an internal resource id
(prefixes: account_, search_, analytics_, support_, export_, data_flow_, backup_,
audit_record_, tombstone_, legal_hold_, marketing_, object_). For each record give:
  resource_id (copied exactly from the transcript), system (one of: {systems}),
  record_type (e.g. USER_PROFILE, SEARCH_INDEX_ENTRY, SUPPORT_MESSAGE, SUPPORT_ATTACHMENT,
  ANALYTICS_PROFILE, USER_EXPORT, MARKETING_PROFILE, BACKUP_ARCHIVE, SECURITY_AUDIT_LOG,
  LEGAL_HOLD_RECORD, ACCOUNT_TOMBSTONE, DATA_FLOW), customer_reference (user_NN if shown),
  email, phone, full_name, status (as displayed), enabled (true/false for connectors),
  restore_allowed (true/false for backups), retention_until, observed_at.
Use null for anything not shown. Only use ids that appear in your transcript.

Return ONLY JSON: {{"transcript": "<text>", "records": [ ... ]}}
{text_block}"""


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader  # optional dependency
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception:
        pass
    if shutil.which("pdftotext"):
        try:
            return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                                  text=True, timeout=60).stdout
        except Exception:
            pass
    return ""


def ocr_text(path: str) -> str:
    if shutil.which("tesseract"):
        try:
            return subprocess.run(["tesseract", path, "-"], capture_output=True, text=True,
                                  timeout=120).stdout
        except Exception:
            pass
    return ""


def regex_records(text: str) -> list[dict]:
    """Deterministic fallback: link identifiers to resource ids on the same line; if the
    whole artifact is about a single resource, attach the artifact-level identifiers."""
    lines = text.splitlines()
    found: dict[str, dict] = {}
    all_ids = set(m.group(0) for m in RESOURCE_RE.finditer(text))
    for line in lines:
        rids = [m.group(0) for m in RESOURCE_RE.finditer(line)]
        if not rids:
            continue
        emails = EMAIL_RE.findall(line)
        users = USER_RE.findall(line)
        for rid in rids:
            r = found.setdefault(rid, {"resource_id": rid, "system": system_of(rid)})
            if len(rids) == 1:
                if emails and not r.get("email"):
                    r["email"] = emails[0]
                if users and not r.get("customer_reference"):
                    r["customer_reference"] = users[0]
    if len(all_ids) == 1:
        rid = next(iter(all_ids))
        r = found[rid]
        emails = EMAIL_RE.findall(text)
        users = USER_RE.findall(text)
        phones = re.findall(r"\+\d[\d\s-]{7,}\d", text)
        if emails and not r.get("email"):
            r["email"] = emails[0]
        if users and not r.get("customer_reference"):
            r["customer_reference"] = users[0]
        if phones and not r.get("phone") and norm_phone(phones[0]):
            r["phone"] = phones[0]
    return list(found.values())


def _attachment_hint(rec: dict, ext: str) -> dict:
    """A support record that *is* an uploaded document (PDF) is a support attachment."""
    if ext == ".pdf" and rec.get("system") == "support_platform":
        rec["record_type"] = "SUPPORT_ATTACHMENT"
    return rec


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if v not in (None, "", "null") and not out.get(k):
            out[k] = v
    return out


class MediaExtractor:
    """LLM vision + local text, cross-checked.

    The model returns both a transcript and structured records.  A model record is kept
    only if its resource id really appears in the transcript or in locally extracted text
    (guards against invented ids), emails it reports must also appear there, and the
    deterministic line parser runs over the same text so records the model skipped are
    still found.  Without a key, only local text extraction is used (and model answers
    from earlier runs are ignored, so rules-only mode is genuinely model-free)."""

    def __init__(self, client: GeminiClient | None, cache_path: str, log=print):
        self.client = client
        self.cache_path = cache_path
        self.log = log
        self.cache = {}
        if os.path.exists(cache_path):
            try:
                self.cache = json.load(open(cache_path, encoding="utf-8"))
            except json.JSONDecodeError:
                self.cache = {}
        self.source_counts = {"llm": 0, "cache": 0, "fallback": 0, "unreadable": 0,
                              "llm_records_rejected": 0}

    @property
    def use_llm(self):
        return self.client is not None and self.client.available

    def extract(self, dataset_dir: str, manifest: dict) -> dict[str, list[dict]]:
        out = {}
        for fid, m in manifest.items():
            path = os.path.join(dataset_dir, m["file_path"])
            ext = os.path.splitext(path)[1].lower()
            if ext not in (".pdf", ".png", ".jpg", ".jpeg") or not os.path.exists(path):
                continue
            key = f"{_sha(path)}|{PROMPT_VERSION}"
            local = pdf_text(path) if ext == ".pdf" else ocr_text(path)
            llm_part = None
            if self.use_llm:
                llm_part = self.cache.get(key)
                if llm_part is not None:
                    self.source_counts["cache"] += 1
                else:
                    llm_part = self._ask_model(path, ext, m, local)
                    if llm_part is not None:
                        self.source_counts["llm"] += 1
                        self.cache[key] = llm_part
            records = self._combine(llm_part, local, ext)
            if llm_part is None:
                self.source_counts["fallback" if local.strip() else "unreadable"] += 1
            out[fid] = records
        if self.use_llm:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            json.dump(self.cache, open(self.cache_path, "w", encoding="utf-8"), indent=1,
                      ensure_ascii=False)
        return out

    def _ask_model(self, path, ext, m, local_text):
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                blob = f.read()
            if ext == ".pdf":
                prompt = PROMPT.format(kind="PDF document", name=name, captured_at=m["captured_at"],
                                       systems=SYSTEMS, text_block="")
                reply = self.client.generate_json(prompt, purpose="media_pdf", image_bytes=blob,
                                                  image_mime="application/pdf")
            else:
                prompt = PROMPT.format(kind="application screenshot", name=name,
                                       captured_at=m["captured_at"], systems=SYSTEMS, text_block="")
                mime = "image/png" if ext == ".png" else "image/jpeg"
                reply = self.client.generate_json(prompt, purpose="media_image", image_bytes=blob,
                                                  image_mime=mime)
        except (LLMUnavailable, ValueError, TypeError) as e:
            self.log(f"[media] LLM failed for {name}, using local fallback: {e}")
            return None
        if isinstance(reply, list):
            reply = {"transcript": "", "records": reply}
        if not isinstance(reply, dict):
            return None
        return {"file": m["file_path"], "transcript": str(reply.get("transcript") or ""),
                "records": [r for r in (reply.get("records") or []) if isinstance(r, dict)]}

    def _combine(self, llm_part, local_text, ext):
        transcript = (llm_part or {}).get("transcript", "")
        corpus = (transcript + "\n" + (local_text or "")).strip()
        corpus_l = corpus.lower()
        merged: dict[str, dict] = {}
        for r in regex_records(corpus) if corpus else []:
            merged[r["resource_id"]] = r
        for r in (llm_part or {}).get("records", []):
            rid = str(r.get("resource_id") or "").strip()
            if not RESOURCE_RE.fullmatch(rid) or rid.lower() not in corpus_l:
                self.source_counts["llm_records_rejected"] += 1
                continue
            clean = {k: v for k, v in r.items() if v not in (None, "", "null")}
            if clean.get("email") and str(clean["email"]).lower() not in corpus_l:
                clean.pop("email")
            clean["resource_id"] = rid
            clean["system"] = system_of(rid) or clean.get("system")
            merged[rid] = _merge(clean, merged.get(rid, {}))
        return [_attachment_hint(r, ext) for r in merged.values()]
