"""Deterministic evidence citation aliases and strict legacy-ID validation."""
from __future__ import annotations

import json
import re

from phase4_engine import compact

TYPE_ALIASES = {
    "EARNINGS_RELEASE": "EARNINGS",
    "SEC_MATERIAL": "SEC_MATERIAL_EVENT",
    "EXEC_CHANGE": "EXECUTIVE_CHANGE",
}
ID_FIELDS = ("recent_earnings", "guidance", "material_sec_events", "key_risk_evidence")


def normalize_id(value):
    """Normalize syntax only; never infer a different event date or type."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in (3, 4):
        return None
    ticker = parts[0].strip().upper()
    raw_day = re.sub(r"[/-]", "", parts[1].strip())
    if not re.fullmatch(r"[A-Z0-9.]{1,12}", ticker) or not re.fullmatch(r"\d{8}", raw_day):
        return None
    event_type = re.sub(r"[^A-Z0-9]+", "_", parts[2].strip().upper()).strip("_")
    event_type = TYPE_ALIASES.get(event_type, event_type)
    accession = re.sub(r"[^A-Z0-9]", "", parts[3].upper()) if len(parts) == 4 else ""
    return ticker, raw_day, event_type, accession


def validate_id(value, canonical_ids):
    known = set(canonical_ids)
    if value in known:
        return {"reason": "EXACT_MATCH", "canonical_id": value, "candidates": [value]}
    key = normalize_id(value)
    matches = sorted(item for item in known if key is not None and normalize_id(item) == key)
    if len(matches) == 1:
        return {"reason": "NORMALIZABLE_MATCH", "canonical_id": matches[0], "candidates": matches}
    if len(matches) > 1:
        return {"reason": "AMBIGUOUS_MATCH", "canonical_id": None, "candidates": matches}
    return {"reason": "NO_MATCH", "canonical_id": None, "candidates": []}


def short_citation_packet(evidence):
    """Return an aliased copy; the stored canonical evidence is never changed."""
    packet = json.loads(compact(evidence))
    canonical_to_short = {}
    short_to_canonical = {}
    for event in packet["recent_material_events"] + packet["other_events"]:
        canonical = event["duplicate_group"]
        if canonical in canonical_to_short:
            raise ValueError("duplicate canonical evidence ID")
        short = f"E{len(short_to_canonical) + 1}"
        canonical_to_short[canonical] = short
        short_to_canonical[short] = canonical
        event["duplicate_group"] = short
        event.pop("event_id", None)
    for field in ID_FIELDS:
        packet[field] = [canonical_to_short[item] for item in packet.get(field, [])]
    packet["citation_schema"] = "Cite only the short evidence IDs E1, E2, ... supplied here."
    return packet, short_to_canonical


def canonicalize_ids(ids, allowed):
    """A dict is an E-number map; a set is the historical canonical-ID mode."""
    if isinstance(allowed, dict):
        if not set(ids) <= allowed.keys():
            raise ValueError("unsupported short evidence ID")
        return [allowed[item] for item in ids]
    if not set(ids) <= set(allowed):
        raise ValueError("unsupported claim evidence ID")
    return list(ids)
