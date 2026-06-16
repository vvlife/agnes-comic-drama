#!/usr/bin/env python3
"""
Agnes 漫剧生成 - 一键运行脚本

用法：
  python scripts/run.py --theme "少年剑仙三年归来" --duration 180 --style 三渲二国风 --genre 仙侠

流程：
  1. 剧本生成（agnes-2.0-flash）
  2. 角色三联卡（agnes-image-2.1-flash）
  3. 分镜关键帧（agnes-image-2.1-flash，图生图，角色卡参考）
  4. 图生视频（agnes-video-v2.0）
  5. 成片拼接（ffmpeg）

可选（需额外 API Key）：
  6. TTS 配音（edge-tts 跨平台 / macOS say 回退）
  7. 音效生成（Agnes 文本描述 + 免费音效库）
  8. 对口型（KLING_API_KEY）
  9. BGM（Suno / Agnes 生成 M3U）
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import requests

# 将 scripts 目录加入 path
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from agnes_client import AgnesClient


# ===================== 配置 =====================

STYLE_PRESETS = {
    "三渲二国风": {
        "prefix": "三渲二国风动画风格，工笔线条，中国传统审美",
        "lighting": "柔和自然光，水墨晕染",
        "palette": "青绿山水色调，朱砂点缀",
    },
    "水墨": {
        "prefix": "中国水墨画风格，留白意境",
        "lighting": "墨色浓淡",
        "palette": "黑白灰为主，偶尔赭石",
    },
    "赛博朋克": {
        "prefix": "赛博朋克动画风格，霓虹灯光，未来科技",
        "lighting": "霓虹光，雨夜反光",
        "palette": "紫蓝配色，霓虹点缀",
    },
    "日系动漫": {
        "prefix": "日系动画风格，精致线条，明亮色彩",
        "lighting": "柔光晴天",
        "palette": "明亮动漫配色",
    },
}

GENRE_PRESETS = {
    "仙侠": "门派纷争 / 修仙问道 / 御剑飞行 / 法宝秘术",
    "宫斗": "宫廷权谋 / 妃嫔博弈 / 皇家礼仪 / 雕梁画栋",
    "江湖": "快意恩仇 / 武林纷争 / 客栈酒肆 / 刀光剑影",
    "都市": "现代都市 / 商战 / 情感纠葛 / 职场风云",
}

# 竖屏尺寸映射（Agnes Image 支持）
IMAGE_SIZES = {
    "portrait": "768x1344",   # 接近 9:16
    "landscape": "1344x768",  # 16:9
    "square": "1024x1024",    # 1:1
}

# 视频时长 → num_frames 映射（必须满足 8n+1 且 ≤441）
# frame_rate = 24
SCENE_DURATION_MAP = {
    5: 121,    # 5s → 121 frames (5.04s)
    10: 241,   # 10s → 241 frames (10.04s)
    15: 361,   # 15s → 361 frames (15.04s)
}

DEFAULTS = {
    "duration_total": 180,
    "scene_duration": 5,
    "style": "三渲二国风",
    "genre": "仙侠",
    "size": "portrait",
    "enable_tts": True,
    "enable_sfx": True,
    "enable_bgm": False,
}

# 音效提示词模板（用于描述场景音效）
SFX_TEMPLATES = {
    "剑气": "sword slash, metallic whoosh, sharp blade cutting air",
    "法术": "magical energy burst, mystical sparkle, ethereal power",
    "爆炸": "explosion, impact, debris scattering, low rumble",
    "风声": "wind howling, gusty breeze, air rushing",
    "水声": "water flowing, stream bubbling, gentle splash",
    "脚步声": "footsteps on stone, echoing in corridor",
    "心跳": "heartbeat, rhythmic thumping, tense atmosphere",
    "雷鸣": "thunder rumble, storm approaching, dramatic",
    "鸟鸣": "birds chirping, peaceful nature, morning ambience",
    "战斗": "clashing weapons, combat, intense action",
}


# ===================== Checkpoint =====================

class Checkpoint:
    def __init__(self, project_dir: pathlib.Path):
        self.path = project_dir / ".checkpoint.json"
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}

    def is_done(self, step: str) -> bool:
        return self.data.get(step) == "done"

    def mark_done(self, step: str):
        self.data[step] = "done"
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    def mark_running(self, step: str):
        self.data[step] = "running"
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))


# ===================== RPM 限流 =====================

class RateLimiter:
    """简单的 RPM 限流器，确保每分钟不超过 20 次请求。"""

    def __init__(self, rpm: int = 18):  # 留 2 个余量
        self.rpm = rpm
        self.timestamps: list[float] = []

    def wait(self):
        now = time.time()
        # 清理 60s 前的记录
        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.rpm:
            oldest = self.timestamps[0]
            sleep_time = 60 - (now - oldest) + 0.5
            if sleep_time > 0:
                print(f"  ⏳ RPM 限流，等待 {sleep_time:.1f}s...")
                time.sleep(sleep_time)
        self.timestamps.append(time.time())


# ===================== 步骤 1：剧本生成 =====================

def generate_script(client: AgnesClient, theme: str, style: str, genre: str,
                    n_scenes: int, scene_duration: int, rate_limiter: RateLimiter,
                    out_path: pathlib.Path, cp: Checkpoint) -> dict:
    """用 agnes-2.0-flash 生成剧本 JSON。"""

    if cp.is_done("script"):
        print("✅ 剧本已存在，跳过")
        return json.loads(out_path.read_text())

    print(f"\n📝 步骤 1/5：生成剧本（{n_scenes} 个镜头）...")
    cp.mark_running("script")

    style_info = STYLE_PRESETS.get(style, STYLE_PRESETS["三渲二国风"])
    genre_info = GENRE_PRESETS.get(genre, genre)

    prompt = f"""你是一位专业的漫剧剧本编剧。请根据以下信息生成分幕剧本，输出严格的 JSON 格式。

主题：{theme}
风格：{style}
类型：{genre_info}
总镜头数：{n_scenes}
每镜头时长：{scene_duration}秒

输出格式（严格 JSON）：
{{
  "title": "剧名",
  "characters": [
    {{"id": "C1", "name": "角色名", "visual": "详细外观描述（服装/发色/体型/特征）", "personality": "性格气质", "age": "年龄"}}
  ],
  "scenes": [
    {{
      "id": "S01",
      "location": "场景地点",
      "time": "时间（如清晨/正午/傍晚/深夜）",
      "characters": ["C1"],
      "action": "画面动作描述（不要对白）",
      "dialogue": [{{"character": "C1", "text": "台词"}}],
      "camera": "镜头运动（如特写/中景/远景/推镜头/摇镜头）",
      "mood": "氛围（如苍凉/壮阔/温馨/紧张）"
    }}
  ]
}}

要求：
1. 角色 2-4 个，每个角色要有独特外观特征便于 AI 绘图
2. 对白简短有力，每句不超过 15 字
3. 镜头之间有连贯性，有叙事推进
4. 保留悬念和冲突
5. 只输出 JSON，不要其他文字"""

    rate_limiter.wait()
    result = client.chat(
        messages=[{"role": "user", "content": prompt}],
        model="agnes-2.0-flash",
        temperature=0.85,
        max_tokens=8192,
    )

    # 提取 JSON
    json_str = result
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]

    script = json.loads(json_str.strip())

    # 确保每个场景都有 mood 字段
    for scene in script.get("scenes", []):
        if "mood" not in scene:
            scene["mood"] = "紧张"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(script, ensure_ascii=False, indent=2))
    cp.mark_done("script")
    print(f"  ✅ 剧本已保存：{out_path}")
    return script


# ===================== 步骤 2：角色三联卡 =====================

def generate_characters(client: AgnesClient, script: dict, style: str,
                        char_dir: pathlib.Path, cp: Checkpoint,
                        rate_limiter: RateLimiter) -> dict:
    """用 agnes-image-2.1-flash 为每个角色生成 3 张图。"""

    if cp.is_done("characters"):
        print("✅ 角色卡已存在，跳过")
        manifest_path = char_dir / "manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🎨 步骤 2/5：生成角色三联卡（{len(script['characters'])} 个角色）...")
    cp.mark_running("characters")

    style_prefix = STYLE_PRESETS.get(style, STYLE_PRESETS["三渲二国风"])["prefix"]
    manifest = {}

    for char in script["characters"]:
        cid = char["id"]
        print(f"  生成角色 {char['name']}（{cid}）...")

        # 每种图一张
        prompts = {
            "full": f"{style_prefix}，角色全身立绘，{char['visual']}，{char['personality']}气质，站姿，居中构图，纯色背景，高质量角色设定图",
            "close": f"{style_prefix}，角色半身特写，{char['visual']}，{char['personality']}表情，肩部以上，正面，高质量",
            "chibi": f"{style_prefix}，Q版头像，{char['visual']}简化版，可爱风格，圆润线条，大眼睛",
        }

        char_images = []
        for img_type, prompt in prompts.items():
            out_path = char_dir / f"{cid}_{img_type}.png"
            if out_path.exists():
                print(f"    {img_type} 已存在，跳过")
                char_images.append(str(out_path))
                continue

            rate_limiter.wait()
            try:
                client.generate_image_to_file(
                    prompt=prompt,
                    out_path=out_path,
                    size=IMAGE_SIZES["portrait"],
                    response_format="url",
                )
                char_images.append(str(out_path))
                print(f"    ✅ {img_type}")
            except Exception as e:
                print(f"    ❌ {img_type} 失败：{e}")

        manifest[cid] = {
            "name": char["name"],
            "images": char_images,
        }

    manifest_path = char_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("characters")
    print(f"  ✅ 角色卡已保存：{char_dir}")
    return manifest


# ===================== 步骤 3：分镜关键帧 =====================

def generate_storyboard(client: AgnesClient, script: dict, style: str,
                        char_manifest: dict, sb_dir: pathlib.Path,
                        cp: Checkpoint, rate_limiter: RateLimiter) -> dict:
    """用 agnes-image-2.1-flash 图生图生成分镜关键帧。"""

    if cp.is_done("storyboard"):
        print("✅ 分镜关键帧已存在，跳过")
        manifest_path = sb_dir / "manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🖼️ 步骤 3/5：生成分镜关键帧（{len(script['scenes'])} 个镜头）...")
    cp.mark_running("storyboard")

    style_info = STYLE_PRESETS.get(style, STYLE_PRESETS["三渲二国风"])
    manifest = {}

    for scene in script["scenes"]:
        sid = scene["id"]
        out_path = sb_dir / f"{sid}.png"

        if out_path.exists():
            print(f"  {sid} 已存在，跳过")
            manifest[sid] = {"path": str(out_path), "prompt": ""}
            continue

        # 构建提示词
        prompt = f"{style_info['prefix']}，{scene['location']}，{scene['time']}。{scene['action']}。{scene['camera']}。{scene['mood']}氛围。{style_info['lighting']}。{style_info['palette']}。"

        # 收集该镜出场角色的参考图
        ref_images = []
        for cid in scene.get("characters", []):
            if cid in char_manifest:
                imgs = char_manifest[cid].get("images", [])
                # 优先取全身立绘
                for img in imgs:
                    if "full" in img:
                        ref_images.append(img)
                        break
                # 最多 4 张参考图
                if len(ref_images) >= 4:
                    break

        rate_limiter.wait()
        try:
            # 先获取图片 URL，再下载到本地
            img_url = client.generate_image(
                prompt=prompt,
                size=IMAGE_SIZES["portrait"],
                reference_images=ref_images if ref_images else None,
                response_format="url",
            )
            # 下载到本地
            out_path.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(img_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            manifest[sid] = {"path": str(out_path), "url": img_url, "prompt": prompt}
            print(f"  ✅ {sid}")
        except Exception as e:
            print(f"  ❌ {sid} 失败：{e}")
            manifest[sid] = {"path": "", "prompt": prompt, "error": str(e)}

        # 镜头级 checkpoint
        cp.data[f"storyboard.{sid}"] = "done"
        cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

    manifest_path = sb_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("storyboard")
    print(f"  ✅ 分镜关键帧已保存：{sb_dir}")
    return manifest


# ===================== 步骤 4：图生视频 =====================

def generate_videos(client: AgnesClient, script: dict, sb_manifest: dict,
                    vid_dir: pathlib.Path, cp: Checkpoint,
                    rate_limiter: RateLimiter,
                    scene_duration: int = 5) -> dict:
    """用 agnes-video-v2.0 图生视频。"""

    if cp.is_done("videos"):
        print("✅ 视频已存在，跳过")
        return {}

    num_frames = SCENE_DURATION_MAP.get(scene_duration, 121)
    print(f"\n📹 步骤 4/5：图生视频（{len(script['scenes'])} 个镜头，每镜头 ~{scene_duration}s / {num_frames}帧）...")
    cp.mark_running("videos")

    manifest = {}

    for scene in script["scenes"]:
        sid = scene["id"]
        out_path = vid_dir / f"{sid}.mp4"

        # 镜头级 checkpoint
        if cp.data.get(f"videos.{sid}") == "done" and out_path.exists():
            print(f"  {sid} 已存在，跳过")
            manifest[sid] = str(out_path)
            continue

        # 检查关键帧是否存在
        frame_info = sb_manifest.get(sid, {})
        frame_url = frame_info.get("url", "")  # 优先用远程 URL
        frame_path = frame_info.get("path", "")
        if not frame_url and not (frame_path and pathlib.Path(frame_path).exists()):
            print(f"  ⚠️ {sid} 关键帧不存在，跳过视频生成")
            continue

        # 构建视频提示词
        video_prompt = f"{scene['action']}，{scene['camera']}，{scene['mood']}氛围，cinematic quality，smooth motion"

        # 图生视频：优先用 URL，否则用本地文件
        image_input = frame_url if frame_url else frame_path

        rate_limiter.wait()
        try:
            client.generate_video_full(
                prompt=video_prompt,
                out_path=out_path,
                image=image_input,
                height=1344,
                width=768,
                num_frames=num_frames,
                frame_rate=24,
            )
            manifest[sid] = str(out_path)
            cp.data[f"videos.{sid}"] = "done"
            cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))
            print(f"  ✅ {sid}")
        except Exception as e:
            print(f"  ❌ {sid} 失败：{e}")

    cp.mark_done("videos")
    print(f"  ✅ 视频已保存：{vid_dir}")
    return manifest


# ===================== 步骤 5：成片拼接 =====================

def edit_final(project_dir: pathlib.Path, script: dict, vid_manifest: dict,
               cp: Checkpoint, scene_duration: int = 5) -> pathlib.Path | None:
    """用 ffmpeg 拼接视频 + 转场 + 烧字幕。"""

    if cp.is_done("edit"):
        final = project_dir / "final.mp4"
        if final.exists():
            print(f"✅ 成片已存在：{final}")
            return final

    print(f"\n🎬 步骤 5/5：成片拼接...")

    vid_dir = project_dir / "videos"
    final = project_dir / "final.mp4"

    # 收集按顺序排列的视频文件
    video_files = []
    for scene in script["scenes"]:
        sid = scene["id"]
        vid_path = vid_dir / f"{sid}.mp4"
        if vid_path.exists():
            video_files.append(vid_path)
        else:
            print(f"  ⚠️ {sid}.mp4 不存在，跳过")

    if not video_files:
        print("  ❌ 没有可用的视频文件")
        return None

    # 多镜头时：加 xfade 转场拼接
    if len(video_files) == 1:
        # 单镜头，直接复制
        import shutil
        shutil.copy2(video_files[0], final)
    else:
        # 构建 xfade 滤镜链
        transition_duration = 0.5  # 转场时长 0.5s
        n = len(video_files)

        # 输入
        inputs = []
        for vf in video_files:
            inputs += ["-i", str(vf)]

        # 构建 xfade 滤镜链
        # 获取每个视频的实际时长
        durations = []
        for vf in video_files:
            dur = get_video_duration(vf)
            durations.append(dur)

        # xfade 链：[0][1]xfade=transition=fade:duration=T:offset=O[v01]; [v01][2]xfade=...
        filter_parts = []
        offset = durations[0] - transition_duration
        for i in range(n - 1):
            if i == 0:
                in_a = f"[{i}:v]"
                in_b = f"[{i+1}:v]"
            else:
                in_a = f"[v{i-1}{i}]"
                in_b = f"[{i+1}:v]"

            out_label = f"[v{i}{i+1}]" if i < n - 2 else "[vout]"

            trans_type = "fade"  # 可扩展：fade/slideleft/dissolve/wipeleft 等
            filter_parts.append(f"{in_a}{in_b}xfade=transition={trans_type}:duration={transition_duration}:offset={offset}{out_label}")

            if i < n - 2:
                offset += durations[i + 1] - transition_duration

        vfilter = ";".join(filter_parts)

        cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", vfilter, "-map", "[vout]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "23",
               "-an", str(final)]

        print(f"  拼接 {n} 个镜头，转场：fade（{transition_duration}s）...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                print(f"  ⚠️ xfade 拼接失败：{result.stderr[:300]}")
                print(f"  回退到简单拼接...")
                _simple_concat(video_files, project_dir, final)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"  ⚠️ xfade 拼接出错：{e}，回退简单拼接...")
            _simple_concat(video_files, project_dir, final)

    # 烧字幕
    srt_path = project_dir / "subtitle.srt"
    generate_srt(script, srt_path, scene_duration=scene_duration)

    if srt_path.exists() and srt_path.stat().st_size > 0 and final.exists():
        final_sub = project_dir / "final_with_sub.mp4"
        sub_cmd = ["ffmpeg", "-y", "-i", str(final),
                   "-vf", f"subtitles={srt_path}",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                   "-c:a", "copy", str(final_sub)]
        try:
            result = subprocess.run(sub_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0 and final_sub.exists():
                # 替换 final
                final.unlink()
                final_sub.rename(final)
                print(f"  ✅ 字幕已烧录")
            else:
                final_sub.unlink(missing_ok=True)
        except Exception:
            final_sub.unlink(missing_ok=True)

    if not final.exists():
        return None

    cp.mark_done("edit")
    print(f"  ✅ 成片已保存：{final}")
    return final


def _simple_concat(video_files: list, project_dir: pathlib.Path, final: pathlib.Path):
    """简单拼接（无转场）作为 fallback。"""
    concat_file = project_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for vf in video_files:
            f.write(f"file '{vf}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
           "-i", str(concat_file),
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", str(final)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def get_video_duration(path: pathlib.Path) -> float:
    """用 ffprobe 获取视频时长。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 5.0  # fallback


def generate_srt(script: dict, out_path: pathlib.Path, scene_duration: int = 5):
    """从剧本生成 SRT 字幕。"""
    lines = []
    idx = 1
    current_time = 0.0

    for scene in script["scenes"]:
        sd = scene_duration
        dialogues = scene.get("dialogue", [])
        if not dialogues:
            current_time += sd
            continue

        # 对白均匀分布在镜头时间中
        per_dialogue = sd / max(len(dialogues), 1)

        for d in dialogues:
            char_name = d.get("character", "")
            text = d.get("text", "")
            # 查找角色名
            for c in script.get("characters", []):
                if c["id"] == char_name:
                    char_name = c["name"]
                    break

            start = current_time
            end = current_time + per_dialogue

            start_srt = format_srt_time(start)
            end_srt = format_srt_time(end)

            lines.append(f"{idx}")
            lines.append(f"{start_srt} --> {end_srt}")
            lines.append(f"{char_name}：{text}")
            lines.append("")

            idx += 1
            current_time = end

        # 如果还有剩余时间
        remaining = sd - (current_time - (start - per_dialogue * (len(dialogues) - 1)))
        if remaining > 0:
            current_time += remaining

    out_path.write_text("\n".join(lines), encoding="utf-8")


def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ===================== TTS 配音 =====================

# edge-tts 语音映射
EDGE_TTS_VOICES = {
    "male": [
        "zh-CN-YunxiNeural",      # 阳光男声（首选）
        "zh-CN-YunjianNeural",    # 沉稳男声
        "zh-CN-YunyangNeural",    # 新闻男声
    ],
    "female": [
        "zh-CN-XiaoxiaoNeural",   # 温柔女声（首选）
        "zh-CN-XiaoyiNeural",     # 甜美女声
        "zh-CN-XiaohanNeural",    # 知性女声
    ],
}


def _run_edge_tts(text: str, voice: str, out_path: pathlib.Path) -> bool:
    """用 edge-tts Python API 生成 TTS（跨平台，无需系统依赖）。"""
    import asyncio
    try:
        import edge_tts
    except ImportError:
        return False

    async def _generate():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))

    try:
        # 检测是否有运行中的事件循环
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 在已有事件循环中，用 nest_asyncio 或新建线程
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _generate())
                future.result(timeout=60)
        else:
            asyncio.run(_generate())
        return out_path.exists()
    except Exception as e:
        print(f"    edge-tts 错误：{e}")
        return False


def _run_say_tts(text: str, voice: str, out_path: pathlib.Path) -> bool:
    """用 macOS say 生成 TTS（仅 macOS 可用，作为回退）。"""
    try:
        aiff_path = out_path.with_suffix(".aiff")
        subprocess.run(["say", "-v", voice, "-o", str(aiff_path), text],
                       capture_output=True, timeout=30)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(aiff_path),
             "-ar", "24000", "-b:a", "64k", str(out_path)],
            capture_output=True, timeout=30)
        aiff_path.unlink(missing_ok=True)
        return out_path.exists()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    except Exception as e:
        print(f"    macOS say 错误：{e}")
        return False


def generate_tts(script: dict, audio_dir: pathlib.Path, cp: Checkpoint,
                 scene_duration: int = 5) -> dict:
    """为剧本对白生成 TTS 音频。优先 edge-tts（跨平台），回退到 macOS say。"""

    if cp.is_done("tts"):
        print("✅ TTS 已存在，跳过")
        manifest_path = audio_dir / "tts_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🎙️ 步骤 6：生成 TTS 配音...")
    cp.mark_running("tts")
    audio_dir.mkdir(parents=True, exist_ok=True)

    # 检测 TTS 引擎优先级：edge-tts > macOS say
    tts_engine = None
    try:
        import edge_tts  # noqa: F401
        tts_engine = "edge-tts"
        print("  使用 edge-tts 引擎（跨平台）")
    except ImportError:
        if sys.platform == "darwin":
            try:
                subprocess.run(["say", "-v", "?"], capture_output=True, timeout=5)
                tts_engine = "say"
                print("  使用 macOS say 引擎（回退）")
            except FileNotFoundError:
                pass
        if not tts_engine:
            print("  ⚠️ 未找到 TTS 引擎，尝试自动安装 edge-tts...")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "edge-tts", "-q"],
                    capture_output=True, timeout=60)
                import edge_tts  # noqa: F401
                tts_engine = "edge-tts"
                print("  ✅ edge-tts 自动安装成功")
            except Exception:
                print("  ❌ 无法安装 edge-tts，跳过配音")
                cp.mark_done("tts")
                return {}

    manifest = {}
    idx = 0

    for scene_idx, scene in enumerate(script["scenes"]):
        sid = scene["id"]
        dialogues = scene.get("dialogue", [])
        if not dialogues:
            continue

        per_dialogue = scene_duration / max(len(dialogues), 1)

        for d_idx, d in enumerate(dialogues):
            char_id = d.get("character", "")
            text = d.get("text", "")
            if not text:
                continue

            # 查找角色信息
            char_name = char_id
            char_gender = "male"  # 默认
            for c in script.get("characters", []):
                if c["id"] == char_id:
                    char_name = c["name"]
                    visual = c.get("visual", "").lower()
                    if any(w in visual for w in ["女", "娘", "姑", "妃", "姬", "婉", "柔"]):
                        char_gender = "female"
                    break

            out_path = audio_dir / f"tts_{sid}_{d_idx:02d}.mp3"

            # 计算时间戳
            start_time = scene_idx * scene_duration + d_idx * per_dialogue

            # 选择语音
            if tts_engine == "edge-tts":
                voice = EDGE_TTS_VOICES.get(char_gender, EDGE_TTS_VOICES["male"])[0]
                success = _run_edge_tts(text, voice, out_path)
            elif tts_engine == "say":
                voice = "Ting-Ting" if char_gender == "female" else "Li-Mu"
                success = _run_say_tts(text, voice, out_path)
            else:
                success = False

            # edge-tts 失败时尝试 say 回退
            if not success and tts_engine == "edge-tts" and sys.platform == "darwin":
                voice = "Ting-Ting" if char_gender == "female" else "Li-Mu"
                success = _run_say_tts(text, voice, out_path)
                if success:
                    print(f"    （回退到 macOS say）")

            if success and out_path.exists():
                manifest[f"{sid}_{d_idx}"] = {
                    "path": str(out_path),
                    "character": char_name,
                    "text": text,
                    "start": start_time,
                    "duration": per_dialogue,
                }
                print(f"  ✅ {char_name}: {text[:20]}...")
                idx += 1
            else:
                print(f"  ❌ TTS 失败 {char_name}: {text[:20]}...")

    manifest_path = audio_dir / "tts_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("tts")
    print(f"  ✅ TTS 已保存：{audio_dir}（共 {idx} 条）")
    return manifest


# ===================== 音效生成 =====================

def generate_sfx(client: AgnesClient, script: dict, sfx_dir: pathlib.Path,
                 cp: Checkpoint, rate_limiter: RateLimiter) -> dict:
    """为场景生成音效描述（文本），后续可用免费音效库匹配。"""

    if cp.is_done("sfx"):
        print("✅ 音效已存在，跳过")
        manifest_path = sfx_dir / "sfx_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🔊 步骤 7：生成音效描述...")
    cp.mark_running("sfx")

    manifest = {}

    for scene in script["scenes"]:
        sid = scene["id"]
        action = scene.get("action", "")
        mood = scene.get("mood", "")
        location = scene.get("location", "")

        # 用 AI 分析场景，输出音效描述
        prompt = f"""分析以下漫剧场景，输出适合的音效描述（英文关键词，用于搜索免费音效库）。

场景：{location}
动作：{action}
氛围：{mood}

输出 JSON 格式：
{{
  "ambient": "环境音描述（如：wind, forest, peaceful）",
  "actions": ["动作音效1", "动作音效2"],
  "mood": "氛围音效（如：tension, mysterious）"
}}

只输出 JSON，不要其他文字。"""

        try:
            rate_limiter.wait()
            result = client.chat(
                messages=[{"role": "user", "content": prompt}],
                model="agnes-1.5-flash",  # 用轻量模型
                temperature=0.7,
                max_tokens=512,
            )

            # 提取 JSON
            json_str = result
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            sfx_desc = json.loads(json_str.strip())

            # 保存描述
            manifest[sid] = {
                "description": sfx_desc,
                "keywords": " ".join([
                    sfx_desc.get("ambient", ""),
                    " ".join(sfx_desc.get("actions", [])),
                    sfx_desc.get("mood", "")
                ]).strip(),
            }

            print(f"  ✅ {sid}: {manifest[sid]['keywords'][:50]}...")

        except Exception as e:
            print(f"  ⚠️ {sid} 音效分析失败：{e}")
            manifest[sid] = {"description": {}, "keywords": ""}

    manifest_path = sfx_dir / "sfx_manifest.json"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("sfx")
    print(f"  ✅ 音效描述已保存：{sfx_dir}")
    return manifest


# ===================== 音频混音 =====================

def mix_audio(project_dir: pathlib.Path, script: dict,
              tts_manifest: dict, sfx_manifest: dict,
              cp: Checkpoint, scene_duration: int = 5) -> pathlib.Path | None:
    """混合视频 + TTS 配音 + 音效（可选占位）。"""

    if cp.is_done("mix"):
        final = project_dir / "final_with_audio.mp4"
        if final.exists():
            print(f"✅ 混音成片已存在：{final}")
            return final

    print(f"\n🎚️ 步骤 8：音频混音...")
    cp.mark_running("mix")

    video_path = project_dir / "final.mp4"
    if not video_path.exists():
        print("  ❌ 视频文件不存在")
        return None

    # 构建音频轨道
    audio_tracks = []

    # 1. TTS 轨道（按时间对齐）
    if tts_manifest:
        print(f"  合成 {len(tts_manifest)} 条 TTS...")

        # 创建 concat 列表，按时间排序
        tts_items = sorted(tts_manifest.items(), key=lambda x: x[1]["start"])

        # 生成静音填充 + TTS 的复杂滤镜
        filter_parts = []
        inputs = []
        input_idx = 0

        for key, info in tts_items:
            tts_path = pathlib.Path(info["path"])
            if not tts_path.exists():
                continue

            start = info["start"]
            # 添加输入
            inputs += ["-i", str(tts_path)]
            # adelay 滤镜
            delay_ms = int(start * 1000)
            filter_parts.append(f"[{input_idx}:a]adelay={delay_ms}|{delay_ms}[a{input_idx}]")
            input_idx += 1

        if filter_parts:
            # 混合所有音频
            n = input_idx
            amix_inputs = "".join([f"[a{i}]" for i in range(n)])
            filter_complex = ";".join(filter_parts) + f";{amix_inputs}amix=inputs={n}:duration=longest[aout]"

            tts_mixed = project_dir / "tts_mixed.m4a"
            cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_complex, "-map", "[aout]",
                   "-c:a", "aac", "-b:a", "128k", str(tts_mixed)]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and tts_mixed.exists():
                    audio_tracks.append(("tts", tts_mixed))
                    print(f"  ✅ TTS 轨道合成完成")
                else:
                    print(f"  ⚠️ TTS 混音失败：{result.stderr[:200]}")
            except Exception as e:
                print(f"  ⚠️ TTS 混音出错：{e}")

    # 2. 混合视频 + 音频
    final_video = project_dir / "final.mp4"
    final_audio = project_dir / "final_with_audio.mp4"

    if audio_tracks:
        # 有音频轨道，混合
        cmd = ["ffmpeg", "-y", "-i", str(final_video)]
        for _, audio_path in audio_tracks:
            cmd += ["-i", str(audio_path)]

        # 简单混合：视频 + 所有音频
        if len(audio_tracks) == 1:
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(final_audio)]
        else:
            # 多音频混合
            n_audio = len(audio_tracks)
            amix = "".join([f"[{i+1}:a]" for i in range(n_audio)])
            filter_complex = f"{amix}amix=inputs={n_audio}:duration=first[aout]"
            cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", str(final_audio)]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode == 0 and final_audio.exists():
                print(f"  ✅ 混音完成：{final_audio}")
                cp.mark_done("mix")
                return final_audio
            else:
                print(f"  ⚠️ 混音失败：{result.stderr[:200]}")
        except Exception as e:
            print(f"  ⚠️ 混音出错：{e}")

    print("  ⚠️ 无音频可混合，返回原视频")
    return final_video


# ===================== 主流程 =====================

def main():
    parser = argparse.ArgumentParser(description="Agnes 漫剧生成器")
    parser.add_argument("--theme", required=True, help="主题")
    parser.add_argument("--duration", type=int, default=180, help="总时长（秒）")
    parser.add_argument("--style", default="三渲二国风", help="风格")
    parser.add_argument("--genre", default="仙侠", help="类型")
    parser.add_argument("--scene-duration", type=int, default=5, help="单镜头秒数")
    parser.add_argument("--output", default=None, help="输出目录")
    parser.add_argument("--no-tts", action="store_true", help="禁用 TTS 配音")
    parser.add_argument("--no-sfx", action="store_true", help="禁用音效")
    args = parser.parse_args()

    n_scenes = args.duration // args.scene_duration
    slug = re.sub(r"[^\w]", "-", args.theme)[:30]

    if args.output:
        project_dir = pathlib.Path(args.output)
    else:
        project_dir = pathlib.Path(f"output/{slug}")

    project_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"🎭 Agnes 漫剧生成器")
    print(f"  主题：{args.theme}")
    print(f"  风格：{args.style} / 类型：{args.genre}")
    print(f"  时长：{args.duration}s / 镜头：{n_scenes}")
    print(f"  输出：{project_dir}")
    print(f"  💰 全流程免费（Agnes AI）")
    print("=" * 60)

    client = AgnesClient()
    cp = Checkpoint(project_dir)
    rl = RateLimiter(rpm=18)

    # Step 1: 剧本
    script = generate_script(
        client, args.theme, args.style, args.genre,
        n_scenes, args.scene_duration, rl,
        project_dir / "script.json", cp,
    )

    # Step 2: 角色卡
    char_manifest = generate_characters(
        client, script, args.style,
        project_dir / "characters", cp, rl,
    )

    # Step 3: 分镜关键帧
    sb_manifest = generate_storyboard(
        client, script, args.style, char_manifest,
        project_dir / "storyboard", cp, rl,
    )

    # Step 4: 图生视频
    vid_manifest = generate_videos(
        client, script, sb_manifest,
        project_dir / "videos", cp, rl,
        scene_duration=args.scene_duration,
    )

    # Step 5: 成片拼接
    final = edit_final(project_dir, script, vid_manifest, cp,
                       scene_duration=args.scene_duration)

    # Step 6: TTS 配音
    tts_manifest = {}
    if not args.no_tts:
        tts_manifest = generate_tts(
            script, project_dir / "audio", cp,
            scene_duration=args.scene_duration,
        )
    else:
        print("\n🎙️ TTS 配音已禁用")

    # Step 7: 音效描述
    sfx_manifest = {}
    if not args.no_sfx:
        sfx_manifest = generate_sfx(
            client, script, project_dir / "sfx", cp, rl,
        )
    else:
        print("\n🔊 音效已禁用")

    # Step 8: 音频混音
    final_with_audio = None
    if tts_manifest or sfx_manifest:
        final_with_audio = mix_audio(
            project_dir, script, tts_manifest, sfx_manifest, cp,
            scene_duration=args.scene_duration,
        )

    if final_with_audio:
        print(f"\n🎉 漫剧生成完成！")
        print(f"  📁 成片（含配音）：{final_with_audio}")
        print(f"  📁 成片（静音）：{final}")
        print(f"  💰 成本：¥0.00（Agnes AI 免费额度）")
    elif final:
        print(f"\n🎉 漫剧生成完成！")
        print(f"  📁 成片：{final}")
        print(f"  💰 成本：¥0.00（Agnes AI 免费额度）")
    else:
        print(f"\n⚠️ 部分步骤未完成，请检查输出目录：{project_dir}")


if __name__ == "__main__":
    main()
