#!/usr/bin/env python3
"""Import a reviewed manifest from official BLS/BEA/Fed/NYSE calendars."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_calendar import import_events, import_market_sessions, set_coverage
from intraday_runner import config, connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    cfg = config()
    db = connect()
    try:
        count = import_events(db, manifest["events"], cfg)
        sessions = import_market_sessions(db, manifest.get("market_sessions", []))
        for item in manifest["coverage"]:
            set_coverage(db, item["authority"], item["verified_through"], item["source"], cfg)
        print(json.dumps({"imported_events": count,
                          "market_sessions": sessions,
                          "coverage_authorities": sorted(c["authority"] for c in manifest["coverage"])}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
