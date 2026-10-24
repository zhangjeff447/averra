"""Conservative, local sector provenance and benchmark eligibility."""
from __future__ import annotations


def classify(raw_sector, industry, source, cfg, ticker=None):
    mapping = cfg["sector_to_etf"]
    override = cfg.get("verified_ticker_overrides", {}).get(ticker)
    if override:
        normalized = override["sector"]
        return normalized, mapping[normalized], override["confidence"]
    if not raw_sector or raw_sector not in mapping:
        return None, None, cfg["confidence_for_missing"]
    confidence = "HIGH" if source == "GICS_VERIFIED" else cfg["confidence_for_known_nasdaq_sector"]
    if source != "GICS_VERIFIED" and any(
        term.lower() in (industry or "").lower() for term in cfg["low_confidence_industry_contains"]
    ):
        confidence = "LOW"
    if confidence == "LOW":
        return None, None, confidence
    return raw_sector, mapping[raw_sector], confidence


def apply_mapping(db, cfg):
    updates = []
    for row in db.execute("SELECT ticker,sector,industry,sector_source FROM securities"):
        normalized, etf, confidence = classify(row[1], row[2], row[3], cfg, row[0])
        override = cfg.get("verified_ticker_overrides", {}).get(row[0])
        updates.append((row[1], normalized, etf, confidence, override["source"] if override else row[3], row[0]))
    with db:
        db.executemany("""UPDATE securities SET raw_sector=?,normalized_sector=?,
            mapped_sector_etf=?,mapping_confidence=?,mapping_source=? WHERE ticker=?""", updates)
    return len(updates)


def sector_benchmark(stock, cfg):
    confidence = stock["mapping_confidence"] if "mapping_confidence" in stock.keys() else None
    etf = stock["mapped_sector_etf"] if "mapped_sector_etf" in stock.keys() else None
    used = bool(etf and confidence in {"HIGH", "MEDIUM"})
    return etf if used else None, {
        "sector_rs_used": "YES" if used else "NO",
        "sector_rs_weight": None if used else 0.0,
        "fallback_benchmark": None if used else cfg["fallback_benchmark"],
        "mapping_confidence": confidence or "UNKNOWN",
    }
