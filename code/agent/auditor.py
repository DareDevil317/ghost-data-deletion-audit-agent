"""Deletion auditor: the deterministic core.

For one deletion request it
  1. resolves the subject's current + historical identifiers,
  2. links every resource that belongs to the subject (evidence identifiers, data-catalog
     links, and the deletion workflow's own activity around the request),
  3. reconstructs each resource's state at audit_at from the latest admissible evidence,
  4. applies deletion_rules.csv per data category,
  5. emits ordered FINDING / REMEDIATION / VERIFICATION steps.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from dataclasses import dataclass, field

from .evidence import Observation, system_of
from .identity import IdentityIndex

# ---------------------------------------------------------------------------
# Ordering tables (from problem_statement.md "Allowed Operations")
# ---------------------------------------------------------------------------
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OP_ORDER = {op: i for i, op in enumerate([
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
])}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# system -> default data category (rule key in deletion_rules.csv)
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "support_platform": "SUPPORT_MESSAGE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "telemetry_service": "PRODUCT_TELEMETRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
}
# activity event types that reveal which treatment the workflow attempted
ACTIVITY_TREATMENT = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT",
                      "ANONYMIZE_RECORD": "ANONYMIZE"}
WORKFLOW_EVENTS = {"DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD", "DISABLE_DATA_FLOW",
                   "BLOCK_RESTORE", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD"}


def load_rules(dataset_dir):
    rules = {}
    for r in csv.DictReader(open(os.path.join(dataset_dir, "deletion_rules.csv"), encoding="utf-8")):
        rules[r["data_category"]] = r
    return rules


@dataclass
class ResourceView:
    resource_id: str
    system: str
    link_reasons: set = field(default_factory=set)
    state_obs: Observation | None = None       # latest admissible evidence observation
    category: str | None = None
    treatment: str | None = None               # DELETE / REDACT / ANONYMIZE / RETAIN / DISABLE / BLOCK


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    user_id: str | None = None
    notes: list = field(default_factory=list)


class Auditor:
    def __init__(self, dataset_dir: str, observations: list[Observation], manifest: dict,
                 all_requests: list[dict] | None = None):
        self.dataset_dir = dataset_dir
        self.ids = IdentityIndex(dataset_dir)
        self.rules = load_rules(dataset_dir)
        self.manifest = manifest
        self.obs = observations
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        self.activity: list[Observation] = []
        for o in observations:
            self.by_resource[o.resource_id].append(o)
            if o.kind == "activity":
                self.activity.append(o)
        self.activity.sort(key=lambda o: o.observed_at)
        # token -> resources whose evidence mentions that token
        self.token_resources: dict[str, set] = defaultdict(set)
        for o in observations:
            for t in o.links:
                self.token_resources[t].add(o.resource_id)
        self.all_requests = all_requests or []
        self.workflow_assignments = self.assign_workflow(self.all_requests)
        self.current_subject = None
        # SQL files whose primary/replica tables fully capture a system at a point in time
        self.sql_coverage = {}
        self.primary_files = set()
        for o in observations:
            if o.kind == "evidence" and o.attrs.get("scope"):
                self.sql_coverage.setdefault(o.source_id, (set(), set()))[0].add(o.system)
        for o in observations:
            if o.kind == "evidence" and "table" in o.attrs:
                t = o.attrs["table"]
                if o.source_id in self.sql_coverage and "primary" not in t:
                    self.sql_coverage[o.source_id][1].add(o.resource_id)
                if "primary" in t:
                    self.primary_files.add(o.source_id)
                    systems, present = self.sql_coverage.setdefault(o.source_id, (set(), set()))
                    systems.add(o.system)
                    present.add(o.resource_id)
        self.tombstones_by_user = defaultdict(list)
        for o in observations:
            if o.kind == "evidence" and o.system == "identity_service" and \
                    o.attrs.get("account_status") == "DELETED":
                for t in o.links:
                    if t.startswith("u:"):
                        self.tombstones_by_user[t[2:]].append(o)

    # ------------------------------------------------------------------
    def _admissible(self, o: Observation, audit_at: str, for_citation: bool) -> bool:
        if o.observed_at and o.observed_at > audit_at:
            return False
        if for_citation and o.kind == "evidence":
            return self.manifest.get(o.source_id, {}).get("captured_at", "") <= audit_at
        return True

    def _owner_users(self, resource_id: str, audit_at: str) -> set:
        """Users that any admissible evidence links this resource to."""
        owners = set()
        for o in self.by_resource.get(resource_id, []):
            if o.observed_at > audit_at:
                continue
            for t in o.links:
                owners |= self.ids.owners(t)
        return owners

    # ------------------------------------------------------------------
    def link_resources(self, ident, req) -> dict[str, ResourceView]:
        audit_at = req["audit_at"]
        views: dict[str, ResourceView] = {}

        def add(rid, reason):
            sysid = system_of(rid)
            if not sysid:
                return
            v = views.setdefault(rid, ResourceView(rid, sysid))
            v.link_reasons.add(reason)

        # 1) identifier links in evidence / activity details
        for t in ident.tokens:
            for rid in self.token_resources.get(t, ()):
                for o in self.by_resource[rid]:
                    if t in o.links and o.observed_at <= audit_at:
                        add(rid, f"{t[0]}-link:{o.source_id}")
                        break

        # 2) deletion-workflow activity assigned to this request (see assign_workflow)
        for rid, act_id in self.workflow_assignments.get(req["case_id"], []):
            if rid in views:
                views[rid].link_reasons.add("workflow:" + act_id)
            else:
                add(rid, "workflow:" + act_id)

        # 3) same subject had an earlier deletion request: records only ever seen before this
        #    request was made belong to that earlier episode, not to this one.
        earlier = [r for r in self.all_requests
                   if r["case_id"] != req["case_id"] and self.ids.resolve(r["email"]) == ident.user_id
                   and r["audit_at"] < req["requested_at"]]
        if earlier:
            for rid in list(views):
                v = views[rid]
                if any(x.startswith("workflow:") for x in v.link_reasons):
                    continue
                last = max((o.observed_at for o in self.by_resource[rid]
                            if o.links & ident.tokens and o.observed_at <= audit_at), default="")
                if last < req["requested_at"]:
                    del views[rid]

        # 4) no profile resource found at all: fall back to the platform's account naming
        #    convention (account_<NNN> mirrors user_<NN>, as seen in the replica snapshots),
        #    but only when that id is not tied to anyone else.
        if not any(v.system == "account_service" for v in views.values()):
            num = ident.user_id.split("_")[-1]
            if num.isdigit():
                rid = f"account_{int(num):03d}"
                owners = self._owner_users(rid, "9999")
                if not owners or ident.user_id in owners:
                    add(rid, "naming-convention")
        return views

    # ------------------------------------------------------------------
    def assign_workflow(self, requests: list[dict]) -> dict:
        """Attribute deletion-worker activity to requests.

        The deletion workflow acts on a request's resources in a window around the time it
        reported its status.  Several requests can share a window, so resources that
        evidence already ties to a person are excluded, a request is only a candidate for
        a system if evidence has not already given it a resource there, and remaining
        ambiguous batches are matched in order (activity time vs case_id) only when the
        counts line up exactly.  Anything still ambiguous is left unassigned rather than
        guessed.
        """
        from collections import defaultdict
        windows = []
        for r in requests:
            uid = self.ids.resolve(r["email"])
            lo = _shift(r["reported_at"], hours=-WINDOW_HOURS)
            hi = min(_shift(r["reported_at"], hours=WINDOW_HOURS), r["audit_at"])
            windows.append((r, uid, lo, hi))
        # systems each request already has evidence-linked resources in
        has_system = defaultdict(set)
        for r, uid, lo, hi in windows:
            if not uid:
                continue
            for t in self.ids.identity(uid).tokens:
                for rid in self.token_resources.get(t, ()):
                    has_system[r["case_id"]].add(system_of(rid))
        groups = defaultdict(list)
        for a in self.activity:
            if a.attrs.get("source_system") != "deletion_worker":
                continue
            if a.attrs.get("event_type") not in WORKFLOW_EVENTS:
                continue
            owners = self._owner_users(a.resource_id, "9999")
            cands = []
            for r, uid, lo, hi in windows:
                if not uid or not (lo <= a.observed_at <= hi):
                    continue
                if owners:
                    if uid in owners:
                        cands = [r]
                        break
                    continue
                cands.append(r)
            if not cands:
                continue
            # the workflow's status claim comes from its own primary-profile deletion result
            if len(cands) > 1 and a.system == "account_service" and a.attrs.get("event_type") == "DELETE_RECORD":
                want = RESULT_FOR_STATUS
                same = [r for r in cands if want.get(r["reported_status"]) == a.attrs.get("result")]
                cands = same or cands
            if len(cands) > 1:
                need = [r for r in cands if system_of(a.resource_id) not in has_system[r["case_id"]]]
                cands = need or cands
            if len(cands) > 1:
                # one request clearly closest to the activity (within 30 min, rivals over 1 h away)
                dist = sorted((_minutes_between(a.observed_at, r["reported_at"]), r["case_id"], r) for r in cands)
                if dist[0][0] <= 30 and dist[1][0] >= 60:
                    cands = [dist[0][2]]
            key = (a.attrs.get("event_type"), a.system, tuple(sorted(c["case_id"] for c in cands)))
            groups[key].append((a, cands))
        out = defaultdict(list)
        for key, items in groups.items():
            cands = sorted(items[0][1], key=lambda r: _case_num(r["case_id"]))
            acts = sorted({a.resource_id: a for a, _ in items}.values(), key=lambda a: a.observed_at)
            if len(cands) == 1:
                for a in acts:
                    out[cands[0]["case_id"]].append((a.resource_id, a.source_id))
            elif ZIP_AMBIGUOUS and len(acts) == len(cands):
                for a, r in zip(acts, cands):
                    out[r["case_id"]].append((a.resource_id, a.source_id))
        return out

    # ------------------------------------------------------------------
    def latest_state(self, rid: str, audit_at: str) -> Observation | None:
        def key(o):
            return (o.observed_at, self.manifest.get(o.source_id, {}).get("captured_at", ""), o.source_id)

        cands = [o for o in self.by_resource.get(rid, [])
                 if o.kind == "evidence" and self._admissible(o, audit_at, for_citation=True)
                 and o.exists is not None]
        best = max(cands, key=key) if cands else self._absence_proof(rid, audit_at)
        # A fact observed before audit_at but only exported in a later file may be newer than
        # anything citable.  Use its state, but never cite the later file.
        late = [o for o in self.by_resource.get(rid, [])
                if o.kind == "evidence" and o.exists is not None and o.observed_at <= audit_at
                and not self._admissible(o, audit_at, for_citation=True)]
        if late and not cands:
            newest = max(late, key=key)
            if best is None or (newest.observed_at > best.observed_at and newest.exists != best.exists):
                import copy
                best = copy.copy(newest)
                best.source_id = ""
        return best

    def _absence_proof(self, rid: str, audit_at: str) -> Observation | None:
        """No direct observation of this resource: look for evidence that proves absence.

        * a primary/replica SQL snapshot of the same system that does not contain the
          resource (point-in-time table captures are complete for their table);
        * for a user profile, the identity service's deleted-account reference
          (tombstone, account_status=DELETED) for the same subject.
        """
        sysid = system_of(rid)
        best = None
        # priority: primary-table snapshots, then deleted-account references, then
        # scoped inventories (latest admissible within each tier)
        tiers = {0: None, 1: None, 2: None}
        for fid, (systems, present) in self.sql_coverage.items():
            cap = self.manifest[fid]["captured_at"]
            if cap > audit_at or sysid not in systems or rid in present:
                continue
            tier = 0 if fid in self.primary_files else 2
            if tiers[tier] is None or cap > tiers[tier][0]:
                tiers[tier] = (cap, fid)
        if self.current_subject and sysid == "account_service":
            for o in self.tombstones_by_user.get(self.current_subject, []):
                cap = self.manifest.get(o.source_id, {}).get("captured_at", "")
                if cap <= audit_at and o.observed_at <= audit_at and (tiers[1] is None or cap > tiers[1][0]):
                    tiers[1] = (cap, o.source_id)
        strong = [t for t in (tiers[0], tiers[1]) if t]
        best = max(strong) if strong else tiers[2]
        if best is None:
            return None
        return Observation(best[1], best[0], best[0], rid, sysid, exists=False)

    def attempted_treatment(self, rid: str, audit_at: str) -> str | None:
        for a in reversed(self.activity):
            if a.resource_id == rid and a.observed_at <= audit_at:
                t = ACTIVITY_TREATMENT.get(a.attrs.get("event_type"))
                if t:
                    return t
        return None

    def categorize(self, v: ResourceView, audit_at: str) -> None:
        cat = None
        for o in self.by_resource.get(v.resource_id, []):
            if o.record_type and o.record_type in self.rules and o.observed_at <= audit_at:
                cat = o.record_type
        if cat is None:
            cat = SYSTEM_CATEGORY.get(v.system)
        attempted = self.attempted_treatment(v.resource_id, audit_at)
        if v.system == "support_platform" and cat == "SUPPORT_MESSAGE" and attempted == "DELETE":
            cat = "SUPPORT_ATTACHMENT"
        if v.system == "analytics_platform" and attempted == "DELETE":
            cat = "SECONDARY_IDENTIFIER"
        v.category = cat
        action = self.rules.get(cat, {}).get("required_action") if cat in self.rules else None
        if cat == "DATA_FLOW":
            action = "DISABLE"
        v.treatment = action

    # ------------------------------------------------------------------
    def audit(self, req: dict, authorized: bool) -> CaseResult:
        cid = req["case_id"]
        if not authorized:
            return CaseResult(cid, "NOT_APPLICABLE", [])
        audit_at = req["audit_at"]
        uid = self.ids.resolve(req["email"])
        if uid is None:
            return CaseResult(cid, "COMPLETE", [], notes=["subject not found"])
        ident = self.ids.identity(uid)
        self.current_subject = uid
        views = self.link_resources(ident, req)

        findings, remediation, verification = [], [], []
        for rid, v in views.items():
            self.categorize(v, audit_at)
            st = self.latest_state(rid, audit_at)
            v.state_obs = st
            ev = st.source_id if st else ""
            exists = st.exists if st else None
            t = v.treatment
            if t is None:
                continue
            if t == "DELETE":
                if exists:
                    findings.append(("REPORT_REMAINING_RECORD", v, ev))
                    remediation.append(("DELETE_RECORD", v))
                verification.append(("VERIFY_RECORD_ABSENT", v, ev if not exists else ""))
            elif t in ("REDACT", "ANONYMIZE"):
                if exists is False:
                    # record gone entirely: still compliant (nothing identifiable left)
                    verification.append(("VERIFY_RECORD_ABSENT", v, ev))
                    continue
                identifiable = bool(st and (st.pii or (t == "ANONYMIZE" and st.has_reference
                                                         and v.system != "identity_service")))
                if st and st.status in ("IDENTIFIABLE",):
                    identifiable = True
                if st and st.status in ("ANONYMIZED", "REDACTED"):
                    identifiable = False
                op_v = "VERIFY_RECORD_REDACTED" if t == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
                if identifiable:
                    findings.append(("REPORT_EXPOSED_PII", v, ev))
                    remediation.append(("REDACT_RECORD" if t == "REDACT" else "ANONYMIZE_RECORD", v))
                    verification.append((op_v, v, ""))
                else:
                    verification.append((op_v, v, ev))
            elif t == "RETAIN":
                if v.system == "backup_service":
                    allowed = st.restore_allowed if st else None
                    if allowed:
                        findings.append(("REPORT_RESTORE_RISK", v, ev))
                        remediation.append(("BLOCK_RESTORE", v))
                        verification.append(("VERIFY_RESTORE_BLOCKED", v, ""))
                    else:
                        verification.append(("VERIFY_RESTORE_BLOCKED", v, ev))
                else:
                    if exists is not False:
                        verification.append(("VERIFY_RECORD_PRESENT", v, ev))
            elif t == "DISABLE":
                enabled = st.enabled if st else None
                if enabled:
                    findings.append(("REPORT_ACTIVE_DATA_FLOW", v, ev))
                    remediation.append(("DISABLE_DATA_FLOW", v))
                    verification.append(("VERIFY_DATA_FLOW_DISABLED", v, ""))
                else:
                    verification.append(("VERIFY_DATA_FLOW_DISABLED", v, ev))

        status = "INCOMPLETE" if findings else "COMPLETE"
        notes = [{"resource_id": rid, "category": v.category, "treatment": v.treatment,
                  "linked_by": sorted(v.link_reasons),
                  "state": None if not v.state_obs else
                  {"exists": v.state_obs.exists, "source": v.state_obs.source_id,
                   "observed_at": v.state_obs.observed_at, "pii": sorted(v.state_obs.pii),
                   "enabled": v.state_obs.enabled, "restore_allowed": v.state_obs.restore_allowed}}
                 for rid, v in sorted(views.items())]
        steps = []
        if status == "INCOMPLETE":
            for op, v, ev in findings:
                steps.append(_step("FINDING", v, op, ev))
            for op, v in remediation:
                steps.append(_step("REMEDIATION", v, op, ""))
            for op, v, ev in verification:
                steps.append(_step("VERIFICATION", v, op, ""))
        else:
            for op, v, ev in verification:
                steps.append(_step("VERIFICATION", v, op, ev))
        steps = order_steps(steps)
        return CaseResult(cid, status, steps, user_id=uid, notes=notes)


def _step(phase, v, op, ev):
    return {"step_no": 0, "phase": phase, "system_id": v.system, "resource_id": v.resource_id,
            "operation": op, "state": OP_STATE[op], "evidence_id": ev or ""}


def _rid_key(rid: str):
    return rid


def order_steps(steps: list[dict]) -> list[dict]:
    seen, uniq = set(), []
    for s in steps:
        k = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    uniq.sort(key=lambda s: (PHASE_ORDER[s["phase"]], OP_ORDER[s["operation"]],
                             s["system_id"], _rid_key(s["resource_id"])))
    for i, s in enumerate(uniq, 1):
        s["step_no"] = i
    return uniq


WINDOW_HOURS = 2
ZIP_AMBIGUOUS = True
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


def _minutes_between(a: str, b: str) -> float:
    from datetime import datetime
    da = datetime.fromisoformat(a.replace("Z", "+00:00"))
    db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return abs((da - db).total_seconds()) / 60


def _case_num(cid: str) -> int:
    try:
        return int(cid.split("_")[-1])
    except ValueError:
        return 0


def _shift(ts: str, hours: int) -> str:
    from datetime import datetime, timedelta
    d = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(hours=hours)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")
