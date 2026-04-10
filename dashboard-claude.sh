#!/bin/bash

# Usage: ./dashboard-claude.sh /mnt/nas/claude-usage/usage.db

DB_PATH="${1:-$HOME/.claude/usage.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$DB_PATH" ]; then
  echo "Error: database not found at $DB_PATH"
  exit 1
fi

echo "Starting dashboard from $DB_PATH"

python3 - <<EOF
import sys, functools
from pathlib import Path
sys.path.insert(0, '$SCRIPT_DIR')
import dashboard

db = Path('$DB_PATH')
dashboard.DB_PATH = db
dashboard.get_dashboard_data = functools.partial(dashboard.get_dashboard_data, db_path=db)

dashboard.serve()
EOF