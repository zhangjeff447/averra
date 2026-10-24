import errno
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    from tools import windows_lock as fcntl
import time as time_module

MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 10

SENSITIVE_KEYWORDS = (
    "token",
    "secret",
    "password",
    "cookie",
    "oauth",
    "authorization",
    "api_key",
    "auth",
)

ET_ZONE = ZoneInfo("America/New_York")


@dataclass
class LockHandle:
    file_obj: Any
    path: Path


class ProductionConfigError(RuntimeError):
    pass


def _looks_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(word in k for word in SENSITIVE_KEYWORDS)


def _redact_text(value: str) -> str:
    text = value
    text = re.sub(r"(\b\d{4})\d{4,12}(\b)", r"\1****\2", text)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"(bot\d+:)[A-Za-z0-9_-]+", r"\1***", text)
    return text


def redact_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for k, v in payload.items():
            if _looks_sensitive_key(str(k)):
                out[k] = "***"
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(payload, list):
        return [redact_payload(x) for x in payload]
    if isinstance(payload, str):
        return _redact_text(payload)
    return payload


def _rotate_file(path: Path, max_bytes: int = MAX_LOG_BYTES, backup_count: int = LOG_BACKUP_COUNT) -> None:
    if not path.exists():
        return
    if path.stat().st_size <= max_bytes:
        return

    oldest = path.with_name(f"{path.name}.{backup_count}")
    if oldest.exists():
        oldest.unlink()

    for i in range(backup_count - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        dst = path.with_name(f"{path.name}.{i + 1}")
        if src.exists():
            src.replace(dst)

    path.replace(path.with_name(f"{path.name}.1"))


def _append_line(path: Path, line: str) -> None:
    """Append safely on macOS File Provider volumes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    retryable = {errno.EAGAIN, errno.EDEADLK, errno.EBUSY}
    for attempt, delay in enumerate((0.0, 0.05, 0.15, 0.30)):
        if delay:
            time_module.sleep(delay)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            return
        except OSError as exc:
            if exc.errno not in retryable or attempt == 3:
                raise


def append_text_log(path: Path, message: str) -> None:
    _rotate_file(path)
    _append_line(path, _redact_text(message) + "\n")


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    _rotate_file(path)
    clean_record = redact_payload(record)
    _append_line(path, json.dumps(clean_record, ensure_ascii=False, default=str) + "\n")


def ensure_log_files(log_dir: Path) -> Dict[str, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "runtime": log_dir / "runtime.log",
        "execution": log_dir / "execution.jsonl",
        "decisions": log_dir / "decisions.jsonl",
        "errors": log_dir / "errors.log",
        "mcp_error": log_dir / "mcp_connection_error.log",
        "healthcheck": log_dir / "healthcheck.log",
    }
    for p in paths.values():
        p.touch(exist_ok=True)
    return paths


def acquire_lock(lock_path: Path) -> Optional[LockHandle]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = lock_path.open("a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate(0)
    f.write(str(os.getpid()))
    f.flush()
    return LockHandle(file_obj=f, path=lock_path)


def release_lock(lock: Optional[LockHandle]) -> None:
    if lock is None:
        return
    try:
        fcntl.flock(lock.file_obj.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock.file_obj.close()
    except Exception:
        pass


def load_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data, None
        return None, "state_not_dict"
    except Exception as exc:
        return None, str(exc)


def atomic_write_json(path: Path, payload: Dict[str, Any], backup_path: Optional[Path] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_name).replace(path)
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(text, encoding="utf-8")
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        value = v.strip().strip("\"").strip("'")
        if key:
            out[key] = value
    return out


def observed_date(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    d += timedelta(days=7 * (nth - 1))
    return d


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    easter = easter_date(year)
    good_friday = easter - timedelta(days=2)

    holidays = {
        observed_date(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        good_friday,
        last_weekday(year, 5, 0),
        observed_date(date(year, 6, 19)),
        observed_date(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_date(date(year, 12, 25)),
    }
    return holidays


def is_trading_day(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    return now_et.date() not in us_market_holidays(now_et.year)


def is_regular_market_hours(now_et: datetime) -> bool:
    t = now_et.timetz()
    return time(9, 30) <= time(t.hour, t.minute, t.second) <= time(16, 0)


def current_slot(now_et: datetime, slots: List[time], window_minutes: int = 25) -> Optional[str]:
    for slot in slots:
        if now_et.hour == slot.hour and slot.minute <= now_et.minute < slot.minute + window_minutes:
            return f"{slot.hour:02d}:{slot.minute:02d}"
    return None


def scheduler_installed(label: str) -> bool:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    return (launch_agents / f"{label}.plist").exists()


def is_telegram_configured(env: Dict[str, str]) -> bool:
    enabled = env.get("TELEGRAM_ENABLED", "true").strip().lower() == "true"
    if not enabled:
        return False
    return bool(env.get("TELEGRAM_BOT_TOKEN")) and bool(env.get("TELEGRAM_CHAT_ID"))


def should_send_dedup_alert(state: Dict[str, Any], error_key: str, now_utc: datetime, cooldown_hours: int = 6) -> bool:
    alerts = state.setdefault("alert_dedup", {})
    last_sent = alerts.get(error_key)
    if not last_sent:
        alerts[error_key] = now_utc.isoformat()
        return True
    try:
        last = datetime.fromisoformat(str(last_sent).replace("Z", "+00:00"))
    except Exception:
        alerts[error_key] = now_utc.isoformat()
        return True
    if now_utc - last >= timedelta(hours=cooldown_hours):
        alerts[error_key] = now_utc.isoformat()
        return True
    return False


def clear_dedup_alert(state: Dict[str, Any], error_key: str) -> bool:
    alerts = state.setdefault("alert_dedup", {})
    if error_key in alerts:
        del alerts[error_key]
        return True
    return False
