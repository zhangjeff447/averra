"""Private, credential-free timing records for read-only evidence requests."""
from __future__ import annotations

import argparse
import json
import logging
import socket
import ssl
import time
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "source_requests.jsonl"


def error_class(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP_429" if exc.code == 429 else "HTTP_5XX" if exc.code >= 500 else f"HTTP_{exc.code}"
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, socket.gaierror):
        return "DNS_ERROR"
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return "TIMEOUT"  # urllib cannot reliably distinguish connect from read timeout.
    if isinstance(cause, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(cause, ConnectionResetError):
        return "CONNECTION_RESET"
    if isinstance(exc, json.JSONDecodeError):
        return "INVALID_JSON"
    return "INVALID_DATA" if isinstance(exc, ValueError) else "NETWORK_ERROR"


class RequestAudit:
    def __init__(self, path=DEFAULT_PATH, *, enabled=True):
        self.path, self.enabled = Path(path), enabled

    def record(self, source, url, attempt, started, *, error=None, status=None,
               response_bytes=None, retry_delay_seconds=0):
        if not self.enabled:
            return
        # Do not retain URLs, paths, query strings, headers, bodies, or exception text.
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "source": source,
                  "host": urlsplit(url).hostname, "attempt": attempt + 1,
                  "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                  "outcome": "ERROR" if error else "SUCCESS",
                  "error_class": error_class(error) if error else None,
                  "http_status": status, "response_bytes": response_bytes,
                  "retry_delay_seconds": retry_delay_seconds}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            logging.getLogger(__name__).warning("REQUEST_DIAGNOSTICS_WRITE_FAILED")


def summarize(path=DEFAULT_PATH, day=None):
    groups = {}
    malformed = 0
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                if day and not row["timestamp"].startswith(day):
                    continue
                key = row["source"]
                group = groups.setdefault(key, {"requests": 0, "retries": 0,
                    "request_elapsed_ms": 0, "max_request_ms": 0, "errors": Counter()})
                elapsed = float(row["elapsed_ms"])
                group["requests"] += 1
                group["retries"] += int(row["attempt"] > 1)
                group["request_elapsed_ms"] += elapsed
                group["max_request_ms"] = max(group["max_request_ms"], elapsed)
                if row["error_class"]:
                    group["errors"][row["error_class"]] += 1
            except (ValueError, KeyError, TypeError):
                malformed += 1
    for group in groups.values():
        group["request_elapsed_ms"] = round(group["request_elapsed_ms"], 2)
    return {"date_utc": day, "sources": groups, "malformed_rows": malformed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="UTC date, YYYY-MM-DD")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    print(json.dumps(summarize(args.path, args.date), indent=2))
