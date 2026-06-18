#!/usr/bin/env python3
"""
Agnes Comic Drama Generator - Vercel Serverless API
Adapted from Flask web/server.py for Vercel Python runtime.

Key adaptations:
  - OUTPUT_BASE = /tmp/agnes-output  (ephemeral, fine for single-session demo)
  - AGNES_API_KEY from Vercel env var
  - All generation tasks run SYNCHRONOUSLY (no threading in serverless)
  - Config sent via request body instead of persisted to disk
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
import time
import uuid

import requests as http_requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

# ============================================================
# Path setup — api/ directory contains agnes_client.py & run.py
# ============================================================
API_DIR = pathlib.Path(__file__).parent
sys.path.insert(0, str(API_DIR))

import agnes_client
import run as generator

try:
    from supabase_storage import get_storage
except ImportError:
    get_storage = None

# ============================================================
# Flask app
# ============================================================
app = Flask(__name__)
CORS(app)

# ============================================================
# Vercel-adapted config
# ============================================================
OUTPUT_BASE = pathlib.Path("/tmp/agnes-output")
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

# In-memory job store (lives only for the duration of the serverless instance)
jobs: dict[str, dict] = {}


# ============================================================
# Helpers
# ============================================================

def get_api_key(from_body: dict | None = None) -> str:
    """Resolve AGNES_API_KEY from: request body > env var > header."""
    if from_body and from_body.get("AGNES_API_KEY"):
        return from_body["AGNES_API_KEY"]
    key = os.environ.get("AGNES_API_KEY", "")
    if key:
        return key
    auth = request.headers.get("X-API-Key", "")
    if auth:
        return auth
    return ""


def get_client_and_rl(api_key: str | None = None):
    key = api_key or get_api_key()
    if not key:
        raise RuntimeError("AGNES_API_KEY not configured. Set it in Vercel env vars or pass in request body.")
    client = agnes_client.AgnesClient(api_key=key)
    rl = generator.RateLimiter(rpm=18)
    return client, rl


def validate_project(project_id: str):
    # First check local filesystem
    p = OUTPUT_BASE / project_id
    if p.exists():
        return p
    # Then check Supabase
    try:
        storage = get_storage()
        if storage:
            # Check if project exists in Supabase
            state = storage.get_pipeline_state(project_id)
            files = storage.list_files(project_id)
            if state or files:
                # Create local directory as cache
                p.mkdir(parents=True, exist_ok=True)
                # Restore from Supabase
                storage.restore_project(project_id, p)
                return p
    except Exception:
        pass
    return None


def load_script(project_dir: pathlib.Path) -> dict | None:
    sp = project_dir / "script.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except Exception:
            pass
    return None


def save_script(project_dir: pathlib.Path, script: dict):
    sp = project_dir / "script.json"
    sp.write_text(json.dumps(script, ensure_ascii=False, indent=2))


def load_char_manifest(project_dir: pathlib.Path) -> dict:
    mp = project_dir / "characters" / "manifest.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass
    return {}


def load_sb_manifest(project_dir: pathlib.Path) -> dict:
    mp = project_dir / "storyboard" / "manifest.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass
    return {}


def load_vid_manifest(project_dir: pathlib.Path) -> dict:
    vid_dir = project_dir / "videos"
    manifest = {}
    if vid_dir.exists():
        for f in vid_dir.glob("*.mp4"):
            manifest[f.stem] = str(f)
    return manifest


def make_job(project_id: str, task_type: str) -> str:
    job_id = f"{project_id}_{task_type}_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": task_type,
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }
    return job_id


# ============================================================
# Supabase persistence helpers
# ============================================================

def sync_project_to_supabase(project_id: str):
    """Upload all project files from /tmp to Supabase for cross-instance persistence."""
    if not get_storage:
        return
    storage = get_storage()
    if not storage.available:
        return
    p = OUTPUT_BASE / project_id
    if not p.exists():
        return
    for fpath in p.rglob("*"):
        if fpath.is_file() and fpath.stat().st_size < 5_000_000:  # 5MB limit per file
            rel = fpath.relative_to(p)
            try:
                data = fpath.read_bytes()
                ct = "application/json" if fpath.suffix == ".json" else "image/png" if fpath.suffix == ".png" else "video/mp4" if fpath.suffix == ".mp4" else "application/octet-stream"
                storage.save_file(f"{project_id}/{rel}", data, ct)
            except Exception as e:
                print(f"[supabase] sync save error {rel}: {e}")


def restore_project_from_supabase(project_id: str):
    """Download project files from Supabase to /tmp (if not already present)."""
    if not get_storage:
        return
    storage = get_storage()
    if not storage.available:
        return
    p = OUTPUT_BASE / project_id
    p.mkdir(parents=True, exist_ok=True)
    files = storage.list_files(project_id)
    for f in files:
        file_path = f.get("file_path", "")
        local_path = p / file_path
        if local_path.exists():
            continue  # Already present
        data = storage.load_file(f"{project_id}/{file_path}")
        if data:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)


def send_completion_email(to_email: str, project_title: str, project_id: str) -> bool:
    """Send a completion email with download link via Resend API.

    Uses env vars:
      RESEND_API_KEY  — Resend API key (already configured)
      EMAIL_FROM      — Verified sender address (e.g. "Agnes漫剧 <noreply@yourdomain.us.kg>")
    Falls back to generic SMTP if Resend not available:
      SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM
    """
    resend_key = os.environ.get("RESEND_API_KEY", "")
    email_from = os.environ.get("EMAIL_FROM", "")

    if not email_from:
        return False

    base_url = os.environ.get("VERCEL_URL", "")
    if base_url and not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    if not base_url:
        base_url = "https://agnes-comic-drama.vercel.app"

    video_url = f"{base_url}/api/project-files/{project_id}/final_with_audio.mp4"
    page_url = base_url

    html_body = f"""\
<div style="font-family: -apple-system, 'Segoe UI', sans-serif; max-width: 560px; margin: 0 auto; background: #13131a; color: #e8e8f0; border-radius: 16px; overflow: hidden;">
  <div style="background: linear-gradient(135deg, #6c5ce7, #a29bfe); padding: 28px 24px; text-align: center;">
    <h1 style="margin: 0; font-size: 22px; color: white;">🎬 漫剧生成完成</h1>
  </div>
  <div style="padding: 24px;">
    <p style="font-size: 15px; line-height: 1.6; color: #e8e8f0;">
      你好！你的 AI 漫剧 <strong style="color: #a29bfe;">「{project_title}」</strong> 已生成完毕。
    </p>
    <div style="text-align: center; margin: 24px 0;">
      <a href="{video_url}" style="display: inline-block; background: #6c5ce7; color: white; padding: 14px 32px; border-radius: 10px; text-decoration: none; font-weight: 700; font-size: 15px;">
        ⬇️ 下载视频
      </a>
    </div>
    <p style="font-size: 13px; color: #8888a0; line-height: 1.6;">
      如果按钮无法下载，请复制链接到浏览器：<br>
      <span style="word-break: break-all; color: #a29bfe;">{video_url}</span>
    </p>
    <hr style="border: none; border-top: 1px solid #2a2a3a; margin: 20px 0;">
    <p style="font-size: 11px; color: #555570; text-align: center;">
      由 <a href="{page_url}" style="color: #a29bfe;">Agnes 漫剧生成器</a> 自动发送<br>
      链接有效期取决于服务器缓存，请尽快下载
    </p>
  </div>
</div>
"""

    # Method 1: Resend HTTP API (preferred for serverless)
    if resend_key and not email_from.endswith("@resend.dev"):
        try:
            resp = http_requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": email_from,
                    "to": [to_email],
                    "subject": f"🎬 漫剧已生成：{project_title}",
                    "html": html_body,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return True
            print(f"Resend API error: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Resend send failed: {e}")

    # Method 2: Generic SMTP fallback
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if smtp_user and smtp_pass:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            smtp_host = os.environ.get("SMTP_HOST", "mail.smtp2go.com")
            smtp_port = int(os.environ.get("SMTP_PORT", "2525"))

            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"🎬 漫剧已生成：{project_title}"
            msg["From"] = email_from
            msg["To"] = to_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.sendmail(email_from, [to_email], msg.as_string())
            return True
        except Exception as e:
            print(f"SMTP send failed: {e}")

    return False


# ============================================================
# Routes — Static info
# ============================================================

@app.route("/api/models", methods=["GET"])
def api_models():
    return jsonify({
        "text": ["agnes-2.0-flash", "agnes-1.5-flash"],
        "image": ["agnes-image-2.1-flash"],
        "video": ["agnes-video-v2.0"],
    })


@app.route("/api/styles", methods=["GET"])
def api_styles():
    return jsonify({
        "styles": ["三渲二国风", "水墨", "赛博朋克", "日系动漫"],
        "genres": ["仙侠", "宫斗", "江湖", "都市"],
    })


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok", "output_base": str(OUTPUT_BASE)})


@app.route("/", methods=["GET"])
def serve_index():
    """Serve the frontend SPA from the api/ directory (bundled with the function)."""
    html_path = API_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "Agnes Comic Drama API is running. Frontend not found.", 200


# ============================================================
# Routes — Config (Vercel: env var based)
# ============================================================

@app.route("/api/config", methods=["GET"])
def api_get_config():
    key = os.environ.get("AGNES_API_KEY", "")
    masked = ""
    if key:
        masked = key[:4] + "*" * (len(key) - 8) + key[-4:] if len(key) > 8 else "****"

    email_from = os.environ.get("EMAIL_FROM", "")
    email_masked = ""
    resend_key = os.environ.get("RESEND_API_KEY", "")
    has_email = False

    if email_from and resend_key and not email_from.endswith("@resend.dev"):
        # Extract email from "Name <email>" format
        import re as _re
        m = _re.search(r'<([^>]+)>', email_from)
        raw = m.group(1) if m else email_from
        parts = raw.split("@")
        if len(parts) == 2:
            email_masked = parts[0][:2] + "***@" + parts[1]
        else:
            email_masked = "***"
        has_email = True
    elif email_from:
        email_masked = email_from

    return jsonify({
        "AGNES_API_KEY": masked,
        "has_key": bool(key),
        "EMAIL_FROM": email_masked,
        "has_email": has_email,
        "platform": "vercel",
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    """On Vercel, config cannot be persisted to disk. Guide user to env vars."""
    body = request.get_json(force=True, silent=True) or {}
    key = body.get("AGNES_API_KEY", "").strip()

    # Check if running on Vercel (read-only filesystem)
    is_vercel = os.environ.get("VERCEL", "") == "1"

    if is_vercel:
        existing = os.environ.get("AGNES_API_KEY", "")
        if existing:
            return jsonify({
                "ok": True,
                "message": "已使用 Vercel 环境变量中预设的 API Key，无需额外配置。"
            })
        return jsonify({
            "ok": False,
            "message": "Vercel 环境下无法持久化配置。请在 Vercel Dashboard → Settings → Environment Variables 中添加 AGNES_API_KEY。"
        })

    # Local mode: try to save to config.json
    try:
        config_path = API_DIR.parent / "web" / "config.json"
        cfg = {}
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
        if key:
            cfg["AGNES_API_KEY"] = key
        config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        return jsonify({"ok": True, "message": "已保存"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/test", methods=["POST"])
def api_test_config():
    body = request.get_json(force=True, silent=True) or {}
    api_key = get_api_key(from_body=body)
    if not api_key:
        return jsonify({"ok": False, "error": "未配置 API KEY"})
    try:
        import ssl
        import certifi
        import urllib.request
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            "https://apihub.agnes-ai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            if resp.status == 200:
                return jsonify({"ok": True, "message": "API KEY 有效"})
            else:
                return jsonify({"ok": False, "error": f"HTTP {resp.status}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/test-email", methods=["POST"])
def api_test_email():
    """Send a test email to verify Gmail SMTP configuration."""
    body = request.get_json(force=True, silent=True) or {}
    test_to = body.get("to", "").strip()
    if not test_to:
        return jsonify({"ok": False, "error": "请提供测试收件邮箱地址"})

    ok = send_completion_email(test_to, "测试邮件 - Agnes 漫剧生成器", "test_project")
    if ok:
        return jsonify({"ok": True, "message": f"测试邮件已发送到 {test_to}"})
    return jsonify({"ok": False, "error": "发送失败，请检查 GMAIL_ADDRESS 和 GMAIL_APP_PASSWORD 环境变量"})


# ============================================================
# Routes — Projects
# ============================================================

@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    projects = []
    
    # Get projects from Supabase
    supabase_projects = set()
    try:
        storage = get_storage()
        if storage:
            supabase_projects = storage.list_projects()
    except Exception:
        pass
    
    # Get projects from local filesystem
    local_projects = set()
    if OUTPUT_BASE.exists():
        for d in sorted(OUTPUT_BASE.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.is_dir():
                local_projects.add(d.name)
    
    # Merge both sets
    all_project_ids = supabase_projects.union(local_projects)
    
    for pid in all_project_ids:
        p = OUTPUT_BASE / pid
        # Ensure local copy exists
        if pid in supabase_projects and pid not in local_projects:
            try:
                storage = get_storage()
                if storage:
                    p.mkdir(parents=True, exist_ok=True)
                    storage.restore_project(pid, p)
            except Exception:
                pass
        
        script = load_script(p)
        projects.append({
            "id": pid,
            "title": script.get("title", "") if script else pid,
            "has_script": (p / "script.json").exists(),
            "has_characters": (p / "characters" / "manifest.json").exists(),
            "has_storyboard": (p / "storyboard" / "manifest.json").exists(),
            "has_videos": (p / "videos").exists() and any((p / "videos").glob("*.mp4")),
            "has_final": (p / "final.mp4").exists(),
            "modified": time.time(),
        })
    
    return jsonify({"projects": projects})


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    body = request.get_json(force=True, silent=True) or {}
    theme = body.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "请输入主题"}), 400

    style = body.get("style", "三渲二国风")
    genre = body.get("genre", "仙侠")
    duration = int(body.get("duration", 15))
    scene_duration = int(body.get("scene_duration", 5))
    n_scenes = int(body.get("n_scenes", 0)) or (duration // scene_duration)
    enable_tts = body.get("enable_tts", True)
    enable_sfx = body.get("enable_sfx", True)
    email = body.get("email", "").strip()

    slug = re.sub(r"[^a-zA-Z0-9]", "-", theme)[:20].strip("-") or "project"
    project_id = f"{slug}_{int(time.time())}"
    project_dir = OUTPUT_BASE / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": project_id,
        "theme": theme,
        "style": style,
        "genre": genre,
        "duration": duration,
        "scene_duration": scene_duration,
        "enable_tts": enable_tts,
        "enable_sfx": enable_sfx,
        "n_scenes": n_scenes,
        "email": email,
        "created_at": time.time(),
    }
    (project_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    # Sync to Supabase
    try:
        storage = get_storage()
        if storage:
            storage.save_state(project_id, step=0, meta=meta)
    except Exception:
        pass

    return jsonify({"id": project_id, "meta": meta}), 201


@app.route("/api/projects/<project_id>", methods=["GET"])
def api_get_project(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    meta_path = p / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass

    script = load_script(p)
    char_manifest = load_char_manifest(p)
    sb_manifest = load_sb_manifest(p)
    vid_manifest = load_vid_manifest(p)

    return jsonify({
        "id": project_id,
        "meta": meta,
        "has_script": script is not None,
        "has_characters": bool(char_manifest),
        "has_storyboard": bool(sb_manifest),
        "has_videos": bool(vid_manifest),
        "has_final": (p / "final.mp4").exists(),
        "script": script,
        "character_count": len(char_manifest),
        "scene_count": len(script.get("scenes", [])) if script else 0,
    })


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def api_delete_project(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    shutil.rmtree(p)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/meta", methods=["PUT"])
def api_update_meta(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    meta_path = p / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    for k, v in body.items():
        meta[k] = v
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


# ============================================================
# Routes — Script (SYNCHRONOUS on Vercel)
# ============================================================

@app.route("/api/projects/<project_id>/script", methods=["POST"])
def api_generate_script(project_id: str):
    """Generate script synchronously (no background thread on Vercel)."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    meta_path = p / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass

    theme = meta.get("theme", "未命名")
    style = meta.get("style", "三渲二国风")
    genre = meta.get("genre", "仙侠")
    n_scenes = meta.get("n_scenes", 3)
    scene_duration = meta.get("scene_duration", 5)

    cp = generator.Checkpoint(p)
    cp.data.pop("script", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "script")

    # Run synchronously
    jobs[job_id]["status"] = "running"
    try:
        client, rl = get_client_and_rl()
        cp = generator.Checkpoint(p)
        script = generator.generate_script(
            client, theme, style, genre,
            n_scenes, scene_duration, rl,
            p / "script.json", cp,
        )
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = script
        jobs[job_id]["logs"].append("Script generation completed")
    except Exception as e:
        import traceback
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")
        jobs[job_id]["logs"].append(f"Traceback: {traceback.format_exc()}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


@app.route("/api/projects/<project_id>/script", methods=["GET"])
def api_get_script(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本尚未生成"}), 404
    return jsonify(script)


@app.route("/api/projects/<project_id>/script", methods=["PUT"])
def api_update_script(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "无效数据"}), 400
    save_script(p, body)
    return jsonify({"ok": True})


# ============================================================
# Routes — Characters (SYNCHRONOUS)
# ============================================================

@app.route("/api/projects/<project_id>/characters", methods=["GET"])
def api_get_characters(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    char_manifest = load_char_manifest(p)
    char_dir = p / "characters"

    characters = []
    if script:
        for char in script.get("characters", []):
            cid = char["id"]
            images = char_manifest.get(cid, {}).get("images", [])
            image_urls = []
            for img_path in images:
                img = pathlib.Path(img_path)
                if img.exists():
                    rel = img.relative_to(p)
                    image_urls.append(f"/api/project-files/{project_id}/{rel}")
            characters.append({
                **char,
                "images": image_urls,
                "has_images": len(image_urls) > 0,
            })

    return jsonify({"characters": characters})


@app.route("/api/projects/<project_id>/characters/<cid>", methods=["PUT"])
def api_update_character(project_id: str, cid: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    for char in script.get("characters", []):
        if char["id"] == cid:
            for k, v in body.items():
                if k in ("name", "visual", "personality", "age"):
                    char[k] = v
            break
    save_script(p, script)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/characters/<cid>/generate", methods=["POST"])
def api_generate_character(project_id: str, cid: str):
    """Generate character images synchronously."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    char_info = None
    for char in script.get("characters", []):
        if char["id"] == cid:
            char_info = char
            break
    if not char_info:
        return jsonify({"error": "角色不存在"}), 404

    char_dir = p / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    for old in char_dir.glob(f"{cid}_*.png"):
        old.unlink(missing_ok=True)
    char_manifest = load_char_manifest(p)
    char_manifest.pop(cid, None)
    (char_dir / "manifest.json").write_text(json.dumps(char_manifest, ensure_ascii=False, indent=2))

    cp = generator.Checkpoint(p)
    cp.data.pop("characters", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "character")
    jobs[job_id]["status"] = "running"

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        style = meta.get("style", "三渲二国风")
        manifest = generator.generate_characters(
            client, script, style,
            char_dir, generator.Checkpoint(p), rl,
        )
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = manifest
        jobs[job_id]["logs"].append(f"Character {char_info['name']} generation completed")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


# ============================================================
# Routes — Storyboard (SYNCHRONOUS)
# ============================================================

@app.route("/api/projects/<project_id>/storyboard/manifest", methods=["GET"])
def api_get_storyboard_manifest(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    return jsonify(load_sb_manifest(p))


@app.route("/api/projects/<project_id>/storyboard/generate-all", methods=["POST"])
def api_generate_all_storyboard(project_id: str):
    """Generate all storyboard frames synchronously."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    cp = generator.Checkpoint(p)
    cp.data.pop("storyboard", None)
    keys_to_remove = [k for k in cp.data if k.startswith("storyboard.")]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "storyboard")
    jobs[job_id]["status"] = "running"

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        style = meta.get("style", "三渲二国风")
        char_manifest = load_char_manifest(p)
        sb_dir = p / "storyboard"

        manifest = generator.generate_storyboard(
            client, script, style, char_manifest,
            sb_dir, generator.Checkpoint(p), rl,
        )
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = manifest
        jobs[job_id]["logs"].append("Storyboard generation completed")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


@app.route("/api/projects/<project_id>/storyboard/<sid>/generate", methods=["POST"])
def api_generate_storyboard_scene(project_id: str, sid: str):
    """Generate single storyboard frame synchronously."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    scene = None
    for s in script.get("scenes", []):
        if s["id"] == sid:
            scene = s
            break
    if not scene:
        return jsonify({"error": "场景不存在"}), 404

    sb_dir = p / "storyboard"
    old_frame = sb_dir / f"{sid}.png"
    old_frame.unlink(missing_ok=True)
    sb_manifest = load_sb_manifest(p)
    sb_manifest.pop(sid, None)
    sb_dir.mkdir(parents=True, exist_ok=True)
    (sb_dir / "manifest.json").write_text(json.dumps(sb_manifest, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "storyboard")
    jobs[job_id]["status"] = "running"

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        style = meta.get("style", "三渲二国风")
        style_info = generator.STYLE_PRESETS.get(style, generator.STYLE_PRESETS["三渲二国风"])
        char_manifest = load_char_manifest(p)

        prompt = f"{style_info['prefix']}，{scene['location']}，{scene['time']}。{scene['action']}。{scene['camera']}。{scene['mood']}氛围。{style_info['lighting']}。{style_info['palette']}。"

        ref_images = []
        for cid in scene.get("characters", []):
            if cid in char_manifest:
                imgs = char_manifest[cid].get("images", [])
                for img in imgs:
                    if "full" in img and pathlib.Path(img).exists():
                        ref_images.append(img)
                        break
                if len(ref_images) >= 4:
                    break

        rl.wait()
        img_url = client.generate_image(
            prompt=prompt,
            size=generator.IMAGE_SIZES["landscape"],
            reference_images=ref_images if ref_images else None,
            response_format="url",
        )

        sb_dir.mkdir(parents=True, exist_ok=True)
        r = http_requests.get(img_url, stream=True, timeout=120)
        r.raise_for_status()
        with open(old_frame, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)

        sb_manifest[sid] = {"path": str(old_frame), "url": img_url, "prompt": prompt}
        (sb_dir / "manifest.json").write_text(json.dumps(sb_manifest, ensure_ascii=False, indent=2))

        jobs[job_id]["status"] = "done"
        jobs[job_id]["logs"].append(f"Scene {sid} storyboard generated")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


# ============================================================
# Routes — Videos (SYNCHRONOUS — may hit timeout for long runs)
# ============================================================

@app.route("/api/projects/<project_id>/videos/generate-all", methods=["POST"])
def api_generate_all_videos(project_id: str):
    """Generate all videos synchronously. WARNING: may exceed serverless timeout."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    cp = generator.Checkpoint(p)
    cp.data.pop("videos", None)
    keys_to_remove = [k for k in cp.data if k.startswith("videos.")]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "videos")
    jobs[job_id]["status"] = "running"

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        scene_duration = meta.get("scene_duration", 5)
        style = meta.get("style", "三渲二国风")
        sb_manifest = load_sb_manifest(p)
        vid_dir = p / "videos"

        manifest = generator.generate_videos(
            client, script, sb_manifest,
            vid_dir, generator.Checkpoint(p), rl,
            scene_duration=scene_duration,
            style=style,
        )
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = manifest
        jobs[job_id]["logs"].append("All videos generated")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


@app.route("/api/projects/<project_id>/videos/<sid>/generate", methods=["POST"])
def api_generate_video_scene(project_id: str, sid: str):
    """Generate single scene video synchronously."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    scene = None
    for s in script.get("scenes", []):
        if s["id"] == sid:
            scene = s
            break
    if not scene:
        return jsonify({"error": "场景不存在"}), 404

    sb_manifest = load_sb_manifest(p)
    frame_info = sb_manifest.get(sid, {})
    frame_url = frame_info.get("url", "")
    frame_path = frame_info.get("path", "")
    if not frame_url and not (frame_path and pathlib.Path(frame_path).exists()):
        return jsonify({"error": "该场景尚无分镜帧，请先生成分镜帧"}), 400

    vid_dir = p / "videos"
    old_vid = vid_dir / f"{sid}.mp4"
    old_vid.unlink(missing_ok=True)

    job_id = make_job(project_id, "video")
    jobs[job_id]["status"] = "running"

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        scene_duration = meta.get("scene_duration", 5)

        image_input = frame_url if frame_url else frame_path
        video_prompt = f"{scene['action']}，{scene['camera']}，{scene['mood']}氛围，cinematic quality，smooth motion"

        num_frames = generator.SCENE_DURATION_MAP.get(scene_duration, 121)
        vid_dir.mkdir(parents=True, exist_ok=True)

        client.generate_video_full(
            prompt=video_prompt,
            out_path=old_vid,
            image=image_input,
            height=768,
            width=1344,
            num_frames=num_frames,
            frame_rate=24,
        )

        cp = generator.Checkpoint(p)
        cp.data[f"videos.{sid}"] = "done"
        cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

        jobs[job_id]["status"] = "done"
        jobs[job_id]["logs"].append(f"Scene {sid} video generated")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


# ============================================================
# Routes — Render & Final
# ============================================================

@app.route("/api/projects/<project_id>/render", methods=["POST"])
def api_render(project_id: str):
    """Render final video synchronously."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    for f in ["final.mp4", "final_with_sub.mp4", "final_with_audio.mp4", "final_lipsync.mp4"]:
        (p / f).unlink(missing_ok=True)

    cp = generator.Checkpoint(p)
    cp.data.pop("edit", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "render")
    jobs[job_id]["status"] = "running"

    try:
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        scene_duration = meta.get("scene_duration", 5)
        vid_manifest = load_vid_manifest(p)

        result = generator.edit_final(
            p, script, vid_manifest,
            generator.Checkpoint(p),
            scene_duration=scene_duration,
        )

        if result:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = str(result)
            jobs[job_id]["logs"].append("Final video rendered")
        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["logs"].append("Render failed — no output")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


@app.route("/api/projects/<project_id>/scenes/order", methods=["PUT"])
def api_update_scene_order(project_id: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    body = request.get_json(force=True, silent=True) or {}
    order = body.get("order", [])
    if not order:
        return jsonify({"error": "无效顺序"}), 400
    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404
    scene_map = {s["id"]: s for s in script.get("scenes", [])}
    new_scenes = []
    for sid in order:
        if sid in scene_map:
            new_scenes.append(scene_map[sid])
    for s in script.get("scenes", []):
        if s["id"] not in order:
            new_scenes.append(s)
    script["scenes"] = new_scenes
    save_script(p, script)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/scenes/<sid>", methods=["DELETE"])
def api_delete_scene(project_id: str, sid: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404
    script["scenes"] = [s for s in script.get("scenes", []) if s["id"] != sid]
    save_script(p, script)
    (p / "storyboard" / f"{sid}.png").unlink(missing_ok=True)
    (p / "videos" / f"{sid}.mp4").unlink(missing_ok=True)
    return jsonify({"ok": True})


# ============================================================
# Routes — Auto mode (SYNCHRONOUS — single request)
# ============================================================

@app.route("/api/projects/<project_id>/auto", methods=["POST"])
def api_auto_run(project_id: str):
    """Full auto pipeline — runs synchronously. May timeout for large projects."""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    cp = generator.Checkpoint(p)
    for key in ["script", "characters", "storyboard", "videos", "edit"]:
        cp.data.pop(key, None)
    keys_to_remove = [k for k in cp.data if k.startswith(("storyboard.", "videos."))]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "auto")
    jobs[job_id]["status"] = "running"

    try:
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        theme = meta.get("theme", "未命名")
        style = meta.get("style", "三渲二国风")
        genre = meta.get("genre", "仙侠")
        n_scenes = meta.get("n_scenes", 3)
        scene_duration = meta.get("scene_duration", 5)

        client, rl = get_client_and_rl()
        cp = generator.Checkpoint(p)

        # Step 1: Script
        jobs[job_id]["logs"].append("Step 1/5: Generating script...")
        script = generator.generate_script(
            client, theme, style, genre,
            n_scenes, scene_duration, rl,
            p / "script.json", cp,
        )

        # Step 2: Characters
        jobs[job_id]["logs"].append("Step 2/5: Generating characters...")
        char_dir = p / "characters"
        char_dir.mkdir(parents=True, exist_ok=True)
        char_manifest = generator.generate_characters(
            client, script, style, char_dir, cp, rl,
        )

        # Step 3: Storyboard
        jobs[job_id]["logs"].append("Step 3/5: Generating storyboard...")
        sb_dir = p / "storyboard"
        sb_dir.mkdir(parents=True, exist_ok=True)
        sb_manifest = generator.generate_storyboard(
            client, script, style, char_manifest,
            sb_dir, cp, rl,
        )

        # Step 4: Videos
        jobs[job_id]["logs"].append("Step 4/5: Generating videos...")
        vid_dir = p / "videos"
        vid_dir.mkdir(parents=True, exist_ok=True)
        vid_manifest = generator.generate_videos(
            client, script, sb_manifest,
            vid_dir, cp, rl,
            scene_duration=scene_duration,
            style=style,
        )

        # Step 5: Edit
        jobs[job_id]["logs"].append("Step 5/5: Rendering final video...")
        result = generator.edit_final(
            p, script, vid_manifest, cp,
            scene_duration=scene_duration,
        )

        if result:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = str(result)
            jobs[job_id]["logs"].append("All steps completed!")

            # Send completion email if email was provided
            email = meta.get("email", "").strip()
            if email:
                title = meta.get("theme", "未命名")
                jobs[job_id]["logs"].append(f"📧 Sending completion email to {email}...")
                try:
                    ok = send_completion_email(email, title, project_id)
                    if ok:
                        jobs[job_id]["logs"].append(f"✅ Email sent to {email}")
                    else:
                        jobs[job_id]["logs"].append("⚠️ Email sending failed (check SMTP config)")
                except Exception as email_err:
                    jobs[job_id]["logs"].append(f"⚠️ Email error: {email_err}")
        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["logs"].append("Render failed")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")

    return jsonify({"job_id": job_id, "status": jobs[job_id]["status"]})


# ============================================================
# Routes — Step-based Auto Pipeline (for Vercel serverless)
# Each step runs in a separate request to avoid timeout
# ============================================================

@app.route("/api/projects/<project_id>/auto-step", methods=["POST"])
def api_auto_step(project_id: str):
    """Run a single step of the auto pipeline.
    Body: {"step": 1|2|3|4|5, "email": "optional"}
    Returns: {"step": N, "status": "done|failed", "next_step": N+1|0, "job_id": "...", "logs": [...]}
    """
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    body = request.get_json(silent=True) or {}
    step = int(body.get("step", 0))
    email = body.get("email", "").strip()

    if step < 1 or step > 5:
        return jsonify({"error": "step must be 1-5"}), 400

    job_id = make_job(project_id, f"auto-step-{step}")
    jobs[job_id]["status"] = "running"
    jobs[job_id]["logs"].append(f"Step {step}/5 starting...")

    # Restore from Supabase first (in case we're on a different serverless instance)
    try:
        restore_project_from_supabase(project_id)
    except Exception as e:
        jobs[job_id]["logs"].append(f"[supabase] restore warning: {e}")

    try:
        client, rl = get_client_and_rl()
        meta_path = p / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        theme = meta.get("theme", "未命名")
        style = meta.get("style", "三渲二国风")
        genre = meta.get("genre", "仙侠")
        n_scenes = meta.get("n_scenes", 3)
        scene_duration = meta.get("scene_duration", 5)
        cp = generator.Checkpoint(p)

        if step == 1:
            # Step 1: Script
            jobs[job_id]["logs"].append("正在生成剧本...")
            script = generator.generate_script(
                client, theme, style, genre,
                n_scenes, scene_duration, rl,
                p / "script.json", cp,
            )
            jobs[job_id]["logs"].append(f"剧本完成: {script.get('title', '?')}")

        elif step == 2:
            # Step 2: Characters
            script = load_script(p)
            if not script:
                raise RuntimeError("脚本不存在，请先执行步骤1")
            jobs[job_id]["logs"].append("正在生成角色卡...")
            char_dir = p / "characters"
            char_dir.mkdir(parents=True, exist_ok=True)
            char_manifest = generator.generate_characters(
                client, script, style, char_dir, cp, rl,
            )
            jobs[job_id]["logs"].append(f"角色卡完成: {len(char_manifest)}个角色")

        elif step == 3:
            # Step 3: Storyboard
            script = load_script(p)
            if not script:
                raise RuntimeError("脚本不存在")
            char_manifest = load_char_manifest(p)
            jobs[job_id]["logs"].append("正在生成分镜...")
            sb_dir = p / "storyboard"
            sb_dir.mkdir(parents=True, exist_ok=True)
            sb_manifest = generator.generate_storyboard(
                client, script, style, char_manifest,
                sb_dir, cp, rl,
            )
            jobs[job_id]["logs"].append(f"分镜完成: {len(sb_manifest)}个镜头")

        elif step == 4:
            # Step 4: Videos (one at a time to stay within timeout)
            script = load_script(p)
            if not script:
                raise RuntimeError("脚本不存在")
            sb_manifest = load_sb_manifest(p)
            if not sb_manifest:
                raise RuntimeError("分镜不存在，请先执行步骤3")

            vid_dir = p / "videos"
            vid_dir.mkdir(parents=True, exist_ok=True)

            # Find scenes that need video generation
            scenes_to_gen = []
            for s in script.get("scenes", []):
                sid = s["id"]
                vid_file = vid_dir / f"{sid}.mp4"
                cp_key = f"videos.{sid}"
                if not vid_file.exists() and cp.data.get(cp_key) != "done":
                    scenes_to_gen.append(sid)

            if not scenes_to_gen:
                jobs[job_id]["logs"].append("所有视频已生成")
            else:
                # Generate ONE video per request (to stay within timeout)
                sid = scenes_to_gen[0]
                scene = next((s for s in script["scenes"] if s["id"] == sid), None)
                if not scene:
                    raise RuntimeError(f"场景 {sid} 不存在")

                frame_info = sb_manifest.get(sid, {})
                frame_url = frame_info.get("url", "")
                frame_path = frame_info.get("path", "")

                if not frame_url and not (frame_path and pathlib.Path(frame_path).exists()):
                    jobs[job_id]["logs"].append(f"场景 {sid} 无分镜帧，跳过")
                    cp.data[cp_key] = "skipped"
                    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))
                else:
                    jobs[job_id]["logs"].append(f"正在生成视频 {sid}...")
                    image_input = frame_url if frame_url else frame_path
                    video_prompt = f"{scene['action']}，{scene['camera']}，{scene.get('mood','')}氛围，cinematic quality，smooth motion"
                    num_frames = generator.SCENE_DURATION_MAP.get(scene_duration, 121)
                    vid_path = vid_dir / f"{sid}.mp4"

                    client.generate_video_full(
                        prompt=video_prompt,
                        out_path=vid_path,
                        image=image_input,
                        height=768,
                        width=1344,
                        num_frames=num_frames,
                        frame_rate=24,
                    )
                    cp.data[f"videos.{sid}"] = "done"
                    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))
                    jobs[job_id]["logs"].append(f"视频 {sid} 完成")

                # Check if more videos needed
                remaining = 0
                for s in script.get("scenes", []):
                    sid2 = s["id"]
                    vid_file2 = vid_dir / f"{sid2}.mp4"
                    if not vid_file2.exists() and cp.data.get(f"videos.{sid2}") != "done" and cp.data.get(f"videos.{sid2}") != "skipped":
                        remaining += 1
                if remaining > 0:
                    jobs[job_id]["logs"].append(f"还有 {remaining} 个视频待生成")
                    jobs[job_id]["status"] = "done"
                    jobs[job_id]["result"] = {"step": 4, "next_step": 4, "remaining": remaining}
                    return jsonify({
                        "job_id": job_id,
                        "step": 4,
                        "status": "done",
                        "next_step": 4,
                        "remaining": remaining,
                        "logs": jobs[job_id]["logs"],
                    })

        elif step == 5:
            # Step 5: Render
            script = load_script(p)
            if not script:
                raise RuntimeError("脚本不存在")
            jobs[job_id]["logs"].append("正在渲染成片...")
            vid_manifest = {}
            vid_dir = p / "videos"
            for s in script.get("scenes", []):
                sid = s["id"]
                vid_file = vid_dir / f"{sid}.mp4"
                if vid_file.exists():
                    vid_manifest[sid] = {"path": str(vid_file)}

            result = generator.edit_final(
                p, script, vid_manifest, cp,
                scene_duration=scene_duration,
            )
            jobs[job_id]["logs"].append(f"成片完成: {result}")

            # Send email if provided
            if email:
                jobs[job_id]["logs"].append(f"正在发送邮件到 {email}...")
                try:
                    ok = send_completion_email(email, theme, project_id)
                    jobs[job_id]["logs"].append("✅ 邮件发送成功" if ok else "⚠️ 邮件发送失败")
                except Exception as ee:
                    jobs[job_id]["logs"].append(f"⚠️ 邮件错误: {ee}")

        jobs[job_id]["status"] = "done"
        next_step = step + 1 if step < 5 else 0
        jobs[job_id]["result"] = {"step": step, "next_step": next_step}

        # Sync files to Supabase for cross-instance persistence
        try:
            sync_project_to_supabase(project_id)
        except Exception as e:
            jobs[job_id]["logs"].append(f"[supabase] sync warning: {e}")

        return jsonify({
            "job_id": job_id,
            "step": step,
            "status": "done",
            "next_step": next_step,
            "logs": jobs[job_id]["logs"],
        })

    except Exception as e:
        import traceback
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["logs"].append(f"Error: {e}")
        jobs[job_id]["logs"].append(traceback.format_exc())
        return jsonify({
            "job_id": job_id,
            "step": step,
            "status": "failed",
            "next_step": 0,
            "logs": jobs[job_id]["logs"],
        })



# ============================================================
# Routes — File serving
# ============================================================

@app.route("/api/project-files/<project_id>/<path:filepath>", methods=["GET"])
def api_serve_project_file(project_id: str, filepath: str):
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    target = (p / filepath).resolve()
    if not str(target).startswith(str(p.resolve())):
        return jsonify({"error": "非法路径"}), 403
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target)


@app.route("/api/files/<job_id>/<path:filepath>", methods=["GET"])
def api_serve_file(job_id: str, filepath: str):
    out = OUTPUT_BASE / job_id
    if not out.exists():
        return jsonify({"error": "目录不存在"}), 404
    target = (out / filepath).resolve()
    if not str(target).startswith(str(out.resolve())):
        return jsonify({"error": "非法路径"}), 403
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target)


# ============================================================
# Routes — Job status
# ============================================================

@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "id": job["id"],
        "project_id": job.get("project_id", ""),
        "task_type": job.get("task_type", ""),
        "status": job["status"],
        "logs": job.get("logs", [])[-200:],
        "result": job.get("result"),
    })


@app.route("/api/static-video/<filename>", methods=["GET"])
def api_serve_static_video(filename: str):
    """Serve pre-bundled example videos from api/videos/."""
    vid_dir = API_DIR / "videos"
    target = (vid_dir / filename).resolve()
    if not str(target).startswith(str(vid_dir.resolve())):
        return jsonify({"error": "非法路径"}), 403
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target)


@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    return jsonify({
        "jobs": [
            {"id": j["id"], "status": j["status"], "theme": j.get("theme", "")}
            for j in sorted(jobs.values(), key=lambda x: x.get("created_at", 0), reverse=True)
        ]
    })


# ============================================================
# Legacy one-shot generate (compat)
# ============================================================

@app.route("/api/generate", methods=["POST"])
def api_generate():
    body = request.get_json(force=True, silent=True) or {}
    theme = body.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "请输入主题"}), 400

    duration = int(body.get("duration", 15))
    style = body.get("style", "三渲二国风")
    genre = body.get("genre", "仙侠")
    scene_duration = int(body.get("scene_duration", 5))

    slug = re.sub(r"[^\w\u4e00-\u9fff]", "-", theme)[:30] or "untitled"
    project_id = f"{slug}_{int(time.time())}"
    project_dir = OUTPUT_BASE / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "id": project_id, "theme": theme, "style": style, "genre": genre,
        "duration": duration, "scene_duration": scene_duration,
        "n_scenes": duration // scene_duration,
        "created_at": time.time(),
    }
    (project_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    job_id = make_job(project_id, "auto")
    # Return immediately — use /api/projects/<id>/auto to run the full pipeline
    return jsonify({"job_id": job_id, "project_id": project_id, "status": "pending",
                    "message": "Use /api/projects/{project_id}/auto to start generation"})


@app.route("/api/supabase/status", methods=["GET"])
def api_supabase_status():
    """Check Supabase connection status."""
    if not get_storage:
        return jsonify({"available": False, "reason": "module not loaded"})
    storage = get_storage()
    if not storage.available:
        return jsonify({"available": False, "reason": "not configured (set SUPABASE_URL + SUPABASE_SERVICE_KEY env vars)"})
    ok = storage.keepalive()
    return jsonify({"available": True, "connected": ok})


@app.route("/api/supabase/keepalive", methods=["POST"])
def api_supabase_keepalive():
    """Send a keepalive ping to prevent database from going idle."""
    if not get_storage:
        return jsonify({"ok": False, "reason": "module not loaded"}), 503
    storage = get_storage()
    if not storage.available:
        return jsonify({"ok": False, "reason": "not configured"}), 503
    ok = storage.keepalive()
    return jsonify({"ok": ok})


@app.route("/api/supabase/schema", methods=["GET"])
def api_supabase_schema():
    """Return the SQL schema for manual setup."""
    from supabase_storage import SCHEMA_SQL
    return SCHEMA_SQL, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ============================================================
# Vercel handler
# ============================================================

# For local testing
if __name__ == "__main__":
    print("Agnes Comic Drama — Vercel Serverless (local mode)")
    print(f"  http://127.0.0.1:7788")
    app.run(host="127.0.0.1", port=7788, debug=True)
