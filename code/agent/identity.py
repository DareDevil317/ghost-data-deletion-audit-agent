"""Identity resolution: map a request email to a user and all of their identifiers,
current and historical (from user_audit_log.csv)."""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from dataclasses import dataclass, field

from .evidence import extract_identifiers, norm_email, norm_phone


@dataclass
class Identity:
    user_id: str
    tokens: set = field(default_factory=set)       # e:/p:/h:/u: identifier tokens
    names: set = field(default_factory=set)
    current_email: str = ""
    history_audit_ids: list = field(default_factory=list)


class IdentityIndex:
    def __init__(self, dataset_dir: str):
        self.users = {r["user_id"]: r for r in csv.DictReader(
            open(os.path.join(dataset_dir, "user_identities.csv"), encoding="utf-8"))}
        self.audit = list(csv.DictReader(
            open(os.path.join(dataset_dir, "user_audit_log.csv"), encoding="utf-8")))
        self.email_to_user: dict[str, str] = {}
        self.token_owner: dict[str, set] = defaultdict(set)
        for uid, r in self.users.items():
            self.email_to_user[norm_email(r["email"])] = uid
        for a in self.audit:
            if a["field_name"] == "email":
                for v in (a["old_value"], a["new_value"]):
                    self.email_to_user.setdefault(norm_email(v), a["user_id"])
        for uid in self.users:
            for t in self.identity(uid).tokens:
                self.token_owner[t].add(uid)

    def resolve(self, email: str) -> str | None:
        return self.email_to_user.get(norm_email(email))

    def identity(self, uid: str) -> Identity:
        ident = Identity(uid)
        ident.tokens.add("u:" + uid)
        r = self.users.get(uid)
        if r:
            ident.current_email = norm_email(r["email"])
            ident.tokens.add("e:" + ident.current_email)
            if r["email_hash"]:
                ident.tokens.add("h:" + r["email_hash"].lower())
            p = norm_phone(r["phone"])
            if p:
                ident.tokens.add("p:" + p)
            ident.names.add(r["name"].strip().lower())
        for a in self.audit:
            if a["user_id"] != uid:
                continue
            ident.history_audit_ids.append(a["audit_id"])
            for v in (a["old_value"], a["new_value"]):
                if a["field_name"] == "phone":
                    p = norm_phone(v)
                    if p:
                        ident.tokens.add("p:" + p)
                elif a["field_name"] in ("name", "full_name"):
                    ident.names.add(v.strip().lower())
                else:
                    extract_identifiers(v, ident.tokens)
        return ident

    def owners(self, token: str) -> set:
        return self.token_owner.get(token, set())
