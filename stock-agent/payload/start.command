#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec python3 stock_agent.py serve-commands
