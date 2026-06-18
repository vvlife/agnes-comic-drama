#!/usr/bin/env python3
"""
Cron job to ping Supabase periodically, preventing the free-tier database
from going idle (Supabase pauses databases after 1 week of inactivity).

Run via cron every 6 hours:
  0 */6 * * * /usr/bin/env python3 /path/to/agnes-comic-drama/scripts/supabase_keepalive.py

Or via QClaw cron:
  Schedule: every 6 hours
  Command: python3 /Users/vivy/.qclaw/workspace/skills/agnes-comic-drama/scripts/supabase_keepalive.py
"""

import os
import sys
import time
from pathlib import Path

# Add parent dirs to path so we can import supabase_storage
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from supabase_storage import Storage


def main():
    storage = Storage()
    if not storage.available:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Supabase not configured")
        print("Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
        return 1

    ok = storage.keepalive()
    if ok:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ✅ Supabase keepalive OK")
        return 0
    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ Supabase keepalive failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
