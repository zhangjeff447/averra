import json
import os
import queue
import re
import signal
import sys
import multiprocessing as mp
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class QuoteProvider:
    QUOTE_TOOL_WHITELIST = ["get_equity_quotes", "get_equity_price_book"]

    def __init__(
        self,
        adapter: Optional[Any] = None,
        cache_dir: Optional[Path] = None,
        alpha_vantage_api_key: Optional[str] = None,
        network_timeout_seconds: int = 10,
    ):
        self.adapter = adapter
        self.cache_dir = Path(cache_dir or Path("data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.latest_cache_path = self.cache_dir / "quotes_latest.json"
        self.alpha_vantage_api_key = alpha_vantage_api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
        self.network_timeout_seconds = max(1, int(network_timeout_seconds))
        self.runtime_tools: list[Dict[str, Any]] = []
        self.quote_tool_selected: Optional[Dict[str, Any]] = None
        self._refresh_runtime_tools()

    @staticmethod
    def _trace_params(function_name: str, tool_name: str, params: Dict[str, Any]) -> None:
        safe_params = params if isinstance(params, dict) else {}
        keys = sorted(str(k) for k in safe_params.keys())
        symbols_raw = safe_params.get("symbols")
        symbols: List[str] = []
        if isinstance(symbols_raw, list):
            symbols = [str(s).upper() for s in symbols_raw if str(s).strip()]
        elif isinstance(safe_params.get("symbol"), str) and str(safe_params.get("symbol")).strip():
            symbols = [str(safe_params.get("symbol")).upper()]
        print(
            f"[trace] function={function_name} tool={tool_name} params_keys={keys} symbols={symbols}",
            file=sys.stderr,
            flush=True,
        )

    def _unavailable_quote(self, symbol: str, source: str = "unavailable", stale_reason: str = "quote_unavailable") -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "last": None,
            "bid": None,
            "ask": None,
            "source": source,
            "timestamp": _now_iso(),
            "is_realtime": False,
            "is_stale": True,
            "stale_reason": stale_reason,
        }

    def _run_callable_with_signal_timeout(self, fn, timeout_seconds: int):
        if timeout_seconds <= 0:
            return fn()

        if os.name != "posix":
            return fn()

        class _Timeout(Exception):
            pass

        def _handler(signum, frame):
            raise _Timeout()

        old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
        try:
            return fn()
        except _Timeout:
            return None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    def _run_process_with_timeout(self, target, args, timeout_seconds: int) -> Optional[Dict[str, Any]]:
        q: mp.Queue = mp.Queue(maxsize=1)
        proc = mp.Process(target=target, args=(*args, q), daemon=True)
        proc.start()
        proc.join(timeout_seconds)
        if proc.is_alive():
            proc.terminate()
            proc.join(1)
            if proc.is_alive():
                proc.kill()
                proc.join(1)
            return None
        try:
            payload = q.get_nowait()
            if isinstance(payload, dict):
                return payload
            return None
        except queue.Empty:
            return None

    def _normalize_quote(self, symbol: str, payload: Dict[str, Any], source: str, is_realtime: bool, is_stale: bool, stale_reason: Optional[str]) -> Optional[Dict[str, Any]]:
        if source == "robinhood_mcp":
            from qqq_mcp_adapter import MCPAdapter
            normalizer = object.__new__(MCPAdapter)
            quote = normalizer._normalize_quote(symbol, payload)
            if quote is not None and (is_stale or not is_realtime):
                quote.update(is_stale=True, is_realtime=False, stale_reason=stale_reason or "source_not_realtime")
            return quote
        last = _safe_float(
            payload.get("last_trade_price")
            or payload.get("last_non_reg_trade_price")
            or payload.get("price")
            or payload.get("mark_price")
            or payload.get("last")
            or payload.get("05. price")
            or payload.get("close")
        )
        bid = _safe_float(payload.get("bid_price") or payload.get("bid") or payload.get("best_bid"))
        ask = _safe_float(payload.get("ask_price") or payload.get("ask") or payload.get("best_ask"))

        if bid == 0:
            bid = None
        if ask == 0:
            ask = None

        if last is None and bid is None and ask is None:
            return None

        if last is None:
            if bid is not None and ask is not None:
                last = (bid + ask) / 2.0
            elif bid is not None:
                last = bid
            elif ask is not None:
                last = ask

        ts = _parse_time(
            payload.get("last_trade_at")
            or payload.get("last_trade_timestamp")
            or payload.get("updated_at")
            or payload.get("timestamp")
            or payload.get("as_of")
        )
        if ts is None:
            ts = datetime.now(timezone.utc)

        computed_is_stale = bool(is_stale)
        computed_stale_reason = stale_reason
        if source == "robinhood_mcp" and bool(is_realtime):
            age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
            computed_is_stale = age_seconds > 60
            computed_stale_reason = "quote_older_than_60s" if computed_is_stale else None

        return {
            "symbol": symbol,
            "last": last,
            "bid": bid,
            "ask": ask,
            "source": source,
            "timestamp": ts.isoformat(),
            "is_realtime": bool(is_realtime),
            "is_stale": bool(computed_is_stale),
            "stale_reason": computed_stale_reason,
        }

    def _extract_equity_quote_payload(self, payload: Any, symbol: str) -> Optional[Dict[str, Any]]:
        sym = str(symbol or "").upper()

        def _maybe_parse_json_text(text: str) -> Optional[Any]:
            candidate = str(text or "").strip()
            if not candidate:
                return None
            try:
                return json.loads(candidate)
            except Exception:
                pass
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                snippet = candidate[start : end + 1]
                try:
                    return json.loads(snippet)
                except Exception:
                    return None
            return None

        def _json_candidates(text: str) -> list[Any]:
            candidates: list[Any] = []
            candidate = str(text or "").strip()
            if not candidate:
                return candidates
            try:
                parsed = json.loads(candidate)
                candidates.append(parsed)
            except Exception:
                pass
            decoder = json.JSONDecoder()
            cursor = 0
            while cursor < len(candidate):
                while cursor < len(candidate) and candidate[cursor].isspace():
                    cursor += 1
                if cursor >= len(candidate):
                    break
                try:
                    parsed, cursor = decoder.raw_decode(candidate, cursor)
                except json.JSONDecodeError:
                    break
                candidates.append(parsed)
            return candidates

        def _matches_quote(obj: Any) -> bool:
            if not isinstance(obj, dict):
                return False
            obj_symbol = str(obj.get("symbol") or obj.get("ticker") or obj.get("asset") or "").upper()
            if obj_symbol == sym:
                return True
            price_keys = ("last", "last_trade_price", "price", "mark_price", "05. price", "close", "bid", "ask")
            return any(obj.get(key) is not None for key in price_keys)

        def _search(obj: Any) -> Optional[Dict[str, Any]]:
            if isinstance(obj, dict):
                if isinstance(obj.get("quote"), dict):
                    quote = dict(obj.get("quote") or {})
                    if not quote.get("symbol"):
                        parent_symbol = obj.get("symbol") or obj.get("ticker")
                        if isinstance(parent_symbol, str) and parent_symbol.strip():
                            quote["symbol"] = parent_symbol
                    if _matches_quote(quote):
                        qsym = str(quote.get("symbol") or quote.get("ticker") or quote.get("asset") or "").upper()
                        if qsym in ("", sym):
                            return quote
                if _matches_quote(obj) and str(obj.get("symbol") or obj.get("ticker") or obj.get("asset") or "").upper() in ("", sym):
                    return obj
                direct = obj.get(sym)
                if isinstance(direct, dict):
                    return direct
                for key in ("quotes", "data", "result", "items", "payload"):
                    found = _search(obj.get(key))
                    if found is not None:
                        return found
                for value in obj.values():
                    found = _search(value)
                    if found is not None:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _search(item)
                    if found is not None:
                        return found
            elif isinstance(obj, str):
                for parsed in _json_candidates(obj):
                    found = _search(parsed)
                    if found is not None:
                        return found
            return None

        return _search(payload)

    def _tool_text(self, tool: Dict[str, Any]) -> str:
        name = str(tool.get("name") or "")
        description = str(tool.get("description") or "")
        return f"{name} {description}".lower()

    def _tool_expects_symbol(self, tool: Dict[str, Any]) -> bool:
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            return False
        properties = schema.get("properties")
        if isinstance(properties, dict) and "symbol" in properties:
            return True
        required = schema.get("required")
        if isinstance(required, list) and "symbol" in required:
            return True
        return False

    def _score_quote_tool(self, tool: Dict[str, Any]) -> int:
        text = self._tool_text(tool)
        score = 0
        if "quote" in text:
            score += 10
        if "equity" in text or "stock" in text:
            score += 6
        if "price" in text or "market" in text:
            score += 3
        if self._tool_expects_symbol(tool):
            score += 2
        return score

    def _refresh_runtime_tools(self) -> None:
        if self.adapter is None:
            self.runtime_tools = []
            self.quote_tool_selected = None
            return

        # The production read-only adapter deliberately does not perform tool
        # discovery: its batch prompt and allowlist are the authoritative MCP
        # configuration.  Discovery here would open a second session before
        # account initialization and was the source of the startup failure.
        if hasattr(self.adapter, "read_only_validation"):
            self.runtime_tools = [
                {"name": name, "description": None, "input_schema": None}
                for name in ("get_accounts", "get_portfolio", "get_equity_positions", "get_equity_orders", "get_equity_quotes")
            ]
            self.quote_tool_selected = {"name": "get_equity_quotes", "description": None, "input_schema": None}
            return

        tools: List[Dict[str, Any]] = []
        try:
            listed = self.adapter.list_tools() if hasattr(self.adapter, "list_tools") else []
            runtime_tools = getattr(self.adapter, "runtime_tools", None)
            if isinstance(runtime_tools, list):
                tools = [t for t in runtime_tools if isinstance(t, dict) and t.get("name")]
            elif isinstance(listed, list) and listed and all(isinstance(t, str) for t in listed):
                tools = [{"name": name, "description": None, "input_schema": None} for name in listed]
        except Exception:
            tools = []

        self.runtime_tools = tools
        available = {str(tool.get("name")) for tool in tools if isinstance(tool, dict) and tool.get("name")}
        selected_name: Optional[str] = None
        for tool_name in self.QUOTE_TOOL_WHITELIST:
            if not available or tool_name in available:
                selected_name = tool_name
                break
        if selected_name is None:
            self.quote_tool_selected = None
        else:
            self.quote_tool_selected = {"name": selected_name, "description": None, "input_schema": None}

    def _quote_tool_candidates(self) -> list[Dict[str, Any]]:
        if not self.runtime_tools:
            self._refresh_runtime_tools()
        available = {str(tool.get("name")) for tool in self.runtime_tools if isinstance(tool, dict) and tool.get("name")}
        out: list[Dict[str, Any]] = []
        for tool_name in self.QUOTE_TOOL_WHITELIST:
            if available and tool_name not in available:
                continue
            out.append({"name": tool_name, "description": None, "input_schema": None})
        return out

    def _fetch_mcp_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self.adapter is None:
            return None

        symbols = ["QQQ", "SMH"]

        for tool in self._quote_tool_candidates():
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            params = {"symbols": symbols}
            try:
                self._trace_params("quote_provider", tool_name, params)
                raw = self._run_callable_with_signal_timeout(
                    lambda tool_name=tool_name, params=params: self.adapter.get_equity_quotes(params["symbols"])
                    if tool_name == "get_equity_quotes"
                    else self.adapter.call_tool_exact(tool_name, params),
                    timeout_seconds=self.network_timeout_seconds,
                )
            except Exception:
                print(
                    f"[quote_provider] call exception tool={tool_name} symbol={symbol}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if raw is None:
                print(
                    f"[quote_provider] call timeout_or_none tool={tool_name} symbol={symbol}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            quote_payload = self._extract_equity_quote_payload(raw, symbol)
            if not isinstance(quote_payload, dict):
                print(
                    f"[quote_provider] call no_quote_payload tool={tool_name} symbol={symbol} raw_type={type(raw).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            quote = self._normalize_quote(symbol, quote_payload, "robinhood_mcp", is_realtime=True, is_stale=False, stale_reason=None)
            if quote is not None:
                self.quote_tool_selected = tool
                print(
                    f"[quote_provider] call success tool={tool_name} symbol={symbol} source={quote.get('source')} realtime={quote.get('is_realtime')}",
                    file=sys.stderr,
                    flush=True,
                )
                return quote
            print(
                f"[quote_provider] call normalize_failed tool={tool_name} symbol={symbol}",
                file=sys.stderr,
                flush=True,
            )
        return None

    @staticmethod
    def _yfinance_worker(symbol: str, out_q: mp.Queue) -> None:
        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
            fast_info = getattr(ticker, "fast_info", None)
            payload: Dict[str, Any] = {}
            if fast_info:
                payload["last"] = fast_info.get("lastPrice")
                payload["bid"] = fast_info.get("bid")
                payload["ask"] = fast_info.get("ask")

            if payload.get("last") is None and payload.get("bid") is None and payload.get("ask") is None:
                hist = ticker.history(period="1d", interval="1m")
                if hist is not None and not hist.empty:
                    payload["last"] = float(hist["Close"].dropna().iloc[-1])

            out_q.put(payload)
        except Exception:
            out_q.put({})

    def _from_yfinance(self, symbol: str) -> Optional[Dict[str, Any]]:
        payload = self._run_process_with_timeout(self._yfinance_worker, (symbol,), self.network_timeout_seconds)
        if not isinstance(payload, dict):
            return None
        return self._normalize_quote(symbol, payload, "yfinance", is_realtime=True, is_stale=False, stale_reason=None)

    def _from_alpha_vantage(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.alpha_vantage_api_key:
            return None
        try:
            import requests
        except Exception:
            return None

        try:
            url = (
                "https://www.alphavantage.co/query?"
                f"function=GLOBAL_QUOTE&symbol={symbol}&apikey={self.alpha_vantage_api_key}"
            )
            response = requests.get(url, timeout=(5, 5))
            data = response.json()
            global_quote = data.get("Global Quote") if isinstance(data, dict) else None
            if not isinstance(global_quote, dict):
                return None
            payload = {
                "last": global_quote.get("05. price"),
                "bid": global_quote.get("08. previous close"),
                "ask": global_quote.get("05. price"),
            }
            return self._normalize_quote(symbol, payload, "alphavantage", is_realtime=True, is_stale=False, stale_reason=None)
        except Exception:
            return None

    def _read_latest_cache(self) -> Dict[str, Any]:
        if not self.latest_cache_path.exists():
            return {}
        try:
            data = json.loads(self.latest_cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_latest_cache(self, data: Dict[str, Any]) -> None:
        self.latest_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.latest_cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _choose_best_cache_file(self, symbol: str) -> Optional[Path]:
        pattern = re.compile(rf"^{re.escape(symbol)}_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$")
        candidates = []
        for p in self.cache_dir.glob(f"{symbol}_*.csv"):
            m = pattern.match(p.name)
            if not m:
                continue
            start = m.group(1)
            end = m.group(2)
            candidates.append((end, start, p))
        if not candidates:
            return None
        # Latest end date first, then widest range (earliest start).
        candidates.sort(key=lambda x: (x[0], -int(x[1].replace("-", ""))), reverse=True)
        return candidates[0][2]

    def _from_local_latest_cache(self, symbol: str) -> Optional[Dict[str, Any]]:
        latest = self._read_latest_cache()
        from_latest = latest.get(symbol)
        if isinstance(from_latest, dict):
            normalized = self._normalize_quote(
                symbol,
                from_latest,
                "local_cache",
                is_realtime=False,
                is_stale=True,
                stale_reason="historical_cache",
            )
            if normalized is not None:
                normalized["bid"] = None
                normalized["ask"] = None
                return normalized

        best = self._choose_best_cache_file(symbol)
        if best is None:
            return None
        try:
            import pandas as pd

            df = pd.read_csv(best)
            for col in ("Adj Close", "adj_close", "Close", "close"):
                if col in df.columns:
                    series = df[col].dropna()
                    if not series.empty:
                        px = _safe_float(series.iloc[-1])
                        if px is not None:
                            return {
                                "symbol": symbol,
                                "last": px,
                                "bid": None,
                                "ask": None,
                                "source": "local_cache",
                                "timestamp": _now_iso(),
                                "is_realtime": False,
                                "is_stale": True,
                                "stale_reason": "historical_cache",
                            }
        except Exception:
            return None
        return None

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").upper()
        if not sym:
            return self._unavailable_quote(sym, source="invalid_symbol", stale_reason="invalid_symbol")

        # Robinhood MCP is always authoritative when it returns a usable quote.
        # Do not consult the local latest cache until the MCP quote path fails.
        realtime_quote = self._fetch_mcp_quote(sym)
        if realtime_quote is not None:
            latest = self._read_latest_cache()
            latest[sym] = realtime_quote
            self._write_latest_cache(latest)
            return realtime_quote

        if getattr(self.adapter, "fail_closed", False):
            return self._unavailable_quote(sym, source="mcp_unavailable", stale_reason="mcp_unavailable")

        for loader in (self._from_local_latest_cache, self._from_yfinance, self._from_alpha_vantage):
            quote = loader(sym)
            if quote is not None:
                latest = self._read_latest_cache()
                latest[sym] = quote
                self._write_latest_cache(latest)
                return quote

        return self._unavailable_quote(sym)
