"""One sender; read-only mode only writes a local preview."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def deliver(text, priority='normal'):
    from config.unified import load_config, READ_ONLY_VALIDATION
    config, _ = load_config()
    if config.operation_mode == READ_ONLY_VALIDATION:
        path = ROOT / 'logs/telegram_preview.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as output:
            output.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                                     'text': text, 'priority': priority, 'preview_only': True}, ensure_ascii=False) + '\n')
        return {'delivered': False, 'preview_only': True}
    from qqq_mcp_adapter import MCPAdapter
    sender = MCPAdapter(workspace=str(ROOT))
    return sender._notify_telegram(text, priority)
