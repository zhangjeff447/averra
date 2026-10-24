#!/usr/bin/env python3
"""Install only the two Stock Agent services; preserve previous plist copies."""
import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from quant_screen import ROOT
from shadow_daily_scheduler import execution_lock
from stock_agent import load_env

LABELS = ('com.averra.stock.shadow-v11', 'com.averra.stock.commands')


def check():
    load_env(ROOT / '.env')
    required = ['ALPACA_API_KEY', 'ALPACA_API_SECRET', 'STOCK_TELEGRAM_CHAT_ID', 'OPENAI_API_KEY']
    present = {key: bool(os.environ.get(key)) for key in required}
    present['STOCK_TELEGRAM_BOT_TOKEN'] = bool(os.environ.get('STOCK_TELEGRAM_BOT_TOKEN') or os.environ.get('STOCK_TELEGRAM_TOKEN'))
    if not all(present.values()):
        raise ValueError('Missing environment variable names: ' + ', '.join(k for k, v in present.items() if not v))
    for label in LABELS:
        spec = plistlib.loads((ROOT / 'launchd' / (label + '.plist')).read_bytes())
        if spec['Label'] != label or spec['WorkingDirectory'] != str(ROOT):
            raise ValueError('PLIST_PROJECT_MISMATCH')
        if not Path(spec['ProgramArguments'][0]).is_file():
            raise ValueError('RUNTIME_MISSING')
    return {'environment_present': present, 'project_path': str(ROOT)}


def install():
    status = check()
    launch_dir = Path.home() / 'Library' / 'LaunchAgents'
    qqq = launch_dir / 'com.qqqsmh.agent.plist'
    original = hashlib.sha256(qqq.read_bytes()).hexdigest() if qqq.exists() else None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backups = ROOT / 'data' / 'launchd_backups' / stamp
    backups.mkdir(parents=True, exist_ok=True)
    with execution_lock() as acquired:
        if not acquired:
            raise RuntimeError('ACTIVE_SHADOW_RUN_INSTALL_BLOCKED')
        for label in LABELS:
            destination = launch_dir / (label + '.plist')
            source = ROOT / 'launchd' / (label + '.plist')
            if destination.exists():
                shutil.copy2(destination, backups / destination.name)
            service = f'gui/{os.getuid()}/{label}'
            loaded = subprocess.run(['launchctl', 'print', service], capture_output=True).returncode == 0
            if loaded:
                subprocess.run(['launchctl', 'bootout', service], check=True)
            shutil.copy2(source, destination)
            os.chmod(destination, 0o644)
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(destination)], check=True)
    current = hashlib.sha256(qqq.read_bytes()).hexdigest() if qqq.exists() else None
    if current != original:
        raise RuntimeError('QQQ_PLIST_CHANGED_EXTERNALLY')
    return {**status, 'installed': list(LABELS), 'qqq_plist_unchanged': True,
            'backups': str(backups), 'ai_calls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install() if args.install else check(), indent=2))
