#!/bin/bash

# Usage: ./scan-claude.sh /mnt/nas/claude-usage/usage.db

DB_PATH="${1:-$HOME/.claude/usage.db}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check the db path is reachable
DB_DIR="$(dirname "$DB_PATH")"
if ! timeout 2 ls "$DB_DIR" &>/dev/null; then
  echo "Error: cannot reach $DB_DIR — is the NAS mounted?"
  exit 1
fi

echo "Scanning ~/.claude/projects/ → $DB_PATH"

python3 - <<EOF
import sys
sys.path.insert(0, '$SCRIPT_DIR')
import scanner
scanner.scan(db_path='$DB_PATH')
EOF