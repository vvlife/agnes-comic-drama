#!/usr/bin/env python3
"""
Agnes 漫剧生成器 - Web 后端
Flask API 服务，三步工作流：脚本管理 → 人物管理 → 制作片子
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS

# ============================================================
# 路径配置
# ============================================================
SKILL_DIR = pathlib.Path(__file__).parent.parent  # .../skills/agnes-comic-drama
SCRIPTS_DIR = SKILL_DIR / "scripts"
WORKSPACE = pathlib.Path.home() / ".qclaw" / "workspace"
OUTPUT_BASE = WORKSPACE / "output"
CONFIG_FILE = SKILL_DIR / "web" / "config.json"

sys.path.insert(0, str(SCRIPTS_DIR))

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ============================================================
# 导入 run.py 的函数
# ============================================================
import agnes_client
import run as generator

# ============================================================
# 任务状态存储
# ============================================================
jobs: dict[str, dict] = {}

STATUS = {
    "pending": "等待中",
    "running": "生成中",
    "done": "已完成",
    "failed": "失败",
}


# ============================================================
# 工具函数
# ============================================================

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def get_env():
    local_cfg = load_config()
    api_key = local_cfg.get("AGNES_API_KEY", "")
    if api_key:
        return {"AGNES_API_KEY": api_key}
    api_key = os.environ.get("AGNES_API_KEY", "")
    if api_key:
        return {"AGNES_API_KEY": api_key}
    oc_path = pathlib.Path.home() / ".qclaw" / "openclaw.json"
    if oc_path.exists():
        try:
            cfg = json.loads(oc_path.read_text())
            skills = cfg.get("skills", {}).get("entries", {})
            for k, v in skills.items():
                if "agnes" in k.lower():
                    ak = v.get("env", {}).get("AGNES_API_KEY", "")
                    if ak:
                        return {"AGNES_API_KEY": ak}
        except Exception:
            pass
    return {"AGNES_API_KEY": ""}


def get_client_and_rl():
    """创建 AgnesClient 和 RateLimiter 实例"""
    env = get_env()
    if not env["AGNES_API_KEY"]:
        raise RuntimeError("未找到 AGNES_API_KEY，请在设置中配置")
    client = agnes_client.AgnesClient(api_key=env["AGNES_API_KEY"])
    rl = generator.RateLimiter(rpm=18)
    return client, rl


def get_project_dir(project_id: str) -> pathlib.Path:
    """获取项目目录，验证存在性"""
    p = OUTPUT_BASE / project_id
    return p


def validate_project(project_id: str):
    """验证项目存在，返回目录路径或 None"""
    p = get_project_dir(project_id)
    if not p.exists():
        return None
    return p


def load_script(project_dir: pathlib.Path) -> dict | None:
    """加载项目脚本"""
    sp = project_dir / "script.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except Exception:
            pass
    return None


def save_script(project_dir: pathlib.Path, script: dict):
    """保存脚本"""
    sp = project_dir / "script.json"
    sp.write_text(json.dumps(script, ensure_ascii=False, indent=2))


def load_char_manifest(project_dir: pathlib.Path) -> dict:
    """加载角色卡 manifest"""
    mp = project_dir / "characters" / "manifest.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass
    return {}


def load_sb_manifest(project_dir: pathlib.Path) -> dict:
    """加载分镜 manifest"""
    mp = project_dir / "storyboard" / "manifest.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            pass
    return {}


def load_vid_manifest(project_dir: pathlib.Path) -> dict:
    """从视频目录重建视频 manifest"""
    vid_dir = project_dir / "videos"
    manifest = {}
    if vid_dir.exists():
        for f in vid_dir.glob("*.mp4"):
            sid = f.stem
            manifest[sid] = str(f)
    return manifest


# ============================================================
# 异步任务辅助
# ============================================================

def start_background_task(project_id: str, task_type: str, target_func, *args, **kwargs):
    """启动后台任务并返回 job_id"""
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

    def wrapper():
        jobs[job_id]["status"] = "running"
        try:
            result = target_func(*args, **kwargs)
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = result
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["logs"].append(f"❌ 错误：{e}")

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return job_id


# ============================================================
# 路由 — 页面
# ============================================================

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ============================================================
# 路由 — 基础 API（模型/风格等）
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


# ============================================================
# 路由 — 项目管理
# ============================================================

@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    """列出所有项目"""
    projects = []
    if OUTPUT_BASE.exists():
        for d in sorted(OUTPUT_BASE.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.is_dir():
                script = load_script(d)
                projects.append({
                    "id": d.name,
                    "title": script.get("title", "") if script else d.name,
                    "has_script": (d / "script.json").exists(),
                    "has_characters": (d / "characters" / "manifest.json").exists(),
                    "has_storyboard": (d / "storyboard" / "manifest.json").exists(),
                    "has_videos": (d / "videos").exists() and any((d / "videos").glob("*.mp4")),
                    "has_final": (d / "final.mp4").exists(),
                    "modified": d.stat().st_mtime,
                })
    return jsonify({"projects": projects})


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    """创建新项目"""
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

    # Use ASCII-safe slug for project ID
    slug = re.sub(r"[^a-zA-Z0-9]", "-", theme)[:20].strip("-") or "project"
    project_id = f"{slug}_{int(time.time())}"
    project_dir = OUTPUT_BASE / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    # 保存项目元信息
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
        "created_at": time.time(),
    }
    (project_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    return jsonify({"id": project_id, "meta": meta}), 201


@app.route("/api/projects/<project_id>", methods=["GET"])
def api_get_project(project_id: str):
    """获取项目详情"""
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
    """删除项目"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    shutil.rmtree(p)
    return jsonify({"ok": True})


# ============================================================
# 路由 — 脚本管理
# ============================================================

@app.route("/api/projects/<project_id>/script", methods=["POST"])
def api_generate_script(project_id: str):
    """生成/重新生成脚本"""
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

    # 重置相关 checkpoint
    cp = generator.Checkpoint(p)
    cp.data.pop("script", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_script_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "script",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"📝 开始生成脚本...")
            client, rl = get_client_and_rl()
            cp = generator.Checkpoint(p)
            script = generator.generate_script(
                client, theme, style, genre,
                n_scenes, scene_duration, rl,
                p / "script.json", cp,
            )
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = script
            log(f"✅ 脚本生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 脚本生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/projects/<project_id>/script", methods=["GET"])
def api_get_script(project_id: str):
    """获取脚本"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本尚未生成"}), 404
    return jsonify(script)


@app.route("/api/projects/<project_id>/script", methods=["PUT"])
def api_update_script(project_id: str):
    """更新脚本（手动编辑保存）"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "无效数据"}), 400

    save_script(p, body)
    return jsonify({"ok": True})


# ============================================================
# 路由 — 角色管理
# ============================================================

@app.route("/api/projects/<project_id>/characters", methods=["GET"])
def api_get_characters(project_id: str):
    """获取所有角色卡"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    char_manifest = load_char_manifest(p)
    char_dir = p / "characters"

    # 如果 manifest 缺少角色但图片存在，自动修复
    needs_fix = False
    if script:
        for char in script.get("characters", []):
            cid = char["id"]
            if cid not in char_manifest:
                # 检查磁盘上是否有该角色的图片
                char_images = []
                for img_type in ["full", "close", "chibi"]:
                    img_path = char_dir / f"{cid}_{img_type}.png"
                    if img_path.exists():
                        char_images.append(str(img_path))
                if char_images:
                    char_manifest[cid] = {"name": char.get("name", cid), "images": char_images}
                    needs_fix = True
    if needs_fix and char_dir.exists():
        (char_dir / "manifest.json").write_text(json.dumps(char_manifest, ensure_ascii=False, indent=2))

    characters = []
    if script:
        for char in script.get("characters", []):
            cid = char["id"]
            images = char_manifest.get(cid, {}).get("images", [])
            # 将绝对路径转为相对路径用于URL
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
    """更新角色信息（visual等）"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    body = request.get_json(force=True, silent=True) or {}
    found = False
    for char in script.get("characters", []):
        if char["id"] == cid:
            for k, v in body.items():
                if k in ("name", "visual", "personality", "age"):
                    char[k] = v
            found = True
            break

    if not found:
        return jsonify({"error": "角色不存在"}), 404

    save_script(p, script)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/characters/<cid>/generate", methods=["POST"])
def api_generate_character(project_id: str, cid: str):
    """为角色生成/重新生成图片"""
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

    # 删除旧的角色图
    char_dir = p / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    for old in char_dir.glob(f"{cid}_*.png"):
        old.unlink(missing_ok=True)
    # 更新 manifest 移除该角色
    char_manifest = load_char_manifest(p)
    char_manifest.pop(cid, None)
    (char_dir / "manifest.json").write_text(json.dumps(char_manifest, ensure_ascii=False, indent=2))

    # 不重置 characters checkpoint，仅重置该角色的 checkpoint
    cp = generator.Checkpoint(p)
    # generate_characters 使用 "characters" 作为整体 checkpoint key
    # 为了让单个角色能重新生成，需要重置整体 checkpoint
    cp.data.pop("characters", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_char_{cid}_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "character",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    # 传入完整脚本而非 mini_script，这样 generate_characters 会为所有角色生成
    # 但因为其他角色的图已存在，它们会被跳过，只有当前角色会被重新生成

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"🎨 生成角色 {char_info['name']} 的三联卡...")
            client, rl = get_client_and_rl()
            meta_path = p / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            style = meta.get("style", "三渲二国风")

            # 传入完整脚本，其他角色的已有图片会被跳过
            manifest = generator.generate_characters(
                client, script, style,
                char_dir, generator.Checkpoint(p), rl,
            )
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = manifest
            log(f"✅ 角色 {char_info['name']} 生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 角色生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


# ============================================================
# 路由 — 分镜 & 视频
# ============================================================

@app.route("/api/projects/<project_id>/storyboard/manifest", methods=["GET"])
def api_get_storyboard_manifest(project_id: str):
    """获取分镜 manifest"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404
    return jsonify(load_sb_manifest(p))


@app.route("/api/projects/<project_id>/meta", methods=["PUT"])
def api_update_meta(project_id: str):
    """更新项目元信息"""
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

@app.route("/api/projects/<project_id>/storyboard/generate-all", methods=["POST"])
def api_generate_all_storyboard(project_id: str):
    """生成全部分镜帧"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    # 重置 storyboard checkpoint
    cp = generator.Checkpoint(p)
    cp.data.pop("storyboard", None)
    # 也重置各场景的 checkpoint
    keys_to_remove = [k for k in cp.data if k.startswith("storyboard.")]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_sb_all_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "storyboard",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"🖼️ 开始生成分镜关键帧...")
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
            log(f"✅ 分镜关键帧全部生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 分镜生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/projects/<project_id>/storyboard/<sid>/generate", methods=["POST"])
def api_generate_storyboard_scene(project_id: str, sid: str):
    """为某个场景生成分镜帧"""
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

    # 删除旧分镜帧
    sb_dir = p / "storyboard"
    old_frame = sb_dir / f"{sid}.png"
    old_frame.unlink(missing_ok=True)
    # 更新 manifest 移除该场景
    sb_manifest = load_sb_manifest(p)
    sb_manifest.pop(sid, None)
    sb_dir.mkdir(parents=True, exist_ok=True)
    (sb_dir / "manifest.json").write_text(json.dumps(sb_manifest, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_sb_{sid}_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "storyboard",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"🖼️ 生成场景 {sid} 的分镜帧...")
            client, rl = get_client_and_rl()
            meta_path = p / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            style = meta.get("style", "三渲二国风")
            style_info = generator.STYLE_PRESETS.get(style, generator.STYLE_PRESETS["三渲二国风"])
            char_manifest = load_char_manifest(p)

            # 构建提示词
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
                size=generator.IMAGE_SIZES["portrait"],
                reference_images=ref_images if ref_images else None,
                response_format="url",
            )

            import requests
            sb_dir.mkdir(parents=True, exist_ok=True)
            r = requests.get(img_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(old_frame, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)

            # 更新 manifest
            sb_manifest[sid] = {"path": str(old_frame), "url": img_url, "prompt": prompt}
            (sb_dir / "manifest.json").write_text(json.dumps(sb_manifest, ensure_ascii=False, indent=2))

            jobs[job_id]["status"] = "done"
            log(f"✅ 场景 {sid} 分镜帧生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 分镜帧生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/projects/<project_id>/videos/generate-all", methods=["POST"])
def api_generate_all_videos(project_id: str):
    """生成全部视频"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    # 重置 videos checkpoint
    cp = generator.Checkpoint(p)
    cp.data.pop("videos", None)
    keys_to_remove = [k for k in cp.data if k.startswith("videos.")]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_vid_all_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "videos",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"📹 开始生成视频...")
            client, rl = get_client_and_rl()
            meta_path = p / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            scene_duration = meta.get("scene_duration", 5)
            sb_manifest = load_sb_manifest(p)
            vid_dir = p / "videos"

            manifest = generator.generate_videos(
                client, script, sb_manifest,
                vid_dir, generator.Checkpoint(p), rl,
                scene_duration=scene_duration,
            )
            jobs[job_id]["status"] = "done"
            jobs[job_id]["result"] = manifest
            log(f"✅ 全部视频生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 视频生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/projects/<project_id>/videos/<sid>/generate", methods=["POST"])
def api_generate_video_scene(project_id: str, sid: str):
    """为某个场景生成视频"""
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

    # 检查分镜帧
    sb_manifest = load_sb_manifest(p)
    frame_info = sb_manifest.get(sid, {})
    frame_url = frame_info.get("url", "")
    frame_path = frame_info.get("path", "")
    if not frame_url and not (frame_path and pathlib.Path(frame_path).exists()):
        return jsonify({"error": "该场景尚无分镜帧，请先生成分镜帧"}), 400

    # 删除旧视频
    vid_dir = p / "videos"
    old_vid = vid_dir / f"{sid}.mp4"
    old_vid.unlink(missing_ok=True)

    job_id = f"{project_id}_vid_{sid}_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "video",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"📹 生成场景 {sid} 的视频...")
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
                height=1344,
                width=768,
                num_frames=num_frames,
                frame_rate=24,
            )

            # 更新 checkpoint
            cp = generator.Checkpoint(p)
            cp.data[f"videos.{sid}"] = "done"
            cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

            jobs[job_id]["status"] = "done"
            log(f"✅ 场景 {sid} 视频生成完成")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 视频生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


# ============================================================
# 路由 — 成片 & 场景管理
# ============================================================

@app.route("/api/projects/<project_id>/render", methods=["POST"])
def api_render(project_id: str):
    """拼接成片"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    # 删除旧成片
    for f in ["final.mp4", "final_with_sub.mp4", "final_with_audio.mp4", "final_lipsync.mp4"]:
        (p / f).unlink(missing_ok=True)

    # 重置 edit checkpoint
    cp = generator.Checkpoint(p)
    cp.data.pop("edit", None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_render_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "render",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            log(f"🎬 开始拼接成片...")
            meta_path = p / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            scene_duration = meta.get("scene_duration", 5)
            vid_manifest = load_vid_manifest(p)

            result = generator.edit_final(
                p, script, vid_manifest,
                generator.Checkpoint(p),
                scene_duration=scene_duration,
            )

            # 可选 TTS + 音效
            if meta.get("enable_tts") or meta.get("enable_sfx"):
                log(f"🎙️ 生成 TTS 配音...")
                tts_manifest = {}
                if meta.get("enable_tts"):
                    tts_manifest = generator.generate_tts(
                        script, p / "audio", generator.Checkpoint(p),
                        scene_duration=scene_duration,
                        vid_manifest=vid_manifest,
                    )

                sfx_manifest = {}
                if meta.get("enable_sfx"):
                    log(f"🔊 生成音效描述...")
                    client, rl = get_client_and_rl()
                    sfx_manifest = generator.generate_sfx(
                        client, script, p / "sfx",
                        generator.Checkpoint(p), rl,
                    )

                if tts_manifest or sfx_manifest:
                    log(f"🎚️ 混音...")
                    final_with_audio = generator.mix_audio(
                        p, script, tts_manifest, sfx_manifest,
                        generator.Checkpoint(p),
                        scene_duration=scene_duration,
                    )
                    if final_with_audio:
                        log(f"✅ 混音成片：{final_with_audio}")

            if result:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["result"] = str(result)
                log(f"✅ 成片已生成")
            else:
                jobs[job_id]["status"] = "failed"
                log(f"❌ 成片拼接失败")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 成片生成失败：{e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/projects/<project_id>/scenes/order", methods=["PUT"])
def api_update_scene_order(project_id: str):
    """调整场景顺序"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    body = request.get_json(force=True, silent=True) or {}
    order = body.get("order", [])  # ["S03", "S01", "S02"]
    if not order:
        return jsonify({"error": "无效顺序"}), 400

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    # 重新排列场景
    scene_map = {s["id"]: s for s in script.get("scenes", [])}
    new_scenes = []
    for sid in order:
        if sid in scene_map:
            new_scenes.append(scene_map[sid])
    # 追加不在 order 中的场景
    for s in script.get("scenes", []):
        if s["id"] not in order:
            new_scenes.append(s)

    script["scenes"] = new_scenes
    save_script(p, script)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/scenes/<sid>", methods=["DELETE"])
def api_delete_scene(project_id: str, sid: str):
    """删除场景"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    script = load_script(p)
    if not script:
        return jsonify({"error": "脚本不存在"}), 404

    script["scenes"] = [s for s in script.get("scenes", []) if s["id"] != sid]
    save_script(p, script)

    # 清理关联文件
    (p / "storyboard" / f"{sid}.png").unlink(missing_ok=True)
    (p / "videos" / f"{sid}.mp4").unlink(missing_ok=True)

    return jsonify({"ok": True})


# ============================================================
# 路由 — 文件服务
# ============================================================

@app.route("/api/project-files/<project_id>/<path:filepath>", methods=["GET"])
def api_serve_project_file(project_id: str, filepath: str):
    """提供项目文件的访问（图片/视频预览）"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    target = (p / filepath).resolve()
    if not str(target).startswith(str(p.resolve())):
        return jsonify({"error": "非法路径"}), 403
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404

    return send_file(target)


# 保留原有文件服务接口（兼容旧 job 模式）
@app.route("/api/files/<job_id>/<path:filepath>", methods=["GET"])
def api_serve_file(job_id: str, filepath: str):
    job = jobs.get(job_id)
    if job:
        out = pathlib.Path(job.get("output_dir", ""))
    else:
        # 尝试作为 project_id
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
# 路由 — 任务状态（通用）
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


# ============================================================
# 路由 — 兼容旧一键生成接口
# ============================================================

def run_generator(theme: str, duration: int, style: str, genre: str,
                  scene_duration: int, output_dir: str, job_id: str,
                  enable_tts: bool = True, enable_sfx: bool = True):
    """后台执行 run.py（旧一键模式）"""
    jobs[job_id]["status"] = "running"
    jobs[job_id]["logs"] = []

    def log(msg: str):
        print(msg, flush=True)
        jobs[job_id]["logs"].append(msg)

    try:
        env = get_env()
        if not env["AGNES_API_KEY"]:
            raise RuntimeError("未找到 AGNES_API_KEY")

        run_py = SCRIPTS_DIR / "run.py"
        cmd = [
            "python3", "-u", str(run_py),
            "--theme", theme,
            "--duration", str(duration),
            "--style", style,
            "--genre", genre,
            "--scene-duration", str(scene_duration),
            "--output", output_dir,
        ]
        if not enable_tts:
            cmd.append("--no-tts")
        if not enable_sfx:
            cmd.append("--no-sfx")

        log(f"🚀 启动生成任务...")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, **env, "PYTHONUNBUFFERED": "1"},
            cwd=str(SCRIPTS_DIR.parent),
            bufsize=0,
        )

        for line in iter(proc.stdout.readline, b""):
            line_str = line.decode("utf-8", errors="replace").rstrip()
            if line_str:
                log(line_str)

        proc.wait()
        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
            final = pathlib.Path(output_dir) / "final.mp4"
            if final.exists():
                jobs[job_id]["result"] = str(final)
                log(f"🎉 成片已生成：{final}")
        else:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 进程退出码：{proc.returncode}")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        log(f"❌ 异常：{e}")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """一键生成（兼容旧接口）"""
    body = request.get_json(force=True, silent=True) or {}
    theme = body.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "请输入主题"}), 400

    duration = int(body.get("duration", 15))
    style = body.get("style", "三渲二国风")
    genre = body.get("genre", "仙侠")
    scene_duration = int(body.get("scene_duration", 5))
    enable_tts = body.get("enable_tts", True)
    enable_sfx = body.get("enable_sfx", True)

    slug = re.sub(r"[^\w\u4e00-\u9fff]", "-", theme)[:30] or "untitled"
    output_dir = str(OUTPUT_BASE / f"{slug}_{int(time.time())}")

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "theme": theme,
        "output_dir": output_dir,
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    t = threading.Thread(
        target=run_generator,
        args=(theme, duration, style, genre, scene_duration, output_dir, job_id,
              enable_tts, enable_sfx),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/jobs/<job_id>/files", methods=["GET"])
def api_job_files(job_id: str):
    """列出任务输出目录中的文件（兼容旧接口）"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"files": []})

    out = pathlib.Path(job.get("output_dir", ""))
    if not out.exists():
        return jsonify({"files": []})

    files = []
    for fp in out.rglob("*"):
        if fp.is_file():
            rel = fp.relative_to(out)
            files.append({
                "path": str(rel),
                "size": fp.stat().st_size,
                "is_video": fp.suffix.lower() in [".mp4", ".mov", ".webm"],
                "is_image": fp.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"],
            })
    return jsonify({"files": files})


@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    """列出所有任务"""
    return jsonify({
        "jobs": [
            {"id": j["id"], "status": j["status"], "theme": j.get("theme", "")}
            for j in sorted(jobs.values(), key=lambda x: x.get("created_at", 0), reverse=True)
        ]
    })


# ============================================================
# 路由 — 配置接口
# ============================================================

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    api_key = cfg.get("AGNES_API_KEY", "")
    masked = ""
    if api_key:
        masked = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:] if len(api_key) > 8 else "****"
    return jsonify({
        "AGNES_API_KEY": masked,
        "has_key": bool(api_key),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    if "AGNES_API_KEY" in body:
        key = body["AGNES_API_KEY"].strip()
        if key:
            cfg["AGNES_API_KEY"] = key
        else:
            cfg.pop("AGNES_API_KEY", None)
        save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/test", methods=["POST"])
def api_test_config():
    env = get_env()
    api_key = env.get("AGNES_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "未配置 API KEY"})
    try:
        import urllib.request
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            "https://apihub.agnes-ai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            if resp.status == 200:
                return jsonify({"ok": True, "message": "API KEY 有效 ✅"})
            else:
                return jsonify({"ok": False, "error": f"HTTP {resp.status}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ============================================================
# 路由 — 自动模式（一键到底）
# ============================================================

@app.route("/api/projects/<project_id>/auto", methods=["POST"])
def api_auto_run(project_id: str):
    """自动模式：从脚本生成到成片拼接，全自动执行"""
    p = validate_project(project_id)
    if not p:
        return jsonify({"error": "项目不存在"}), 404

    # 重置所有 checkpoint
    cp = generator.Checkpoint(p)
    for key in ["script", "characters", "storyboard", "videos", "edit"]:
        cp.data.pop(key, None)
    keys_to_remove = [k for k in cp.data if k.startswith(("storyboard.", "videos."))]
    for k in keys_to_remove:
        cp.data.pop(k, None)
    cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    job_id = f"{project_id}_auto_{uuid.uuid4().hex[:6]}"
    jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "task_type": "auto",
        "status": "pending",
        "logs": [],
        "result": None,
        "created_at": time.time(),
    }

    def _run():
        jobs[job_id]["status"] = "running"
        def log(msg):
            print(msg, flush=True)
            jobs[job_id]["logs"].append(msg)
        try:
            meta_path = p / "meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            theme = meta.get("theme", "未命名")
            style = meta.get("style", "三渲二国风")
            genre = meta.get("genre", "仙侠")
            n_scenes = meta.get("n_scenes", 3)
            scene_duration = meta.get("scene_duration", 5)
            enable_tts = meta.get("enable_tts", True)
            enable_sfx = meta.get("enable_sfx", True)

            client, rl = get_client_and_rl()
            cp = generator.Checkpoint(p)

            # Step 1: Script
            log(f"📝 步骤 1/5：生成剧本（{n_scenes} 个镜头）...")
            script = generator.generate_script(
                client, theme, style, genre,
                n_scenes, scene_duration, rl,
                p / "script.json", cp,
            )
            log(f"✅ 剧本生成完成：{script.get('title', '')}")

            # Step 2: Characters
            log(f"🎨 步骤 2/5：生成角色卡（{len(script.get('characters', []))} 个角色）...")
            char_dir = p / "characters"
            char_dir.mkdir(parents=True, exist_ok=True)
            char_manifest = generator.generate_characters(
                client, script, style, char_dir, cp, rl,
            )
            log(f"✅ 角色卡生成完成")

            # Step 3: Storyboard
            log(f"🖼️ 步骤 3/5：生成分镜关键帧（{len(script.get('scenes', []))} 个镜头）...")
            sb_dir = p / "storyboard"
            sb_dir.mkdir(parents=True, exist_ok=True)
            sb_manifest = generator.generate_storyboard(
                client, script, style, char_manifest,
                sb_dir, cp, rl,
            )
            log(f"✅ 分镜关键帧生成完成")

            # Step 4: Videos
            log(f"📹 步骤 4/5：生成视频...")
            vid_dir = p / "videos"
            vid_dir.mkdir(parents=True, exist_ok=True)
            vid_manifest = generator.generate_videos(
                client, script, sb_manifest,
                vid_dir, cp, rl,
                scene_duration=scene_duration,
            )
            log(f"✅ 视频生成完成")

            # Step 5: Edit + TTS/SFX
            log(f"🎬 步骤 5/5：拼接成片...")
            result = generator.edit_final(
                p, script, vid_manifest, cp,
                scene_duration=scene_duration,
            )

            # Optional TTS + SFX
            if enable_tts or enable_sfx:
                tts_manifest = {}
                if enable_tts:
                    log(f"🎙️ 生成 TTS 配音...")
                    tts_manifest = generator.generate_tts(
                        script, p / "audio", cp,
                        scene_duration=scene_duration,
                        vid_manifest=vid_manifest,
                    )

                sfx_manifest = {}
                if enable_sfx:
                    log(f"🔊 生成音效描述...")
                    sfx_manifest = generator.generate_sfx(
                        client, script, p / "sfx", cp, rl,
                    )

                if tts_manifest or sfx_manifest:
                    log(f"🎚️ 混音...")
                    final_with_audio = generator.mix_audio(
                        p, script, tts_manifest, sfx_manifest, cp,
                        scene_duration=scene_duration,
                    )
                    if final_with_audio:
                        log(f"✅ 混音成片：{final_with_audio}")

            if result:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["result"] = str(result)
                log(f"🎉 全流程完成！")
            else:
                jobs[job_id]["status"] = "failed"
                log(f"❌ 成片拼接失败")
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 自动模式失败：{e}")
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "pending"})


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agnes 漫剧生成器 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7788)
    args = parser.parse_args()

    print(f"🎭 Agnes 漫剧生成器 Web UI（三步工作流版）")
    print(f"   访问地址：http://{args.host}:{args.port}")
    print(f"   输出目录：{OUTPUT_BASE}")
    app.run(host=args.host, port=args.port, debug=False)
