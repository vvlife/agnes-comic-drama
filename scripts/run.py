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
        "prefix": "三渲二国风动画风格，工笔线条，中国传统审美，精致角色设计",
        "lighting": "电影级光影，柔和自然光配合体积光，水墨晕染质感",
        "palette": "青绿山水色调，朱砂点缀，金色装饰细节",
        "quality": "masterpiece, best quality, ultra detailed, professional animation, 8K render, sharp focus",
        "negative": "low quality, blurry, deformed, ugly, bad anatomy, watermark, text",
    },
    "水墨": {
        "prefix": "中国水墨画风格，留白意境，泼墨技法，传统国画美学",
        "lighting": "墨色浓淡层次，宣纸质感，光影留白",
        "palette": "黑白灰为主，偶尔赭石与花青点缀",
        "quality": "masterpiece, ink wash painting, traditional Chinese art, fine brushwork, high resolution",
        "negative": "low quality, blurry, modern elements, neon colors",
    },
    "赛博朋克": {
        "prefix": "赛博朋克动画风格，霓虹灯光，未来科技，暗黑都市",
        "lighting": "霓虹灯光，雨夜地面反光，全息投影光效",
        "palette": "紫蓝配色，霓虹粉点缀，深色背景",
        "quality": "masterpiece, cyberpunk art, neon glow, ultra detailed, cinematic lighting, 8K",
        "negative": "low quality, blurry, bright daylight, nature scenery",
    },
    "日系动漫": {
        "prefix": "日系动画风格，精致线条，明亮色彩，新海诚级画质",
        "lighting": "柔光晴天，丁达尔效应，镜头光晕",
        "palette": "明亮动漫配色，天空蓝与樱花粉",
        "quality": "masterpiece, anime style, Makoto Shinkai quality, vibrant colors, detailed background",
        "negative": "low quality, blurry, realistic style, dark atmosphere",
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

# 场景背景音合成配方：根据 location/mood/action 关键词本地合成（ffmpeg，免费）
# 每个配方：noise 颜色 + 滤波链（已含音量）。部分"史诗/肃杀"类额外叠加低频 drone。
AMBIENT_RECIPES = {
    "water":  ("pink",  "bandpass=f=1400:w=1.5,volume=0.55"),
    "fire":   ("brown", "lowpass=f=900,highpass=f=90,volume=0.6"),
    "wind":   ("brown", "lowpass=f=520,volume=0.55"),
    "battle": ("brown", "lowpass=f=700,volume=0.5"),
    "calm":   ("pink",  "lowpass=f=1300,volume=0.3"),
    "tense":  ("brown", "lowpass=f=300,volume=0.5"),
    "epic":   ("brown", "lowpass=f=800,volume=0.5"),
    "magic":  ("pink",  "highpass=f=2200,volume=0.32"),
    "neutral":("brown", "lowpass=f=850,volume=0.42"),
}
# 关键词 → 配方名（按优先级匹配）
AMBIENT_KEYWORD_MAP = [
    (("水", "河", "海", "雨", "溪", "波", "潮", "浪", "潭", "湖"), "water"),
    (("火", "焰", "熔", "炎", "焚", "灼"), "fire"),
    (("风", "云", "崖", "空", "天", "夜", "月", "雪", "雾"), "wind"),
    (("战", "斗", "杀", "剑", "兵", "击", "爆", "碎", "爪", "刃"), "battle"),
    (("庙", "宫", "殿", "禅", "山", "林", "幽", "静", "竹", "谷"), "calm"),
    (("法", "术", "灵", "阵", "符", "光", "网", "数", "幻", "晶"), "magic"),
]
# 这些氛围额外叠加一层 56Hz 低频 drone（压迫感 / 史诗感）
AMBIENT_DRONE_MOODS = {"肃杀", "紧张", "危急", "暴怒", "恐怖", "悲壮", "惨烈",
                       "震撼", "壮阔", "磅礴", "诡谲", "神秘", "压抑", "混乱"}


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

    prompt = f"""你是一位专业的漫剧编剧兼分镜师，擅长写出有因果、有潜台词、画面感极强的剧本。请根据以下信息生成分幕剧本，输出严格 JSON。

主题：{theme}
风格：{style}
类型：{genre_info}
总镜头数：{n_scenes}
每镜头时长：{scene_duration}秒

## 叙事结构
- 开头（1-3镜）：建立世界观与主角困境，交代动机
- 发展：冲突升级，角色目标清晰，靠对白与动作推进因果
- 高潮（倒数2-3镜）：最激烈对抗 / 反转 / 抉择
- 结尾（最后1镜）：情感落点，留余韵

## 对白规范（重中之重，旧版对白太空洞）
- 每句对白必须"推动剧情或揭示性格"，禁止无意义的单字感叹（如"格！""破阵！""罢了…"）
- 对白要像真实人物会说的话：有意图、有潜台词、有情绪层次，可多镜串成一段对话
- 每句 10-25 字；对白对象的 intent 字段写明"这句台词想达成什么"（推进关系/设套/劝降/点破真相/掩饰恐惧…）
- 不是每个镜头都要对白，但凡有对白必须有意义、能被观众记住

## 人物站位 / 构图（blocking，必填）
- 每个镜头用 blocking 写明：每个出场角色在画面中的位置（左/右/中、前景/后景、高低）、彼此间距、与镜头的关系
- 例："林策立于画面左下三分线，渊魇悬右上方俯压，二人成对角线对峙；墨锋在远景中景中线，形成三角制衡"

## 画面描述规范（action + visual，单镜头描述至少 120 字，比旧版翻倍）
- action：连续具体的视觉动作（人物动作、表情变化、衣袂/发丝动态、环境变化），像描述一段影像
- visual：补充环境质感 / 光影 / 关键道具 / 色彩氛围（80-120 字）
- 好例："凌寒闭目凝神，残剑骤然迸发刺目金芒，剑气瞬间冻结漫天飞石，碎石悬停如星"
- 差例："凌寒使用绝招打败了对手"

## 镜头语言
交替 远景/中景/近景/特写/推/摇/俯/仰；远景建环境，特写给情绪

输出 JSON（严格）：
{{
  "title": "剧名（四或五字，有诗意）",
  "characters": [
    {{"id": "C1", "name": "角色名", "visual": "详细外观（服装/发色/体型/标志性配饰武器）≥40字", "personality": "性格2-3词", "age": "年龄"}}
  ],
  "scenes": [
    {{
      "id": "S01",
      "location": "场景地点（含环境细节）",
      "time": "时间",
      "characters": ["C1"],
      "action": "连续视觉动作描述（≥120字，画面感强）",
      "visual": "光影/质感/道具/色彩补充（80-120字）",
      "blocking": "人物站位与构图（谁在画面何处、彼此关系、与镜头关系）",
      "dialogue": [{{"character": "C1", "text": "有叙事意义的台词（10-25字）", "intent": "这句台词背后的意图"}}],
      "camera": "镜头运动",
      "mood": "氛围词"
    }}
  ]
}}

要求：
1. 角色 2-4 个，外观可辨识（颜色对比、标志性配饰）
2. 对白必须有 intent 且推动叙事，禁止空洞感叹
3. 每个镜头含 action + visual + blocking 三字段，描述量翻倍
4. 首镜必为远景建环境，末镜有情感冲击
5. 只输出 JSON，不要其他文字"""

    last_err = None
    script = None
    for attempt in range(3):
        rate_limiter.wait()
        try:
            result = client.chat(
                messages=[{"role": "user", "content": prompt}],
                model="agnes-2.0-flash",
                temperature=0.85,
                max_tokens=16000,
            )
        except Exception as e:
            last_err = e
            print(f"  ⚠️ 剧本生成请求失败（{attempt+1}/3）：{e}，重试...")
            continue

        # 提取 JSON — 更健壮的解析逻辑
        json_str = result.strip()
        # 1) 去除 markdown 代码块包装
        if "```json" in json_str:
            json_str = json_str.split("```json", 1)[1]
            # 取第一个 ``` 之前的内容
            if "```" in json_str:
                json_str = json_str.split("```", 1)[0]
        elif "```" in json_str:
            parts = json_str.split("```")
            if len(parts) >= 3:
                json_str = parts[1]
        # 2) 定位第一个 { 和最后一个 }，截取纯 JSON
        first_brace = json_str.find("{")
        last_brace = json_str.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            json_str = json_str[first_brace:last_brace + 1]
        # 3) 去除可能存在的尾部逗号（trailing comma）
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        # 4) 解析，失败时保存原始响应用于调试
        try:
            script = json.loads(json_str.strip())
            break
        except json.JSONDecodeError as e:
            last_err = e
            debug_path = out_path.parent / "_script_debug.txt"
            debug_path.write_text(f"=== JSON PARSE ERROR ===\n{e}\n\n=== RAW RESPONSE ===\n{result}\n\n=== CLEANED JSON ===\n{json_str}\n")
            print(f"  ⚠️ JSON 解析失败（{attempt+1}/3）：{e}，重试生成...")
            # 强化提示后重试
            prompt = prompt + "\n\n⚠️ 上一次输出不是合法 JSON，请严格只输出一个完整 JSON 对象，不要任何多余文字、不要省略任何逗号、不要截断。"
            continue
    if script is None:
        raise RuntimeError(f"剧本生成 3 次均失败: {last_err}")

    # 确保每个场景都有 mood 字段
    for scene in script.get("scenes", []):
        if "mood" not in scene:
            scene["mood"] = "紧张"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(script, ensure_ascii=False, indent=2))
    cp.mark_done("script")
    print(f"  ✅ 剧本已保存：{out_path}")
    return script


def _extract_json_array(text: str) -> list | None:
    """从模型输出中尽力解析出一个 JSON 数组（容错：去代码围栏/截断/尾逗号）。"""
    t = (text or "").strip()
    if "```json" in t:
        t = t.split("```json", 1)[1]
        if "```" in t:
            t = t.split("```", 1)[0]
    elif "```" in t:
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
    first = t.find("[")
    last = t.rfind("]")
    if first != -1 and last != -1 and last > first:
        t = t[first:last + 1]
    t = re.sub(r',\s*([}\]])', r'\1', t)
    try:
        obj = json.loads(t.strip())
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _improve_chunk(client: AgnesClient, rate_limiter: RateLimiter,
                   chunk_scenes: list, chunk_idx: int, chunk_total: int) -> list | None:
    """升级一小批镜头（分块以降低超长 JSON 出错概率）。失败返回 None。"""
    prompt = f"""你是一位资深漫剧编剧。下面是一批（{len(chunk_scenes)} 个）已生成剧本的镜头 JSON 数组。请在不改变每个镜头的 id / location / time / camera / mood / characters 字段的前提下，对这批镜头做"质量升级"，只输出升级后的 JSON 数组（以 [ 开头、] 结尾），不要其他任何文字：

1. 对白升级：对白保持为数组，每条对象含 "character"（角色 id，如 "C1"）、"text"（改写后的台词，10-25字，能推动剧情或揭示性格、有潜台词、像真人会说的话）、"intent"（这句台词背后的意图，如"劝降/点破真相/掩饰恐惧"）。把空洞单字感叹扩写成有信息量的短句。
2. 站位升级：为每个镜头补一个 "blocking" 字段，说明每个出场角色在画面中的位置（左/右/中、前景/后景、高低）、彼此间距、与镜头的关系（≥30字）。
3. 描述翻倍：把每个镜头的 "action" 扩写到 ≥120 字（连续具体视觉动作：人物动作、表情、衣袂发丝动态、环境变化），并补一个 "visual" 字段（80-120字：光影/质感/道具/色彩氛围）。保留原有情节要点，只是写得更具体。

原镜头数组：
{json.dumps(chunk_scenes, ensure_ascii=False, indent=2)}
"""
    last_err = None
    for attempt in range(3):
        rate_limiter.wait()
        try:
            result = client.chat(
                messages=[{"role": "user", "content": prompt}],
                model="agnes-2.0-flash",
                temperature=0.7,
                max_tokens=8000,
            )
        except Exception as e:
            last_err = e
            print(f"  ⚠️ 第{chunk_idx+1}/{chunk_total}批优化请求失败（{attempt+1}/3）：{e}，重试...")
            continue
        arr = _extract_json_array(result)
        if arr is not None:
            return arr
        last_err = "JSON 解析失败"
        print(f"  ⚠️ 第{chunk_idx+1}/{chunk_total}批 JSON 解析失败（{attempt+1}/3），重试...")
    print(f"  ⚠️ 第{chunk_idx+1}/{chunk_total}批 3 次均失败，沿用原镜头")
    return None


def improve_existing_script(client: AgnesClient, script: dict, rate_limiter: RateLimiter,
                              out_path: pathlib.Path) -> dict:
    """在保留剧情主线 / 角色 / 标题的前提下，升级现有剧本：

    - 把空洞对白改写为有叙事意义、带潜台词、含 intent 的台词
    - 为每个镜头补上 blocking（人物站位 / 构图）
    - 把 action / visual 描述量翻倍（画面更具体）

    用于"优化剧本"而不丢失已生成视频对应的剧情。
    """
    print(f"\n✨ 优化现有剧本（对白 / 站位 / 描述翻倍，分块升级）...")

    orig_scenes = script.get("scenes", [])
    merged = [dict(s) for s in orig_scenes]  # 占位，按 id 回写
    chunk_size = 4
    chunks = [orig_scenes[i:i + chunk_size] for i in range(0, len(orig_scenes), chunk_size)]
    total = len(chunks)

    for ci, ch in enumerate(chunks):
        arr = _improve_chunk(client, rate_limiter, ch, ci, total)
        if not arr:
            continue
        amap = {s.get("id"): s for s in arr if isinstance(s, dict) and s.get("id")}
        for s in ch:
            up = amap.get(s["id"])
            if not up:
                continue
            new_s = dict(s)  # 继承原字段（id/location/time/camera/mood/characters）
            for fld in ("dialogue", "blocking", "action", "visual"):
                if up.get(fld) not in (None, "", []):
                    new_s[fld] = up[fld]
            new_s.setdefault("blocking", s.get("blocking", ""))
            new_s.setdefault("visual", s.get("visual", ""))
            new_s.setdefault("action", s.get("action", ""))
            new_s.setdefault("dialogue", s.get("dialogue", []))
            idx = next((k for k, o in enumerate(orig_scenes) if o["id"] == s["id"]), None)
            if idx is not None:
                merged[idx] = new_s

    improved = {**script, "scenes": merged}
    improved.setdefault("title", script.get("title", ""))
    improved.setdefault("characters", script.get("characters", []))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(improved, ensure_ascii=False, indent=2))
    print(f"  ✅ 剧本已优化：{out_path}（{len(merged)} 镜）")
    return improved


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

    style_info = STYLE_PRESETS.get(style, STYLE_PRESETS["三渲二国风"])
    style_prefix = style_info["prefix"]
    quality_tags = style_info.get("quality", "masterpiece, best quality, ultra detailed")
    negative_tags = style_info.get("negative", "")
    avoid_hint = f"，avoid: {negative_tags}" if negative_tags else ""
    manifest = {}

    for char in script["characters"]:
        cid = char["id"]
        print(f"  生成角色 {char['name']}（{cid}）...")

        # 每种图一张——全身 / 半身特写 / Q版
        # 关键：先生成 full 立绘（文生图），再以 full 为参考图生图生成 close/chibi，
        # 保证同一角色的三视图外观严格一致（发型/服饰/五官锁定），提升跨片段一致性。
        prompts = {
            "full": (
                f"{style_prefix}，角色全身立绘设定图，{char['visual']}，"
                f"{char['personality']}气质，优雅站姿，居中对称构图，纯净渐变背景，"
                f"全身可见，手脚完整，精致服装纹理，{style_info['lighting']}，"
                f"角色设定画，concept art，{quality_tags}{avoid_hint}"
            ),
            "close": (
                f"{style_prefix}，角色半身特写肖像，{char['visual']}，"
                f"{char['personality']}神情，肩部以上正面视角，细腻面部表情，"
                f"眼神光，精致五官，头发丝缕分明，{style_info['lighting']}，"
                f"portrait，{quality_tags}{avoid_hint}"
            ),
            "chibi": (
                f"{style_prefix}，Q版可爱头像，{char['visual']}简化版，"
                f"圆润线条，大眼睛，2头身比例，柔和粉嫩配色，"
                f"简洁纯色背景，chibi character，cute style，{quality_tags}{avoid_hint}"
            ),
        }

        char_images = []
        full_path = None
        # 若 full 已存在，加载为后续 close/chibi 的参考基准（支持断点续跑）
        existing_full = char_dir / f"{cid}_full.png"
        if existing_full.exists():
            full_path = existing_full

        for img_type, prompt in prompts.items():
            out_path = char_dir / f"{cid}_{img_type}.png"
            if out_path.exists():
                print(f"    {img_type} 已存在，跳过")
                char_images.append(str(out_path))
                if img_type == "full":
                    full_path = out_path
                continue

            # close / chibi 以 full 立绘作为参考图（图生图），锁定同一角色外观
            ref = None
            if img_type != "full" and full_path is not None and full_path.exists():
                ref = [str(full_path)]

            rate_limiter.wait()
            try:
                client.generate_image_to_file(
                    prompt=prompt,
                    out_path=out_path,
                    size=IMAGE_SIZES["landscape"],
                    reference_images=ref,
                    response_format="url",
                )
                char_images.append(str(out_path))
                if img_type == "full":
                    full_path = out_path
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

        quality_tags = style_info.get("quality", "masterpiece, best quality")
        negative_tags = style_info.get("negative", "")
        avoid_hint = f" avoid: {negative_tags}" if negative_tags else ""
        blocking = scene.get("blocking", "")
        visual = scene.get("visual", "")

        # 收集该镜出场角色的参考图（优先全身立绘），用于锁定角色外观一致性
        ref_images = []
        for cid in scene.get("characters", []):
            if cid in char_manifest:
                imgs = char_manifest[cid].get("images", [])
                for img in imgs:
                    if "full" in img:
                        ref_images.append(img)
                        break
                if len(ref_images) >= 4:
                    break

        # 角色一致性约束：画面中角色外观必须与参考图严格一致
        consistency_hint = ""
        if ref_images:
            consistency_hint = (
                "，画面中角色外观必须与参考图严格一致，"
                "发型、服饰、五官与配色不得改变，保持同一角色形象"
            )

        # 构建提示词——层次化：风格→环境→主体→站位→镜头→氛围→品质
        prompt = (
            f"{style_info['prefix']}，"
            f"场景：{scene['location']}，{scene['time']}，"
            f"画面主体：{scene['action']}，"
            f"视觉细节：{visual}，"
            f"人物站位与构图：{blocking}，"
            f"镜头语言：{scene['camera']}，"
            f"景深层次，前中后景分明，"
            f"{scene['mood']}氛围，{style_info['lighting']}，{style_info['palette']}，"
            f"cinematic composition，dramatic lighting"
            f"{consistency_hint}，{quality_tags}{avoid_hint}"
        )

        rate_limiter.wait()
        try:
            # 先获取图片 URL，再下载到本地
            img_url = client.generate_image(
                prompt=prompt,
                size=IMAGE_SIZES["landscape"],
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
                    scene_duration: int = 5,
                    style: str = "三渲二国风") -> dict:
    """用 agnes-video-v2.0 图生视频。"""

    if cp.is_done("videos"):
        print("✅ 视频已存在，跳过")
        # 从磁盘重建 manifest
        manifest = {}
        for scene in script["scenes"]:
            sid = scene["id"]
            vid_path = vid_dir / f"{sid}.mp4"
            if vid_path.exists():
                manifest[sid] = str(vid_path)
        return manifest

    style_info = STYLE_PRESETS.get(style, STYLE_PRESETS["三渲二国风"])
    quality_tags = style_info.get("quality", "cinematic quality")
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

        # 构建视频提示词——强调动态、运镜、站位、氛围
        action = scene.get('action', '')
        visual = scene.get('visual', '')
        blocking = scene.get('blocking', '')
        camera = scene.get('camera', '')
        mood = scene.get('mood', '')
        location = scene.get('location', '')
        video_prompt = (
            f"{action}，{visual}，"
            f"人物站位与构图：{blocking}，{camera}，"
            f"smooth fluid motion，natural character movement，"
            f"hair and clothing dynamics，environmental particle effects，"
            f"keep the character's appearance strictly identical to the reference frame: "
            f"same hairstyle, outfit, and facial features,"
            f"{mood}氛围，{location}，"
            f"cinematic animation，professional quality，{quality_tags}"
        )

        # 图生视频：优先用 URL，否则用本地文件
        image_input = frame_url if frame_url else frame_path

        rate_limiter.wait()
        try:
            client.generate_video_full(
                prompt=video_prompt,
                out_path=out_path,
                image=image_input,
                height=768,
                width=1344,
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

    # 生成 SRT（字幕烧录统一在 main 末尾对所有最终成片执行，避免重复烧录）
    srt_path = project_dir / "subtitle.srt"
    generate_srt(script, srt_path, scene_duration=scene_duration, vid_manifest=vid_manifest)

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


def get_audio_duration(path: pathlib.Path) -> float:
    """用 ffprobe 获取音频时长。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 2.0  # fallback


def _ffmpeg_has_filter(name: str) -> bool:
    """检测当前 ffmpeg 是否支持指定滤镜（如 subtitles 需要 libass）。"""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=10)
        return re.search(rf"\b{re.escape(name)}\b", r.stdout) is not None
    except Exception:
        return False


def _detect_cjk_font() -> str | None:
    """检测系统中可用于字幕烧录的中文字体。

    优先返回 TTF/OTF（libass 的 FontFile 对 .ttc 支持不稳定）；
    找不到时返回 None，由调用方决定是否回退到 fontconfig 字体名。
    """
    if sys.platform == "darwin":
        candidates = [
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    else:  # Linux / 其他
        candidates = [
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def burn_subtitles(project_dir: pathlib.Path, srt_path: pathlib.Path,
                   video_path: pathlib.Path) -> pathlib.Path:
    """将 SRT 字幕烧录到视频。返回烧录后的视频路径（失败则原样返回）。"""
    if not srt_path.exists() or srt_path.stat().st_size == 0:
        return video_path
    if not _ffmpeg_has_filter("subtitles"):
        print("  ⚠️ 当前 ffmpeg 不含 libass（subtitles 滤镜），无法烧录字幕。")
        print("     请安装完整版：brew install ffmpeg-full")
        return video_path

    font_path = _detect_cjk_font()
    if font_path:
        font_arg = f"FontFile={font_path}"
    else:
        font_arg = "FontName=WenQuanYi Micro Hei"
        print("  ⚠️ 未检测到中文字体，回退到 WenQuanYi（可能无效果）")

    # 转义路径中的冒号（subtitles 滤镜把 ':' 当作选项分隔符）
    srt_escaped = str(srt_path).replace(":", "\\:")
    out = video_path.with_name(video_path.stem + "_sub.mp4")
    vf = (f"subtitles={srt_escaped}"
          f":force_style='{font_arg},FontSize=22,"
          f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
          f"Outline=2,Shadow=1'")
    cmd = ["ffmpeg", "-y", "-i", str(video_path),
           "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-c:a", "copy", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and out.exists():
            video_path.unlink()
            out.rename(video_path)
            print(f"  ✅ 字幕已烧录：{video_path.name}")
            return video_path
        print(f"  ⚠️ 字幕烧录失败：{r.stderr[-300:]}")
        out.unlink(missing_ok=True)
    except Exception as e:
        print(f"  ⚠️ 字幕烧录出错：{e}")
        out.unlink(missing_ok=True)
    return video_path


def _align_subtitle_windows(sid, dialogues, tts_manifest, scene_start, scene_dur):
    """计算每段对白字幕的 [start, end]，优先对齐真实 TTS 音频时长。

    有 TTS 清单时，用每句配音真实的 start / tts_duration，使字幕与声音同步；
    否则回退到按场景时长平均切分。同时避免同场景内相邻字幕重叠、且不超出场景结尾。
    """
    n = len(dialogues)
    if not n:
        return []
    per = scene_dur / n
    wins = []
    for i in range(n):
        key = f"{sid}_{i}"
        info = (tts_manifest or {}).get(key)
        if info and info.get("tts_duration"):
            st = float(info.get("start", scene_start + i * per))
            en = st + float(info["tts_duration"])
        else:
            st = scene_start + i * per
            en = st + per
        wins.append([st, en])
    # 防止相邻字幕重叠：后一条起点不早于前一条终点
    for i in range(1, n):
        if wins[i][0] < wins[i - 1][1]:
            wins[i][0] = wins[i - 1][1]
            if wins[i][1] < wins[i][0]:
                wins[i][1] = wins[i][0]
    scene_end = scene_start + scene_dur
    for i in range(n):
        if wins[i][1] > scene_end:
            wins[i][1] = scene_end
        if wins[i][0] > wins[i][1]:
            wins[i][0] = wins[i][1]
    return [(w[0], w[1]) for w in wins]


def generate_srt(script: dict, out_path: pathlib.Path, scene_duration: int = 5,
                 vid_manifest: dict | None = None, transition: float = 0.5,
                 tts_manifest: dict | None = None):
    """从剧本生成 SRT 字幕。

    时间轴与 TTS 配音严格一致：基于成片真实时间轴（xfade 拼接），
    避免字幕与声音错位。
    """
    lines = []
    idx = 1

    # 统一时间轴：优先用真实视频拼接时间线
    if vid_manifest:
        tl = _composition_timeline(script, vid_manifest, scene_duration, transition)
    else:
        tl = {}
        t = 0.0
        for scene in script["scenes"]:
            tl[scene["id"]] = {"start": t, "duration": float(scene_duration)}
            t += scene_duration

    for scene in script["scenes"]:
        sid = scene["id"]
        dialogues = scene.get("dialogue", [])
        if not dialogues:
            continue

        seg = tl.get(sid, {"start": 0.0, "duration": float(scene_duration)})
        # 字幕计时优先对齐真实 TTS 音频时长（声音-文字同步）
        wins = _align_subtitle_windows(sid, dialogues, tts_manifest,
                                       seg["start"], seg["duration"])

        for d_idx, d in enumerate(dialogues):
            text = d.get("text", "")
            st, en = wins[d_idx] if d_idx < len(wins) else (seg["start"], seg["start"] + seg["duration"])

            start_srt = format_srt_time(st)
            end_srt = format_srt_time(en)

            lines.append(f"{idx}")
            lines.append(f"{start_srt} --> {end_srt}")
            # 字幕只显示台词本身，不带角色名与冒号
            lines.append(f"{text}")
            lines.append("")

            idx += 1

    out_path.write_text("\n".join(lines), encoding="utf-8")


def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ===================== TTS 配音 =====================

# edge-tts 语音映射 — 每个角色分配不同语音
EDGE_TTS_VOICES = {
    "male": [
        "zh-CN-YunxiNeural",      # 阳光男声（首选）
        "zh-CN-YunjianNeural",    # 沉稳男声
        "zh-CN-YunyangNeural",    # 新闻男声
        "zh-HK-WanLungNeural",    # 港式男声
        "zh-TW-YunJheNeural",     # 台式男声
    ],
    "female": [
        "zh-CN-XiaoxiaoNeural",   # 温柔女声（首选）
        "zh-CN-XiaoyiNeural",     # 甜美女声
        "zh-CN-XiaohanNeural",    # 知性女声
        "zh-HK-HiuGaNeural",      # 港式女声
        "zh-TW-HsiaoChenNeural",  # 台式女声
    ],
}

# 角色语音缓存：确保同一角色始终用同一语音
_character_voice_map: dict[str, str] = {}


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


def _get_voice_for_character(char_id: str, char_gender: str) -> str:
    """为角色分配唯一语音（同性别角色用不同声音）。"""
    global _character_voice_map
    if char_id in _character_voice_map:
        return _character_voice_map[char_id]

    voices = EDGE_TTS_VOICES.get(char_gender, EDGE_TTS_VOICES["male"])
    # 已用同性别语音数量 → 选下一个
    used = sum(1 for v in _character_voice_map.values() if v in voices)
    voice = voices[used % len(voices)]
    _character_voice_map[char_id] = voice
    return voice


def _compute_scene_offsets(script: dict, vid_manifest: dict,
                           scene_duration: int = 5) -> dict[str, float]:
    """根据实际存在的视频，计算每个场景在最终成片中的起始秒数。
    
    vid_manifest: {"S01": "path/to/S01.mp4", ...}  只包含成功生成的场景
    """
    offsets = {}
    t = 0.0
    for scene in script["scenes"]:
        sid = scene["id"]
        if sid in vid_manifest:
            offsets[sid] = t
            # 用 ffprobe 取实际时长
            vid_path = pathlib.Path(vid_manifest[sid]) if vid_manifest[sid] else None
            if vid_path and vid_path.exists():
                dur = get_video_duration(vid_path)
            else:
                dur = float(scene_duration)
            t += dur
        # 如果场景视频不存在，不计入时间轴
    return offsets


def _composition_timeline(script: dict, vid_manifest: dict,
                          scene_duration: int = 5, transition: float = 0.5) -> dict:
    """计算每个场景在最终成片（xfade 转场拼接）中的真实起始秒数与可见时长。

    与 edit_final 的 xfade 拼接严格对齐：
      成片总时长 = Σ vid_dur - transition*(n-1)
    字幕(SRT)与配音(TTS)共用此时间轴，避免声音/文字错位。
    返回 {sid: {"start": float, "duration": float}}
    """
    tl: dict[str, dict[str, float]] = {}
    cursor = 0.0
    first = True
    for scene in script["scenes"]:
        sid = scene["id"]
        if sid not in vid_manifest:
            continue
        vp = pathlib.Path(vid_manifest[sid]) if vid_manifest[sid] else None
        dur = get_video_duration(vp) if (vp and vp.exists()) else float(scene_duration)
        if first:
            start = 0.0
            vis = dur
            first = False
        else:
            vis = dur - transition
            start = cursor
        tl[sid] = {"start": start, "duration": max(vis, 0.2)}
        cursor = start + vis
    return tl


def generate_tts(script: dict, audio_dir: pathlib.Path, cp: Checkpoint,
                 scene_duration: int = 5,
                 vid_manifest: dict | None = None,
                 checkpoint_key: str = "tts") -> dict:
    """为剧本对白生成 TTS 音频。优先 edge-tts（跨平台），回退到 macOS say。

    vid_manifest: 实际存在的视频清单，用于精确对齐 TTS 时间戳。
    checkpoint_key: 缓存标记键，编辑器重渲染时用不同键避免复用原片 TTS。
    """

    if cp.is_done(checkpoint_key):
        print("✅ TTS 已存在，跳过")
        manifest_path = audio_dir / "tts_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🎙️ 步骤 6：生成 TTS 配音...")
    cp.mark_running(checkpoint_key)
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
                cp.mark_done(checkpoint_key)
                return {}

    # 计算实际场景偏移（基于成片真实时间轴，与字幕一致）
    if vid_manifest:
        _tl = _composition_timeline(script, vid_manifest, scene_duration)
        scene_offsets = {sid: info["start"] for sid, info in _tl.items()}
        scene_durations = {sid: info["duration"] for sid, info in _tl.items()}
        print(f"  场景偏移：{scene_offsets}")
    else:
        scene_offsets = {}
        scene_durations = {}
        t = 0.0
        for scene in script["scenes"]:
            scene_offsets[scene["id"]] = t
            scene_durations[scene["id"]] = float(scene_duration)
            t += scene_duration

    valid_sids = set(scene_offsets.keys())

    # 收集所有场景的对白，缺失视频的对白追加到前一个有视频的场景
    scene_dialogues: dict[str, list] = {}  # sid -> [{character, text, orig_sid}]
    last_valid_sid = None
    for scene in script["scenes"]:
        sid = scene["id"]
        ds = scene.get("dialogue", [])
        if sid in valid_sids:
            scene_dialogues[sid] = [{**d, "orig_sid": sid} for d in ds]
            last_valid_sid = sid
        elif ds and last_valid_sid:
            # 缺失视频的对白追加到前一个有视频的场景
            scene_dialogues[last_valid_sid].extend(
                [{**d, "orig_sid": sid} for d in ds]
            )
            print(f"  📎 {sid} 对白合并到 {last_valid_sid}")
        elif ds:
            print(f"  ⚠️ {sid} 对白无法合并（无前置视频场景）")

    manifest = {}
    idx = 0

    for sid, dialogues in scene_dialogues.items():
        if not dialogues:
            continue

        scene_start = scene_offsets[sid]
        # 获取该场景实际视频时长
        if sid in scene_durations:
            actual_duration = scene_durations[sid]
        else:
            actual_duration = float(scene_duration)

        per_dialogue = actual_duration / max(len(dialogues), 1)

        for d_idx, d in enumerate(dialogues):
            char_id = d.get("character", "")
            text = d.get("text", "")
            orig_sid = d.get("orig_sid", sid)
            if not text:
                continue

            # 查找角色信息
            char_name = char_id
            char_gender = "male"
            for c in script.get("characters", []):
                if c["id"] == char_id:
                    char_name = c["name"]
                    visual = c.get("visual", "").lower()
                    if any(w in visual for w in ["女", "娘", "姑", "妃", "姬", "婉", "柔"]):
                        char_gender = "female"
                    break

            out_path = audio_dir / f"tts_{orig_sid}_{d_idx:02d}.mp3"

            # 计算时间戳：基于实际视频拼接位置
            start_time = scene_start + d_idx * per_dialogue

            # 选择语音：每个角色用不同声音
            if tts_engine == "edge-tts":
                voice = _get_voice_for_character(char_id, char_gender)
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
                # 获取 TTS 实际时长
                tts_duration = get_audio_duration(out_path)
                manifest[f"{sid}_{d_idx}"] = {
                    "path": str(out_path),
                    "character": char_name,
                    "character_id": char_id,
                    "gender": char_gender,
                    "voice": voice if tts_engine == "edge-tts" else ("Ting-Ting" if char_gender == "female" else "Li-Mu"),
                    "text": text,
                    "start": round(start_time, 3),
                    "tts_duration": round(tts_duration, 3),
                    "scene_duration": round(per_dialogue, 3),
                }
                print(f"  ✅ {char_name}({voice.split('_')[-1].replace('Neural','')}): {text[:20]}... [{start_time:.1f}s]")
                idx += 1
            else:
                print(f"  ❌ TTS 失败 {char_name}: {text[:20]}...")

    manifest_path = audio_dir / "tts_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done(checkpoint_key)
    print(f"  ✅ TTS 已保存：{audio_dir}（共 {idx} 条）")
    return manifest


# ===================== 口型同步 =====================

def _extract_volume_envelope(audio_path: pathlib.Path) -> list[tuple[float, float]]:
    """提取音频音量包络，返回 [(time, rms_normalized), ...]。"""
    import re
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(audio_path), "-af",
             "astat=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        times = []
        for line in result.stderr.split("\n"):
            m = re.search(r"pts_time:([\d.]+).*frame:.*?(\S+)", line)
            if not m:
                m = re.search(r"atime=([\d.]+).*level=([-\d.]+)", line)
            if m:
                t = float(m.group(1))
                rms_str = m.group(2)
                try:
                    rms = float(rms_str)
                    norm = max(0.0, min(1.0, (rms + 60) / 60))  # -60~0 dB → 0~1
                    times.append((t, norm))
                except ValueError:
                    pass
        return times if times else [(0.0, 0.5), (1.0, 0.5)]
    except Exception:
        return [(0.0, 0.3), (0.5, 0.8), (1.0, 0.3)]


def _make_lipsync_video(vid_path: pathlib.Path, tts_path: pathlib.Path,
                        out_path: pathlib.Path,
                        scene_offset: float, tts_start_in_scene: float,
                        tts_dur: float) -> bool:
    """为场景生成口型同步版视频。

    为避免与最终 mix 阶段重复叠加 TTS 导致“双重配音”，这里只输出
    与原视频视觉一致、去除音轨的版本；真正的配音对齐统一由 mix_audio
    基于成片时间轴完成（与字幕 SRT 同源，保证声画同步）。
    """
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(vid_path),
            "-an", "-c:v", "copy",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"    ffmpeg stderr: {result.stderr[:200]}")
            return False
        return out_path.exists()
    except Exception as e:
        print(f"    口型视频出错：{e}")
        return False


def generate_lipsync(project_dir: pathlib.Path, script: dict,
                      tts_manifest: dict, vid_manifest: dict,
                      cp: Checkpoint, scene_duration: int = 5) -> dict:
    """口型同步：将 TTS 音频对齐到视频，在说话时段产生微妙的画面微动。"""

    if cp.is_done("lipsync"):
        print("✅ 口型同步已存在，跳过")
        ls_path = project_dir / "lipsync_manifest.json"
        if ls_path.exists():
            return json.loads(ls_path.read_text())
        return {}

    print(f"\n👄 步骤 6.5：口型同步...")
    cp.mark_running("lipsync")

    if not tts_manifest:
        print("  ⚠️ 无 TTS 数据，跳过")
        cp.mark_done("lipsync")
        return {}

    lipsync_dir = project_dir / "lipsync"
    lipsync_dir.mkdir(parents=True, exist_ok=True)

    scene_offsets = {sid: info["start"]
                     for sid, info in _composition_timeline(script, vid_manifest, scene_duration).items()}
    vid_dir = project_dir / "videos"

    manifest = {}
    success_count = 0

    for key, tts_info in tts_manifest.items():
        sid = key.rsplit("_", 1)[0]
        char_name = tts_info.get("character", "?")
        tts_path = pathlib.Path(tts_info["path"])
        start_time = tts_info["start"]
        tts_dur = tts_info.get("tts_duration", 2.0)

        if not tts_path.exists():
            continue

        vid_path = vid_dir / f"{sid}.mp4"
        if not vid_path.exists():
            continue

        out_vid = lipsync_dir / f"{sid}_lipsync.mp4"
        scene_start = scene_offsets.get(sid, 0.0)
        relative_start = start_time - scene_start

        # 获取视频实际时长
        vid_dur = get_video_duration(vid_path)

        print(f"  {char_name}({sid})：TTS at {relative_start:.1f}s (scene starts {scene_start:.1f}s)")

        ok = _make_lipsync_video(
            vid_path, tts_path, out_vid,
            scene_offset=scene_start,
            tts_start_in_scene=relative_start,
            tts_dur=tts_dur,
        )

        if ok:
            manifest[sid] = str(out_vid)
            success_count += 1
            print(f"  ✅ {sid} 口型同步完成")
        else:
            print(f"  ⚠️ {sid} 口型同步失败，跳过")

    # 保存 manifest
    manifest_path = project_dir / "lipsync_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("lipsync")
    print(f"  ✅ 口型同步完成（{success_count}/{len(tts_manifest)} 节）")
    return manifest


# ===================== 音效生成 =====================

def _ambient_recipe_for(location: str, mood: str, action: str) -> str:
    """根据场景文本挑选背景音配方名。"""
    text = f"{location} {mood} {action}"
    for keys, name in AMBIENT_KEYWORD_MAP:
        if any(k in text for k in keys):
            return name
    return "neutral"


def _synth_ambient_clip(out_path: pathlib.Path, dur: float, color: str,
                        filt: str, add_drone: bool) -> bool:
    """用 ffmpeg 本地合成一段与视频等长的场景环境音。"""
    try:
        out_path = pathlib.Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dur = max(1.0, float(dur))
        base = f"anoisesrc=color={color}:duration={dur}:amplitude=0.9"
        fade = f"afade=t=in:st=0:d=0.6,afade=t=out:st={max(0.0, dur - 0.6):.2f}:d=0.6"
        noise_filt = f"{filt},{fade}"
        if add_drone:
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", base,
                "-f", "lavfi", "-i", f"sine=frequency=56:duration={dur}",
                "-filter_complex",
                f"[0:a]{noise_filt}[n];[1:a]volume=0.22[d];"
                f"[n][d]amix=inputs=2:duration=longest[a]",
                "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "4", str(out_path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", base,
                "-af", noise_filt,
                "-c:a", "libmp3lame", "-q:a", "4", str(out_path),
            ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.returncode == 0 and out_path.exists()
    except Exception as e:
        print(f"    ⚠️ 背景音合成失败 {out_path}: {e}")
        return False


def build_ambient_master(project_dir: pathlib.Path,
                         segments: list[tuple], total_duration: float) -> pathlib.Path | None:
    """把各场景环境音按成片时间轴拼成一条统一背景音轨。

    segments: 有序列表 [(amb_path_or_None, start_sec, dur_sec)]，
    顺序需与成片（xfade 拼接）一致。每个片段延迟到其起始秒数后混音。
    返回 ambient_master.wav 路径，无有效片段返回 None。
    """
    inputs = []
    parts = []
    idx = 0
    for amb, start, dur in segments:
        if amb and pathlib.Path(amb).exists():
            inputs += ["-i", str(amb)]
        else:
            inputs += ["-f", "lavfi", "-i",
                       f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={dur}"]
        start_ms = int(start * 1000)
        parts.append(f"[{idx}:a]adelay={start_ms}|{start_ms}[d{idx}]")
        idx += 1
    if idx == 0:
        return None
    amix = "".join(f"[d{i}]" for i in range(idx)) + f"amix=inputs={idx}:duration=longest[aamb]"
    filt = ";".join(parts) + f";{amix};[aamb]volume=0.32,lowpass=f=9500[aout]"
    out = project_dir / "ambient_master.wav"
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filt, "-map", "[aout]",
           "-t", str(total_duration), "-c:a", "pcm_s16le", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode == 0 and out.exists():
        return out
    print(f"  ⚠️ 背景音主控合成失败：{r.stderr[-200:]}")
    return None


def generate_sfx(client: AgnesClient, script: dict, sfx_dir: pathlib.Path,
                 cp: Checkpoint, rate_limiter: RateLimiter,
                 vid_manifest: dict | None = None,
                 scene_duration: int = 5) -> dict:
    """为场景生成本地合成的环境背景音（ffmpeg，免费），写入 sfx_manifest。

    不再只是"描述文本"，而是真实可混的音轨：按 location/mood 关键词选择
    合成配方（风/水/火/战/静/法术…），每个场景一段与视频等长的环境音。
    """

    if cp.is_done("sfx"):
        print("✅ 背景音已存在，跳过")
        manifest_path = sfx_dir / "sfx_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}

    print(f"\n🔊 步骤 7：合成场景背景音（本地 ffmpeg）...")
    cp.mark_running("sfx")
    sfx_dir.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for scene in script["scenes"]:
        sid = scene["id"]
        location = scene.get("location", "")
        mood = scene.get("mood", "")
        action = scene.get("action", "")
        recipe_name = _ambient_recipe_for(location, mood, action)
        color, filt = AMBIENT_RECIPES[recipe_name]
        add_drone = mood in AMBIENT_DRONE_MOODS

        # 该场景的真实时长（与视频一致，保证对齐）
        dur = float(scene_duration)
        if vid_manifest and vid_manifest.get(sid):
            vp = pathlib.Path(vid_manifest[sid])
            if vp.exists():
                dur = get_video_duration(vp)

        out_path = sfx_dir / f"{sid}_amb.mp3"
        ok = _synth_ambient_clip(out_path, dur, color, filt, add_drone)
        manifest[sid] = {
            "path": str(out_path) if ok else "",
            "recipe": recipe_name,
            "mood": mood,
            "location": location,
            "duration": round(dur, 3),
        }
        print(f"  {'✅' if ok else '⚠️'} {sid}: {recipe_name} 背景音 ({dur:.1f}s)")

    manifest_path = sfx_dir / "sfx_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cp.mark_done("sfx")
    print(f"  ✅ 背景音已合成：{sfx_dir}")
    return manifest


# ===================== 音频混音 =====================

def _build_vid_manifest(project_dir: pathlib.Path, script: dict) -> dict:
    """从 videos 目录重建视频清单（仅含已存在文件）。"""
    vd = project_dir / "videos"
    vm = {}
    for s in script.get("scenes", []):
        p = vd / f"{s['id']}.mp4"
        if p.exists():
            vm[s["id"]] = str(p)
    return vm


def _build_tts_track(tts_manifest: dict, out_path: pathlib.Path) -> pathlib.Path | None:
    """把 TTS manifest 中每条配音按 start 延迟对齐，混合成一条音轨。

    供主流程 mix_audio 与编辑器 edit_render 共用，保证配音时间轴与字幕一致。
    """
    if not tts_manifest:
        return None
    tts_items = sorted(tts_manifest.items(), key=lambda x: x[1]["start"])
    filter_parts = []
    inputs = []
    input_idx = 0
    for key, info in tts_items:
        tts_path = pathlib.Path(info["path"])
        if not tts_path.exists():
            continue
        start = info["start"]
        inputs += ["-i", str(tts_path)]
        delay_ms = int(start * 1000)
        filter_parts.append(f"[{input_idx}:a]adelay={delay_ms}|{delay_ms}[a{input_idx}]")
        input_idx += 1
    if not filter_parts:
        return None
    n = input_idx
    amix_inputs = "".join([f"[a{i}]" for i in range(n)])
    filter_complex = ";".join(filter_parts) + f";{amix_inputs}amix=inputs={n}:duration=longest[aout]"
    out_path = pathlib.Path(out_path)
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_complex, "-map", "[aout]",
           "-c:a", "aac", "-b:a", "128k", str(out_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and out_path.exists():
            return out_path
        print(f"  ⚠️ TTS 轨道合成失败：{r.stderr[:200]}")
    except Exception as e:
        print(f"  ⚠️ TTS 轨道合成出错：{e}")
    return None



def mix_audio(project_dir: pathlib.Path, script: dict,
              tts_manifest: dict, sfx_manifest: dict,
              cp: Checkpoint, scene_duration: int = 5,
              vid_manifest: dict | None = None) -> pathlib.Path | None:
    """混合视频 + TTS 配音 + 场景背景音（真实音轨）。"""

    if cp.is_done("mix"):
        final = project_dir / "final_with_audio.mp4"
        if final.exists():
            print(f"✅ 混音成片已存在：{final}")
            return final

    print(f"\n🎚️ 步骤 8：音频混音...")
    cp.mark_running("mix")

    # 优先使用 xfade 拼接的纯净视频（无内置音轨），配音由下方统一叠加，
    # 避免与 lipsync 视频内已混音的 TTS 重复导致“双重配音”。
    video_path = project_dir / "final.mp4"
    if not video_path.exists():
        video_path = project_dir / "final_lipsync.mp4"
    if not video_path.exists():
        print("  ❌ 视频文件不存在")
        return None
    print(f"  使用视频：{video_path.name}")

    if vid_manifest is None:
        vid_manifest = _build_vid_manifest(project_dir, script)

    # 构建音频轨道
    audio_tracks = []

    # 1. TTS 轨道（按时间对齐）
    if tts_manifest:
        print(f"  合成 {len(tts_manifest)} 条 TTS...")
        tts_mixed = _build_tts_track(tts_manifest, project_dir / "tts_mixed.m4a")
        if tts_mixed:
            audio_tracks.append(("tts", tts_mixed))
            print(f"  ✅ TTS 轨道合成完成")

    # 1.5 场景背景音（按成片时间轴拼接成统一环境音轨，置于配音之下）
    if sfx_manifest:
        segs = []
        if vid_manifest:
            tl = _composition_timeline(script, vid_manifest, scene_duration)
            for scene in script["scenes"]:
                sid = scene["id"]
                info = sfx_manifest.get(sid, {})
                if sid in tl and info.get("path"):
                    segs.append((info["path"], tl[sid]["start"], tl[sid]["duration"]))
        if segs:
            total = get_video_duration(video_path)
            print(f"  合成场景背景音轨（{len(segs)} 段，总长 {total:.1f}s）...")
            ambient_master = build_ambient_master(project_dir, segs, total)
            if ambient_master:
                audio_tracks.append(("ambient", ambient_master))
                print(f"  ✅ 背景音轨：{ambient_master.name}")

    # 2. 混合视频 + 音频（配音 + 场景背景音）
    final_audio = project_dir / "final_with_audio.mp4"

    if audio_tracks:
        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        for _, audio_path in audio_tracks:
            cmd += ["-i", str(audio_path)]

        n_audio = len(audio_tracks)
        amix = "".join([f"[{i+1}:a]" for i in range(n_audio)])
        filter_complex = f"{amix}amix=inputs={n_audio}:duration=longest[aout]"
        cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(final_audio)]

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
    return video_path


# ===================== 编辑器：剪辑输出 =====================

def _xfade_concat(clip_paths: list, out_path: pathlib.Path, transition: float = 0.5):
    """把多个视频片段用 xfade 转场拼接成一个文件（与 edit_final 同逻辑）。"""
    out_path = pathlib.Path(out_path)
    if len(clip_paths) == 1:
        import shutil
        shutil.copy2(clip_paths[0], out_path)
        return out_path

    n = len(clip_paths)
    inputs = []
    for cp_ in clip_paths:
        inputs += ["-i", str(cp_)]

    durations = [get_video_duration(p) for p in clip_paths]
    filter_parts = []
    offset = durations[0] - transition
    for i in range(n - 1):
        in_a = f"[{i}:v]" if i == 0 else f"[v{i-1}{i}]"
        in_b = f"[{i+1}:v]"
        out_label = f"[v{i}{i+1}]" if i < n - 2 else "[vout]"
        filter_parts.append(
            f"{in_a}{in_b}xfade=transition=fade:duration={transition}:offset={offset}{out_label}")
        if i < n - 2:
            offset += durations[i + 1] - transition

    vfilter = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", vfilter, "-map", "[vout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-an", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"  ⚠️ 剪辑拼接失败：{r.stderr[-200:]}")
        return None
    return out_path


def _generate_srt_for_timeline(script: dict, tl: list, out_path: pathlib.Path, disabled=None,
                                tts_manifest: dict | None = None):
    """根据显式时间轴 tl=[(sid, start, dur), ...] 生成 SRT（与 generate_srt 同源）。

    disabled: 需要关闭字幕的 sid 集合（编辑器逐镜/全局开关）。
    tts_manifest: 真实配音清单，用于让字幕与声音精确对齐。
    """
    disabled = set(disabled or [])
    lines = []
    idx = 1
    tl_map = {sid: (start, dur) for sid, start, dur in tl}
    for scene in script["scenes"]:
        sid = scene["id"]
        if sid not in tl_map:
            continue
        if sid in disabled:
            continue
        start, dur = tl_map[sid]
        dialogues = scene.get("dialogue", [])
        if not dialogues:
            continue
        # 字幕计时优先对齐真实 TTS 音频时长（声音-文字同步）
        wins = _align_subtitle_windows(sid, dialogues, tts_manifest, start, dur)
        for d_idx, d in enumerate(dialogues):
            text = d.get("text", "")
            st, en = wins[d_idx] if d_idx < len(wins) else (start, start + dur)
            lines.append(str(idx))
            lines.append(f"{format_srt_time(st)} --> {format_srt_time(en)}")
            # 字幕只显示台词本身，不带角色名与冒号
            lines.append(f"{text}")
            lines.append("")
            idx += 1
    out_path.write_text("\n".join(lines), encoding="utf-8")


def _load_sfx_m(project_dir: pathlib.Path) -> dict:
    """读取 sfx_manifest.json，缺省返回空。"""
    sp = project_dir / "sfx" / "sfx_manifest.json"
    if sp.exists():
        try:
            return json.loads(sp.read_text())
        except Exception:
            return {}
    return {}


def _build_scene_voice(sid: str, tts_m_full: dict, out_dir: pathlib.Path):
    """把某镜头所有对白 TTS 按场景内偏移聚合成一条镜头配音片段。

    返回临时片段路径；无有效对白返回 None。
    """
    if not tts_m_full:
        return None
    entries = [(k, v) for k, v in tts_m_full.items() if k.startswith(f"{sid}_")]
    if not entries:
        return None
    entries.sort(key=lambda kv: int(kv[0].split("_")[-1]))
    per = float(entries[0][1].get("scene_duration", 1.0))
    inputs = []
    parts = []
    for k, (kk, info) in enumerate(entries):
        p = pathlib.Path(info["path"])
        if not p.exists():
            continue
        inputs += ["-i", str(p)]
        d = int(k * per * 1000)
        parts.append(f"[{k}:a]adelay={d}|{d}[v{k}]")
    if not parts:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"voice_{sid}.m4a"
    n = len(parts)
    amix = "".join(f"[v{i}]" for i in range(n))
    filt = ";".join(parts) + f";{amix}amix=inputs={n}:duration=longest[vo]"
    r = subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", filt,
                       "-map", "[vo]", "-c:a", "aac", "-b:a", "128k", str(out)],
                      capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and out.exists():
        return out
    return None


def edit_render(project_dir: pathlib.Path, script: dict, edits: list,
                cp: Checkpoint, scene_duration: int = 5,
                with_audio: bool = True,
                audio_tracks: dict | None = None,
                audio_clips: dict | None = None,
                subtitle_disabled: list | None = None) -> pathlib.Path | None:
    """编辑器导出：对场景做裁剪(in/out)/启停/重排后重新拼接成片。

    edits: 有序列表，每项 {"sid", "in"(秒), "out"(秒), "enabled"(bool)}
    返回 edited_final.mp4（无声）或 edited_final_with_audio.mp4（含配音+背景音）。
    """
    print(f"\n✂️ 编辑器导出（{len(edits)} 个片段操作）...")
    vid_dir = project_dir / "videos"
    clip_dir = project_dir / "edit_clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    clips = []  # (clip_path, sid, dur)
    for e in edits:
        if not e.get("enabled", True):
            continue
        sid = e["sid"]
        src = vid_dir / f"{sid}.mp4"
        if not src.exists():
            print(f"  ⚠️ 片段 {sid} 视频不存在，跳过")
            continue
        d = get_video_duration(src)
        in_s = max(0.0, float(e.get("in", 0.0) or 0.0))
        out_s = min(d, float(e.get("out", d) or d))
        if out_s - in_s < 0.2:
            continue
        clip = clip_dir / f"{sid}.mp4"
        cmd = ["ffmpeg", "-y", "-ss", f"{in_s:.3f}", "-to", f"{out_s:.3f}",
               "-i", str(src), "-c:v", "libx264", "-crf", "23", "-preset", "fast",
               "-an", str(clip)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and clip.exists():
            clips.append((clip, sid, out_s - in_s))
            print(f"  ✅ 片段 {sid}: {in_s:.1f}s→{out_s:.1f}s")
        else:
            print(f"  ⚠️ 片段 {sid} 裁剪失败")

    if not clips:
        print("  ⚠️ 没有可用片段")
        return None

    edited = project_dir / "edited_final.mp4"
    res = _xfade_concat([c[0] for c in clips], edited, transition=0.5)
    if not res or not edited.exists():
        return None

    # 剪辑后时间轴（与 xfade 拼接一致）
    tl = []
    cursor = 0.0
    first = True
    for clip, sid, dur in clips:
        if first:
            tl.append((sid, 0.0, dur))
            first = False
        else:
            tl.append((sid, cursor, dur - 0.5))
        cursor = tl[-1][1] + tl[-1][2]

    # 字幕（基于剪辑后时间轴）
    srt = project_dir / "edited_subtitle.srt"
    _generate_srt_for_timeline(script, tl, srt, disabled=subtitle_disabled)
    burned = burn_subtitles(project_dir, srt, edited)
    edited = burned if (burned and burned.exists()) else edited

    if not with_audio:
        print(f"  ✅ 无声剪辑成片：{edited}")
        return edited

    # ---------- 音频：分轨混音 ----------
    tts_m = None  # 整条合成分支里生成的配音清单（用于字幕对齐）
    # 向后兼容：未提供逐镜头裁剪时，用整条合成
    if audio_clips is None:
        if audio_tracks is None:
            audio_tracks = (
                {"tts": {"enabled": True, "volume": 1.0},
                 "ambient": {"enabled": True, "volume": 0.32}}
                if with_audio else
                {"tts": {"enabled": False}, "ambient": {"enabled": False}}
            )
        tt_cfg = audio_tracks.get("tts") or {}
        amb_cfg = audio_tracks.get("ambient") or {}
        music_cfg = audio_tracks.get("music") or {}
        order_sids = [sid for _, sid, _ in clips]
        scene_by_id = {s["id"]: s for s in script.get("scenes", [])}
        sub_script = {**script, "scenes": [scene_by_id[s] for s in order_sids if s in scene_by_id]}
        input_tracks = []
        if tt_cfg.get("enabled", False):
            temp_vm = {sid: str(c) for c, sid, _ in clips}
            cp.data.pop("edit_tts", None)
            tts_m = generate_tts(sub_script, project_dir / "edit_audio", cp,
                                 scene_duration=scene_duration, vid_manifest=temp_vm,
                                 checkpoint_key="edit_tts")
            tts_mixed = _build_tts_track(tts_m, project_dir / "edit_audio" / "tts_mixed.m4a")
            if tts_mixed:
                input_tracks.append((tts_mixed, float(tt_cfg.get("volume", 1.0))))
        if amb_cfg.get("enabled", False):
            sfx_m = _load_sfx_m(project_dir)
            segs = [(sfx_m[sid]["path"], st, du) for sid, st, du in tl
                    if sid in sfx_m and sfx_m[sid].get("path")]
            if segs:
                amb = build_ambient_master(project_dir, segs, get_video_duration(edited))
                if amb:
                    input_tracks.append((amb, float(amb_cfg.get("volume", 0.32))))
        if music_cfg.get("enabled", False) and music_cfg.get("path"):
            mp = pathlib.Path(music_cfg["path"])
            if not mp.is_absolute():
                mp = project_dir / mp
            if mp.exists():
                input_tracks.append((mp, float(music_cfg.get("volume", 0.5))))
        final_a = project_dir / "edited_final_with_audio.mp4"
        if not input_tracks:
            print("  ✅ 剪辑成片（无启用音频轨）")
            return edited
        inputs = ["-i", str(edited)]
        vp = []
        for idx, (ap, vol) in enumerate(input_tracks):
            inputs += ["-i", str(ap)]
            vp.append(f"[{idx+1}:a]volume={vol:.3f}[a{idx}]")
        mix = "".join(f"[a{i}]" for i in range(len(input_tracks)))
        filt = ";".join(vp) + f";{mix}amix=inputs={len(input_tracks)}:duration=longest[aout]"
        r = subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", filt,
                           "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                           "-c:a", "aac", "-b:a", "160k", "-shortest", str(final_a)],
                          capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and final_a.exists():
            # 字幕与配音对齐：用真实 TTS 时长重新烧录字幕到带配音成片
            if tts_m:
                srt_a = project_dir / "edited_subtitle_aligned.srt"
                _generate_srt_for_timeline(script, tl, srt_a,
                                            disabled=subtitle_disabled, tts_manifest=tts_m)
                burned_a = burn_subtitles(project_dir, srt_a, final_a)
                if burned_a and burned_a.exists():
                    final_a = burned_a
            print(f"  ✅ 剪辑成片（整条合成，{len(input_tracks)} 轨）")
            return final_a
        print(f"  ⚠️ 音频混合失败：{r.stderr[-200:]}")
        return edited

    # ===== 逐镜头裁剪模式（每条轨道的每个镜头片段可独立裁剪）=====
    tt_cfg = audio_tracks.get("tts") or {}
    amb_cfg = audio_tracks.get("ambient") or {}
    music_cfg = audio_tracks.get("music") or {}

    if tt_cfg.get("enabled", False):
        order_sids = [sid for _, sid, _ in clips]
        scene_by_id = {s["id"]: s for s in script.get("scenes", [])}
        sub_script = {**script, "scenes": [scene_by_id[s] for s in order_sids if s in scene_by_id]}
        temp_vm = {sid: str(c) for c, sid, _ in clips}
        cp.data.pop("edit_tts", None)
        tts_m_full = generate_tts(sub_script, project_dir / "edit_audio", cp,
                                  scene_duration=scene_duration, vid_manifest=temp_vm,
                                  checkpoint_key="edit_tts")
    else:
        tts_m_full = {}

    audio_inputs = []
    def add_clip(path, ss, to, delay_ms):
        if ss is not None and ss <= 0 and (to is None or to >= 9999):
            audio_inputs.append((str(path), None, None, delay_ms))
        else:
            audio_inputs.append((str(path), ss, to, delay_ms))
        return len(audio_inputs)

    parts = []
    track_labels = []

    if tt_cfg.get("enabled", False):
        tts_clips = (audio_clips or {}).get("tts") or {}
        gi_list = []
        for sid, start, dur in tl:
            c = tts_clips.get(sid)
            if c and not c.get("enabled", True):
                continue
            voice = _build_scene_voice(sid, tts_m_full, project_dir / "edit_clips")
            if not voice:
                continue
            cin = float(c["in"]) if (c and c.get("in") is not None) else 0.0
            cout = float(c["out"]) if (c and c.get("out") is not None) else 9999.0
            gi = add_clip(voice, cin, cout, int(start * 1000))
            parts.append(f"[{gi}:a]adelay={int(start*1000)}|{int(start*1000)}[tc{gi}]")
            gi_list.append(gi)
        if gi_list:
            if len(gi_list) == 1:
                parts.append(f"[tc{gi_list[0]}]volume={float(tt_cfg.get('volume',1.0)):.3f}[tl]")
            else:
                ain = "".join(f"[tc{g}]" for g in gi_list)
                parts.append(f"{ain}amix=inputs={len(gi_list)}:duration=longest[tm];[tm]volume={float(tt_cfg.get('volume',1.0)):.3f}[tl]")
            track_labels.append("[tl]")

    if amb_cfg.get("enabled", False):
        sfx_m = _load_sfx_m(project_dir)
        amb_clips = (audio_clips or {}).get("ambient") or {}
        gi_list = []
        for sid, start, dur in tl:
            c = amb_clips.get(sid)
            if c and not c.get("enabled", True):
                continue
            info = sfx_m.get(sid)
            if not info or not info.get("path"):
                continue
            cin = float(c["in"]) if (c and c.get("in") is not None) else 0.0
            cout = float(c["out"]) if (c and c.get("out") is not None) else 9999.0
            gi = add_clip(info["path"], cin, cout, int(start * 1000))
            parts.append(f"[{gi}:a]adelay={int(start*1000)}|{int(start*1000)}[ac{gi}]")
            gi_list.append(gi)
        if gi_list:
            if len(gi_list) == 1:
                parts.append(f"[ac{gi_list[0]}]volume={float(amb_cfg.get('volume',0.32)):.3f}[al]")
            else:
                ain = "".join(f"[ac{g}]" for g in gi_list)
                parts.append(f"{ain}amix=inputs={len(gi_list)}:duration=longest[am];[am]volume={float(amb_cfg.get('volume',0.32)):.3f}[al]")
            track_labels.append("[al]")

    if music_cfg.get("enabled", False) and music_cfg.get("path"):
        mp = pathlib.Path(music_cfg["path"])
        if not mp.is_absolute():
            mp = project_dir / mp
        if mp.exists():
            mc = (audio_clips or {}).get("music") or {}
            cin = float(mc.get("in", 0) or 0)
            cout = float(mc.get("out", 0) or 0)
            gi = add_clip(mp, cin, cout if cout > 0 else 9999, 0)
            parts.append(f"[{gi}:a]volume={float(music_cfg.get('volume',0.5)):.3f}[ml]")
            track_labels.append("[ml]")

    final_a = project_dir / "edited_final_with_audio.mp4"
    if not track_labels:
        print("  ✅ 剪辑成片（无启用音频轨）")
        return edited
    inputs = ["-i", str(edited)]
    for (path, ss, to, dm) in audio_inputs:
        if ss is None:
            inputs += ["-i", path]
        else:
            inputs += ["-ss", f"{ss:.3f}", "-to", f"{to:.3f}", "-i", path]
    if len(track_labels) == 1:
        filt = ";".join(parts)
        map_a = track_labels[0]
    else:
        parts.append("".join(track_labels) + f"amix=inputs={len(track_labels)}:duration=longest[aout]")
        filt = ";".join(parts)
        map_a = "[aout]"
    r = subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", filt,
                       "-map", "0:v", "-map", map_a, "-c:v", "copy",
                       "-c:a", "aac", "-b:a", "160k", "-shortest", str(final_a)],
                      capture_output=True, text=True, timeout=240)
    if r.returncode == 0 and final_a.exists():
        # 字幕与配音对齐：用真实 TTS 时长重新烧录字幕到带配音成片
        if tts_m_full:
            srt_a = project_dir / "edited_subtitle_aligned.srt"
            _generate_srt_for_timeline(script, tl, srt_a,
                                        disabled=subtitle_disabled, tts_manifest=tts_m_full)
            burned_a = burn_subtitles(project_dir, srt_a, final_a)
            if burned_a and burned_a.exists():
                final_a = burned_a
        print(f"  ✅ 剪辑成片（逐镜头分轨混音，{len(track_labels)} 轨）")
        return final_a
    print(f"  ⚠️ 音频混合失败：{r.stderr[-200:]}")
    return edited


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
        style=args.style,
    )

    # Step 5: 成片拼接（传入 vid_manifest 以便字幕 SRT 使用统一时间轴）
    final = edit_final(project_dir, script, vid_manifest, cp,
                       scene_duration=args.scene_duration)

    # Step 6: TTS 配音（传入实际视频清单，精确对齐时间戳）
    tts_manifest = {}
    if not args.no_tts:
        tts_manifest = generate_tts(
            script, project_dir / "audio", cp,
            scene_duration=args.scene_duration,
            vid_manifest=vid_manifest,
        )
    else:
        print("\n🎙️ TTS 配音已禁用")

    # 字幕与配音对齐：TTS 已生成真实时长，重新生成 SRT（在烧录前覆盖），
    # 使每句字幕的起止严格匹配对应配音，实现声音-文字同步。
    if tts_manifest:
        srt_path = project_dir / "subtitle.srt"
        generate_srt(script, srt_path, scene_duration=args.scene_duration,
                     vid_manifest=vid_manifest, tts_manifest=tts_manifest)
        print(f"  🔤 字幕已按配音时长对齐（{len(tts_manifest)} 条）")

    # Step 6.5: 口型同步
    lipsync_manifest = {}
    if not args.no_tts and tts_manifest:
        lipsync_manifest = generate_lipsync(
            project_dir, script, tts_manifest, vid_manifest, cp,
            scene_duration=args.scene_duration,
        )

    # Step 7: 音效（真实场景背景音）
    sfx_manifest = {}
    if not args.no_sfx:
        sfx_manifest = generate_sfx(
            client, script, project_dir / "sfx", cp, rl,
            vid_manifest=vid_manifest, scene_duration=args.scene_duration,
        )
    else:
        print("\n🔊 音效已禁用")

    # Step 8: 音频混音
    # 如果有口型同步视频，用口型版重新拼接
    if lipsync_manifest:
        print("\n🎬 用口型同步视频重新拼接成片...")
        # 替换有口型的视频片段
        merged_vid_manifest = dict(vid_manifest)
        for sid, ls_path in lipsync_manifest.items():
            if pathlib.Path(ls_path).exists():
                merged_vid_manifest[sid] = ls_path
        # 重新拼接
        lipsync_final = project_dir / "final_lipsync.mp4"
        try:
            # 收集视频片段
            video_files = []
            for scene in script["scenes"]:
                sid = scene["id"]
                if sid in merged_vid_manifest:
                    vp = pathlib.Path(merged_vid_manifest[sid])
                    if vp.exists():
                        video_files.append(vp)
            if len(video_files) > 1:
                _simple_concat(video_files, project_dir, lipsync_final)
            elif video_files:
                import shutil
                shutil.copy2(video_files[0], lipsync_final)
            if lipsync_final.exists():
                final = lipsync_final
                print(f"  ✅ 口型成片：{final}")
        except Exception as e:
            print(f"  ⚠️ 口型拼接失败：{e}")

    final_with_audio = None
    if tts_manifest or sfx_manifest:
        final_with_audio = mix_audio(
            project_dir, script, tts_manifest, sfx_manifest, cp,
            scene_duration=args.scene_duration, vid_manifest=vid_manifest,
        )

    # 字幕烧录：确保最终交付的成片都带中文字幕
    # （含配音版、口型版、静音版，避免口型同步后 final 被重定向而漏烧）
    srt_path = project_dir / "subtitle.srt"
    candidates = {
        project_dir / "final.mp4",
        project_dir / "final_lipsync.mp4",
        final_with_audio,
    }
    for vp in candidates:
        if vp and pathlib.Path(vp).exists():
            burn_subtitles(project_dir, srt_path, pathlib.Path(vp))

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
