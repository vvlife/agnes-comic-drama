#!/usr/bin/env python3
"""
Supabase Storage Layer for Agnes Comic Drama
Handles persistent file storage across Vercel serverless instances.

Tables:
  - project_files: Stores file metadata + base64 content for cross-instance persistence
  - pipeline_state: Stores pipeline step state for async job tracking

Usage:
    from supabase_storage import Storage
    storage = Storage()
    storage.save_file("project_id/characters/C1.png", binary_data)
    data = storage.load_file("project_id/characters/C1.png")
    storage.save_state("project_id", step=4, scene="S01", status="done")
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Optional

try:
    from supabase import create_client, Client
except ImportError:
    Client = None
    create_client = None


# SQL schema for setup (run in Supabase SQL Editor):
SCHEMA_SQL = """
-- Project files table (stores binary content as base64)
CREATE TABLE IF NOT EXISTS project_files (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  content_type TEXT DEFAULT 'application/octet-stream',
  content_base64 TEXT,
  size_bytes INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(project_id, file_path)
);

-- Pipeline state table (tracks async pipeline progress)
CREATE TABLE IF NOT EXISTS pipeline_state (
  id BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL UNIQUE,
  current_step INTEGER DEFAULT 0,
  step_status JSONB DEFAULT '{}',
  meta JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_state_project ON pipeline_state(project_id);

-- Enable RLS
ALTER TABLE project_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_state ENABLE ROW LEVEL SECURITY;

-- Allow service_role to bypass RLS (default behavior)
-- For anon access, add policies as needed
"""


class Storage:
    """Supabase-backed persistent storage for cross-instance file sharing."""

    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
    ):
        self.url = url or os.environ.get("SUPABASE_URL", "")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
        self.client: Optional[Client] = None
        if self.url and self.key and create_client:
            try:
                self.client = create_client(self.url, self.key)
            except Exception as e:
                print(f"[supabase] Connection failed: {e}")
                self.client = None
        else:
            print(f"[supabase] Not configured (url={'yes' if self.url else 'no'}, key={'yes' if self.key else 'no'})")

    @property
    def available(self) -> bool:
        return self.client is not None

    # ===== File Storage =====

    def save_file(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        """Save binary file to Supabase. Path format: project_id/characters/C1.png"""
        if not self.client:
            return False
        parts = path.split("/", 1)
        if len(parts) < 2:
            return False
        project_id, file_path = parts
        b64 = base64.b64encode(data).decode()
        try:
            self.client.table("project_files").upsert({
                "project_id": project_id,
                "file_path": file_path,
                "content_type": content_type,
                "content_base64": b64,
                "size_bytes": len(data),
                "updated_at": "now()",
            }).execute()
            return True
        except Exception as e:
            print(f"[supabase] save_file error: {e}")
            return False

    def load_file(self, path: str) -> Optional[bytes]:
        """Load binary file from Supabase. Path format: project_id/characters/C1.png"""
        if not self.client:
            return None
        parts = path.split("/", 1)
        if len(parts) < 2:
            return None
        project_id, file_path = parts
        try:
            res = self.client.table("project_files").select("content_base64").eq(
                "project_id", project_id
            ).eq("file_path", file_path).single().execute()
            if res.data and res.data.get("content_base64"):
                return base64.b64decode(res.data["content_base64"])
        except Exception as e:
            print(f"[supabase] load_file error: {e}")
        return None

    def list_files(self, project_id: str, prefix: str = "") -> list[dict]:
        """List files for a project, optionally filtered by path prefix."""
        if not self.client:
            return []
        try:
            q = self.client.table("project_files").select("file_path,size_bytes,updated_at").eq(
                "project_id", project_id
            )
            if prefix:
                q = q.like("file_path", f"{prefix}%")
            res = q.order("file_path").execute()
            return res.data or []
        except Exception as e:
            print(f"[supabase] list_files error: {e}")
            return []

    def delete_file(self, path: str) -> bool:
        """Delete a file from Supabase."""
        if not self.client:
            return False
        parts = path.split("/", 1)
        if len(parts) < 2:
            return False
        project_id, file_path = parts
        try:
            self.client.table("project_files").delete().eq(
                "project_id", project_id
            ).eq("file_path", file_path).execute()
            return True
        except Exception as e:
            print(f"[supabase] delete_file error: {e}")
            return False

    def delete_project(self, project_id: str) -> bool:
        """Delete all files for a project."""
        if not self.client:
            return False
        try:
            self.client.table("project_files").delete().eq("project_id", project_id).execute()
            self.client.table("pipeline_state").delete().eq("project_id", project_id).execute()
            return True
        except Exception as e:
            print(f"[supabase] delete_project error: {e}")
            return False

    # ===== Pipeline State =====

    def save_state(self, project_id: str, step: int = None, step_status: dict = None, meta: dict = None) -> bool:
        """Save or update pipeline state."""
        if not self.client:
            return False
        update = {"updated_at": "now()"}
        if step is not None:
            update["current_step"] = step
        if step_status is not None:
            update["step_status"] = json.dumps(step_status)
        if meta is not None:
            update["meta"] = json.dumps(meta)
        try:
            self.client.table("pipeline_state").upsert({
                "project_id": project_id,
                **update,
            }).execute()
            return True
        except Exception as e:
            print(f"[supabase] save_state error: {e}")
            return False

    def load_state(self, project_id: str) -> Optional[dict]:
        """Load pipeline state."""
        if not self.client:
            return None
        try:
            res = self.client.table("pipeline_state").select("*").eq(
                "project_id", project_id
            ).single().execute()
            if res.data:
                data = dict(res.data)
                if isinstance(data.get("step_status"), str):
                    data["step_status"] = json.loads(data["step_status"])
                if isinstance(data.get("meta"), str):
                    data["meta"] = json.loads(data["meta"])
                return data
        except Exception as e:
            print(f"[supabase] load_state error: {e}")
        return None

    # ===== Keepalive =====

    def keepalive(self) -> bool:
        """Ping Supabase to prevent database from going idle (free tier pauses after 1 week inactivity)."""
        if not self.client:
            return False
        try:
            self.client.table("pipeline_state").select("id").limit(1).execute()
            return True
        except Exception:
            return False


# Singleton
_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


if __name__ == "__main__":
    # Test
    s = Storage()
    if not s.available:
        print("❌ Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.")
        print("\nTo set up:")
        print("1. Create project at https://supabase.com/dashboard")
        print("2. Get URL + service_role key from Settings > API")
        print("3. Run schema SQL in SQL Editor:")
        print(SCHEMA_SQL)
        print("4. Set env vars:")
        print('   export SUPABASE_URL="https://xxx.supabase.co"')
        print('   export SUPABASE_SERVICE_KEY="eyJ..."')
    else:
        print("✅ Supabase connected!")
        s.keepalive()
        print("✅ Keepalive ping sent")
