#!/usr/bin/env python3
"""
Agnes 漫剧生成器 - Web 后端
Flask API 服务，提供生成、进度查询、文件预览接口
"""

from __future__ import annotations

import json
import os
import pathlib
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
CONFIG_FILE = SKILL_DIR / "web" / "config.json"  # 本地配置，存放 API KEY

# 注入 scripts 目录到 sys.path，以便导入 agnes_client
sys.path.insert(0, str(SCRIPTS_DIR))

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ============================================================
# 任务状态存储（内存，生产环境可换 Redis）
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
    """读取本地配置文件"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    """写入本地配置文件"""
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def get_env():
    """读取 Agnes API Key（优先本地配置 → 环境变量 → openclaw.json）"""
    # 1. 本地配置（Web UI 设置的）
    local_cfg = load_config()
    api_key = local_cfg.get("AGNES_API_KEY", "")
    if api_key:
        return {"AGNES_API_KEY": api_key}

    # 2. 环境变量
    api_key = os.environ.get("AGNES_API_KEY", "")
    if api_key:
        return {"AGNES_API_KEY": api_key}

    # 3. openclaw.json
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


def run_generator(theme: str, duration: int, style: str, genre: str,
                  scene_duration: int, output_dir: str, job_id: str):
    """后台执行 run.py，并实时更新 job 状态"""
    jobs[job_id]["status"] = "running"
    jobs[job_id]["logs"] = []

    def log(msg: str):
        print(msg, flush=True)
        jobs[job_id]["logs"].append(msg)

    try:
        env = get_env()
        if not env["AGNES_API_KEY"]:
            raise RuntimeError("未找到 AGNES_API_KEY，请在 OpenClaw 配置中设置")

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

        log(f"🚀 启动生成任务...")
        log(f"   主题：{theme}")
        log(f"   输出：{output_dir}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, **env, "PYTHONUNBUFFERED": "1"},
            cwd=str(SCRIPTS_DIR.parent),
            bufsize=0,
        )

        # 实时读取输出
        for line in iter(proc.stdout.readline, b""):
            line_str = line.decode("utf-8", errors="replace").rstrip()
            if line_str:
                log(line_str)

        proc.wait()
        if proc.returncode == 0:
            jobs[job_id]["status"] = "done"
            # 查找成片路径
            final = pathlib.Path(output_dir) / "final.mp4"
            if final.exists():
                jobs[job_id]["result"] = str(final)
                log(f"🎉 成片已生成：{final}")
            else:
                log("⚠️ 成片未找到，请检查输出目录")
        else:
            jobs[job_id]["status"] = "failed"
            log(f"❌ 进程退出码：{proc.returncode}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        log(f"❌ 异常：{e}")


# ============================================================
# 路由
# ============================================================

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/models", methods=["GET"])
def api_models():
    """返回可用模型列表（静态）"""
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


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """提交新的漫剧生成任务"""
    body = request.get_json(force=True, silent=True) or {}
    theme = body.get("theme", "").strip()
    if not theme:
        return jsonify({"error": "请输入主题"}), 400

    duration = int(body.get("duration", 15))
    style = body.get("style", "三渲二国风")
    genre = body.get("genre", "仙侠")
    scene_duration = int(body.get("scene_duration", 5))

    # 生成输出目录名
    import re
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
        args=(theme, duration, style, genre, scene_duration, output_dir, job_id),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "pending"})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job_status(job_id: str):
    """查询任务状态和日志"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "id": job["id"],
        "status": job["status"],
        "theme": job.get("theme", ""),
        "logs": job.get("logs", [])[-200:],  # 最多返回 200 条
        "result": job.get("result"),
        "output_dir": job.get("output_dir", ""),
    })


@app.route("/api/jobs/<job_id>/files", methods=["GET"])
def api_job_files(job_id: str):
    """列出任务输出目录中的文件"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    out = pathlib.Path(job["output_dir"])
    if not out.exists():
        return jsonify({"files": []})

    files = []
    for p in out.rglob("*"):
        if p.is_file():
            rel = p.relative_to(out)
            files.append({
                "path": str(rel),
                "size": p.stat().st_size,
                "is_video": p.suffix.lower() in [".mp4", ".mov", ".webm"],
                "is_image": p.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"],
            })
    return jsonify({"files": files})


@app.route("/api/files/<job_id>/<path:filepath>", methods=["GET"])
def api_serve_file(job_id: str, filepath: str):
    """提供任务输出文件的访问（图片/视频预览）"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404

    out = pathlib.Path(job["output_dir"])
    target = (out / filepath).resolve()
    # 防止路径穿越
    if not str(target).startswith(str(out.resolve())):
        return jsonify({"error": "非法路径"}), 403

    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404

    return send_file(target)


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
# 配置接口（API KEY 管理，仅存本地）
# ============================================================

@app.route("/api/config", methods=["GET"])
def api_get_config():
    """读取配置（API KEY 脱敏显示）"""
    cfg = load_config()
    api_key = cfg.get("AGNES_API_KEY", "")
    # 脱敏：只显示前4后4
    masked = ""
    if api_key:
        masked = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:] if len(api_key) > 8 else "****"
    return jsonify({
        "AGNES_API_KEY": masked,
        "has_key": bool(api_key),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    """保存配置（API KEY 写入本地 config.json）"""
    body = request.get_json(force=True, silent=True) or {}
    cfg = load_config()

    if "AGNES_API_KEY" in body:
        key = body["AGNES_API_KEY"].strip()
        if key:
            cfg["AGNES_API_KEY"] = key
        else:
            # 空字符串 = 删除 key
            cfg.pop("AGNES_API_KEY", None)
        save_config(cfg)

    return jsonify({"ok": True})


@app.route("/api/config/test", methods=["POST"])
def api_test_config():
    """测试 API KEY 是否有效"""
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
# 启动
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agnes 漫剧生成器 Web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7788)
    args = parser.parse_args()

    print(f"🎭 Agnes 漫剧生成器 Web UI")
    print(f"   访问地址：http://{args.host}:{args.port}")
    print(f"   输出目录：{OUTPUT_BASE}")
    app.run(host=args.host, port=args.port, debug=True)
