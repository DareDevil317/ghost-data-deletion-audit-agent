"""Evidence parsing: turn every supplied evidence source into normalized observations.

An Observation says: "source S, at time T, saw resource R in system X, and the record
did / did not exist, and contained these identifiers / flags".

All formats (SQL snapshots, JSON records, five log styles, text handover, activity
CSV, and media text extracted from PDFs/images) are reduced to this one shape so
the auditor never has to care where a fact came from.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
RESOURCE_RE = re.compile(
    r"\b(account|search|analytics|support|export|data_flow|backup|audit_record|"
    r"tombstone|legal_hold|marketing|object|email_delivery|cache|telemetry)_\d+\b"
)

# resource-id prefix -> owning system.  Longest prefixes first.
PREFIX_SYSTEM = [
    ("audit_record_", "security_service"),
    ("data_flow_", "integration_service"),
    ("legal_hold_", "legal_service"),
    ("email_delivery_", "email_delivery_service"),
    ("tombstone_", "identity_service"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("analytics_", "analytics_platform"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("marketing_", "marketing_platform"),
    ("object_", "object_storage"),
    ("cache_", "cache_service"),
    ("telemetry_", "telemetry_service"),
]

DIRECT_ID_FIELDS = {
    "email", "phone", "email_hash", "full_name", "display_name", "name",
    "requester_email", "requester_name", "requester_phone", "owner_email",
    "owner_phone", "customer_email", "matched_value",
}
REFERENCE_FIELDS = {"customer_reference", "user_id", "subject_reference", "subject_ref"}
NULLS = {None, "", "null", "None", "none", "NULL"}


def system_of(resource_id: str) -> str | None:
    for prefix, system in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return None


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str | None:
    digits = re.sub(r"\D", "", v or "")
    return digits if len(digits) >= 7 else None


def looks_like_phone(v: str) -> bool:
    return bool(re.fullmatch(r"\+?[\d\s().-]{7,}", v.strip())) and len(re.sub(r"\D", "", v)) >= 7


def extract_identifiers(value, out: set) -> None:
    """Collect normalized identifier tokens ("e:", "p:", "h:", "u:") from any value."""
    if value is None:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and k in ("phone", "requester_phone", "owner_phone") and v not in NULLS:
                p = norm_phone(v)
                if p:
                    out.add("p:" + p)
            extract_identifiers(v, out)
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            extract_identifiers(v, out)
        return
    s = str(value)
    for m in EMAIL_RE.findall(s):
        out.add("e:" + norm_email(m))
    for m in HASH_RE.findall(s):
        out.add("h:" + m.lower())
    for m in USER_RE.findall(s):
        out.add("u:" + m)
    if looks_like_phone(s):
        p = norm_phone(s)
        if p:
            out.add("p:" + p)


def _truthy(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "enabled", "allowed"):
        return True
    if s in ("false", "0", "no", "disabled", "blocked", "denied"):
        return False
    return None


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------
@dataclass
class Observation:
    source_id: str            # evidence_xx / activity_xx / audit_xx
    source_time: str          # when the source became available (file captured_at / event time)
    observed_at: str          # when the fact was observed
    resource_id: str
    system: str
    exists: bool | None = None          # True present, False absent, None unknown
    record_type: str | None = None      # category hint (USER_PROFILE, SUPPORT_MESSAGE, ...)
    status: str | None = None           # raw status words (IDENTIFIABLE, ANONYMIZED, ACTIVE ...)
    links: set = field(default_factory=set)      # identifiers linking this record to people
    pii: set = field(default_factory=set)        # direct identifiers present in the record body
    has_reference: bool = False                  # stable user reference present
    enabled: bool | None = None                  # data-flow enabled?
    restore_allowed: bool | None = None          # backup restorable?
    retention_until: str | None = None
    attrs: dict = field(default_factory=dict)    # anything else worth keeping
    kind: str = "evidence"                       # evidence | activity


def _record_to_obs(rec: dict, source_id: str, source_time: str, observed_at: str,
                   exists: bool | None = True, extra_links: set | None = None) -> Observation | None:
    rid = rec.get("resource_id")
    if not rid or not isinstance(rid, str):
        return None
    system = rec.get("system") or rec.get("origin_system") or system_of(rid)
    if system_of(rid):
        system = system_of(rid)
    obs = Observation(source_id, source_time, observed_at or source_time, rid, system, exists)
    obs.record_type = rec.get("record_type")
    links: set = set()
    extract_identifiers({k: v for k, v in rec.items() if k not in ("resource_id",)}, links)
    if extra_links:
        links |= extra_links
    obs.links = links
    pii: set = set()
    for k, v in rec.items():
        if k in DIRECT_ID_FIELDS and v not in NULLS:
            pii.add(k)
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 in DIRECT_ID_FIELDS and v2 not in NULLS:
                    pii.add(k2)
    for txt_field in ("message_text", "message_excerpt", "text"):
        if isinstance(rec.get(txt_field), str) and (EMAIL_RE.search(rec[txt_field])):
            pii.add(txt_field)
    obs.pii = pii
    obs.has_reference = any(rec.get(k) not in NULLS for k in REFERENCE_FIELDS)
    for k in ("enabled",):
        if k in rec:
            obs.enabled = _truthy(rec[k])
    for k in ("restore_allowed", "eligible"):
        if k in rec and obs.restore_allowed is None:
            obs.restore_allowed = _truthy(rec[k])
    if rec.get("retention_until") not in NULLS:
        obs.retention_until = rec.get("retention_until")
    # explicit result words
    res = str(rec.get("result") or "").upper()
    if res:
        obs.status = res
        if res in ("NOT_FOUND", "ABSENT", "MISS", "DELETED"):
            obs.exists = False
        elif res in ("FOUND", "HIT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE",
                     "BLOCKED", "DISABLED", "PRESENT"):
            obs.exists = True
        if res == "ACTIVE" and system in ("integration_service",):
            obs.enabled = True
        if res == "DISABLED":
            obs.enabled = False
        if res == "ACTIVE" and system == "backup_service":
            obs.restore_allowed = True
        if res == "BLOCKED":
            obs.restore_allowed = False
    hs = str(rec.get("http_status") or rec.get("status_code") or "")
    if hs == "404":
        obs.exists = False
    elif hs == "200":
        obs.exists = True
    if str(rec.get("rows", "")) == "0":
        obs.exists = False
    status = str(rec.get("account_status") or "").upper()
    if status:
        obs.attrs["account_status"] = status
        if status in ("DELETED", "ERASED") and system == "account_service":
            obs.exists = False
    for k in ("available", "download_available", "archive_present"):
        if k in rec and _truthy(rec[k]) is False:
            obs.exists = False
    for k in ("connector_name", "event_code", "collection", "index_name", "file", "file_name",
              "detector", "direct_identifiers", "marker_found", "decision", "deletion_block",
              "scheduler_registered", "access", "reason", "email_opt_in", "audience_cohort"):
        if k in rec and rec[k] not in NULLS:
            obs.attrs[k] = rec[k]
    if "deletion_block" in rec and _truthy(rec["deletion_block"]) is True:
        obs.restore_allowed = False
    if "decision" in rec:
        d = str(rec["decision"]).lower()
        if "deny" in d or "block" in d or "reject" in d:
            obs.restore_allowed = False
        elif "allow" in d or "approve" in d or "permit" in d:
            obs.restore_allowed = True
    return obs


# ---------------------------------------------------------------------------
# Format-specific readers
# ---------------------------------------------------------------------------
def _walk_records(node, ctx_time=None, ctx=None):
    """Yield (record_dict, observed_at, context) for every dict that has a resource_id."""
    ctx = dict(ctx or {})
    if isinstance(node, dict):
        t = node.get("captured_at") or node.get("observed_at") or node.get("recorded_at") or ctx_time
        for key in ("data_store", "collection", "source"):
            if key in node and isinstance(node[key], str):
                ctx[key] = node[key]
        if "resource_id" in node and isinstance(node["resource_id"], str):
            yield node, t, ctx
            # nested dicts inside a record (profile, hits) are handled below too
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _walk_records(v, t, ctx)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_records(v, ctx_time, ctx)


def read_json(path, source_id, captured_at):
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for rec, t, ctx in _walk_records(data, captured_at):
        flat = dict(rec)
        # profile lookups wrap fields in "profile"; search hits wrap in hits.documents
        if isinstance(rec.get("profile"), dict):
            flat.update({k: v for k, v in rec["profile"].items()})
        if rec.get("profile", "x") is None and "status_code" in rec:
            flat["exists_hint"] = False
        o = _record_to_obs(flat, source_id, captured_at, t or captured_at)
        if o:
            if "hits" in rec and isinstance(rec["hits"], dict):
                o.exists = (rec["hits"].get("total", 0) or 0) > 0
                # identifiers come from the hit documents
                o.links = set()
                extract_identifiers(rec["hits"].get("documents"), o.links)
                extract_identifiers(rec.get("query"), o.links)
                o.pii = set()
                for d in rec["hits"].get("documents", []) or []:
                    for k, v in d.items():
                        if k in DIRECT_ID_FIELDS and v not in NULLS:
                            o.pii.add(k)
            if "store" in ctx or "collection" in ctx:
                o.attrs.setdefault("collection", ctx.get("collection"))
            out.append(o)
    # nested search documents inside "hits" are also yielded by the walker as their own
    # records with the same resource_id; drop exact duplicates later by the auditor.
    return out


def read_sql(path, source_id, captured_at):
    con = sqlite3.connect(":memory:")
    con.executescript(open(path, encoding="utf-8").read())
    out, coverage = [], []
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tables:
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        if "resource_id" not in cols:
            continue
        rows = con.execute(f'select * from "{t}"').fetchall()
        systems_in_table = set()
        for row in rows:
            rec = dict(zip(cols, row))
            if "record_json" in rec and rec["record_json"]:
                inner = json.loads(rec["record_json"])
                inner.setdefault("resource_id", rec["resource_id"])
                inner["origin_system"] = rec.get("origin_system")
                inner["collection"] = rec.get("collection_name")
                rec = inner
                t_obs = row[cols.index("updated_at")] if "updated_at" in cols else captured_at
            else:
                t_obs = rec.get("updated_at") or rec.get("indexed_at") or captured_at
            rec["table"] = t
            o = _record_to_obs(rec, source_id, captured_at, captured_at)
            if o:
                o.attrs["table"] = t
                o.attrs["updated_at"] = t_obs
                out.append(o)
                systems_in_table.add(o.system)
        coverage.append((t, systems_in_table))
    # point-in-time inventories declare which systems they captured completely
    if "snapshot_scope" in tables:
        for system, scope_time in con.execute("select origin_system, captured_at from snapshot_scope"):
            o = Observation(source_id, captured_at, scope_time, "__scope__", system, None)
            o.attrs["scope"] = True
            out.append(o)
    return out


KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def _parse_log_line(line: str):
    line = line.rstrip("\n")
    m = re.match(r"(\d{4}-\d\d-\d\dT[\d:]+Z)\s+(.*)", line)
    if not m:
        return None
    ts, rest = m.groups()
    d = {"ts": ts}
    if rest.startswith("{"):
        try:
            d.update(json.loads(rest))
        except json.JSONDecodeError:
            pass
        return d
    b = re.match(r"\[(\w+)\] \[(\w+)\] \[([\w-]+)\] (\w+):", rest)
    if b:
        d.update(level=b.group(1), service=b.group(2), trace_id=b.group(3), event=b.group(4))
    p = re.match(r"\| (\w+) \| (\w+) \| ([\w-]+) \| (\w+) \|", rest)
    if p:
        d.update(level=p.group(1), service=p.group(2), trace_id=p.group(3), event=p.group(4))
    s = re.match(r"<\d+>1 (\S+) (\w+) (\S+) - - ", rest)
    if s:
        d.update(service=s.group(2), trace_id=s.group(3))
    for k, v in KV_RE.findall(rest):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"')
        d[k] = v
    if "message" in d:
        for k, v in KV_RE.findall(d["message"]):
            if v.startswith('"'):
                v = v[1:-1]
            d[k] = v
    if "function" in d:
        d["service"] = d["function"]
    if "requestId" in d and "trace_id" not in d:
        d["trace_id"] = d["requestId"]
    return d


SUBJECT_KEYS = ("lookup_key", "subject_key", "scan_key", "match_key", "owner_key",
                "subject_ref", "correlation_key", "subject_reference")


def read_log(path, source_id, captured_at):
    """Group log lines by trace, carry the subject key from *_started onto results."""
    out = []
    trace_subject: dict = {}
    for line in open(path, encoding="utf-8"):
        d = _parse_log_line(line)
        if not d:
            continue
        tid = d.get("trace_id")
        subj = set()
        for k in SUBJECT_KEYS:
            if d.get(k) not in NULLS:
                extract_identifiers(d[k], subj)
        if tid:
            trace_subject.setdefault(tid, set()).update(subj)
        rid = d.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(rid) and not system_of(rid):
            continue
        ev = d.get("event", "")
        if ev.endswith("_started"):
            continue  # the completed line carries the result
        rec = {k: v for k, v in d.items() if k not in ("ts", "level", "severity", "logger",
                                                         "instance", "pod", "region", "trace_id",
                                                         "requestId", "function", "message",
                                                         "service", "attempt")}
        links = set(trace_subject.get(tid, set())) if tid else set()
        o = _record_to_obs(rec, source_id, captured_at, d["ts"], exists=True, extra_links=links)
        if not o:
            continue
        o.attrs["event"] = ev
        o.attrs["log_service"] = d.get("service")
        res = str(d.get("result", "")).lower()
        if res in ("miss", "none", "no_hit", "not_found"):
            o.exists = False
        if ev in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
            o.exists = True
            o.pii.add("matched_value")
            if ev == "attachment_pii_scan":
                o.record_type = "SUPPORT_ATTACHMENT"
            elif ev == "message_pii_scan":
                o.record_type = "SUPPORT_MESSAGE"
        if ev == "analytics_profile_read" and str(d.get("direct_identifiers")) == "0":
            o.pii.discard("direct_identifiers")
        if "direct_identifiers" in d:
            if str(d["direct_identifiers"]) not in ("0", "null"):
                o.pii.add("direct_identifiers")
        o.pii.discard("direct_identifiers") if str(d.get("direct_identifiers")) == "0" else None
        if ev == "deletion_marker_check" and _truthy(d.get("marker_found")) is False:
            o.exists = True
        if ev == "contact_lookup_completed":
            o.record_type = o.record_type or "SECONDARY_IDENTIFIER"
        if ev in ("connector_heartbeat", "connector_config_loaded") and "enabled" in d:
            o.enabled = _truthy(d["enabled"])
        out.append(o)
    return out


def read_handover_txt(path, source_id, captured_at):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip().lstrip("-").strip()
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not system_of(parts[2]):
            continue
        ts, system, rid, rtype, result = parts[:5]
        subject = parts[5] if len(parts) > 5 else ""
        rec = {"system": system, "resource_id": rid, "record_type": rtype, "result": result,
               "subject_reference": subject}
        o = _record_to_obs(rec, source_id, captured_at, ts)
        if o:
            o.pii = set()
            if result in ("IDENTIFIABLE", "FOUND") and EMAIL_RE.search(subject):
                o.pii.add("subject")
            out.append(o)
    return out


def read_activity(path):
    """system_activity.csv rows become activity observations (claims, not proof)."""
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        rid = r["resource_id"]
        o = Observation(r["activity_id"], r["event_time"], r["event_time"], rid,
                        system_of(rid) or r["target_system"], None, kind="activity")
        o.attrs.update(event_type=r["event_type"], source_system=r["source_system"],
                       target_system=r["target_system"], result=r["reported_result"],
                       details=r["details"])
        extract_identifiers(r["details"], o.links)
        # "repopulated downstream resource search_4882" style mentions
        others = [m.group(0) for m in RESOURCE_RE.finditer(r["details"]) if m.group(0) != rid]
        o.attrs["mentions"] = others
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# Loader for the whole evidence corpus
# ---------------------------------------------------------------------------
def load_manifest(dataset_dir):
    return {r["file_id"]: r for r in csv.DictReader(open(os.path.join(dataset_dir, "evidence_files.csv"),
                                                        encoding="utf-8"))}


def load_all_observations(dataset_dir, media_records=None):
    """Return (observations, manifest). media_records: {file_id: [record dicts]} from media.py."""
    manifest = load_manifest(dataset_dir)
    obs: list[Observation] = []
    for fid, m in manifest.items():
        path = os.path.join(dataset_dir, m["file_path"])
        cap = m["captured_at"]
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".json":
                obs += read_json(path, fid, cap)
            elif ext == ".sql":
                obs += read_sql(path, fid, cap)
            elif ext == ".log":
                obs += read_log(path, fid, cap)
            elif ext == ".txt":
                obs += read_handover_txt(path, fid, cap)
            elif ext in (".pdf", ".png", ".jpg", ".jpeg") and media_records is not None:
                for rec in media_records.get(fid, []):
                    # a screenshot / document shows the record as it was when it was
                    # captured; dates printed inside it (submitted, created...) are
                    # content, not the observation time
                    o = _record_to_obs(rec, fid, cap, cap)
                    if o:
                        o.attrs["media"] = True
                        obs.append(o)
        except Exception as exc:  # one bad file must not kill the run
            print(f"[evidence] failed to parse {path}: {exc}")
    obs += read_activity(os.path.join(dataset_dir, "system_activity.csv"))
    return obs, manifest
