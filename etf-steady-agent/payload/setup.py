#!/usr/bin/env python3
"""Official setup for the public Averra Agent package."""
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERIFY_URL = "https://averra-ai.haomingzhang2002.chatgpt.site/api/setup-key/verify"

def verify_key(key: str) -> bool:
    request = urllib.request.Request(VERIFY_URL, data=json.dumps({"mode": "agent", "key": key}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200 and json.load(response).get("ok") is True
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return False
        raise RuntimeError("Key verification is unavailable. Setup has not started.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("Key verification is unavailable. Setup has not started.") from error

def main() -> int:
    print("Averra Agent setup · an internet connection is required to verify your Agent key.")
    try:
        key = getpass.getpass("Agent setup key: ").strip()
        if not verify_key(key):
            print("Invalid Agent key. No setup changes were made.", file=sys.stderr)
            return 1
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    venv = ROOT / ".venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)
    result = subprocess.run([str(python), str(ROOT / "run_agent.py"), "--check-config"], cwd=ROOT)
    if result.returncode:
        return result.returncode
    print("Setup complete. The Agent is in READ_ONLY_VALIDATION mode. V3 policy is SMH 70%, QQQ 20%, SGOV 10%. Website allocations are drafts only; no live execution is available.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
