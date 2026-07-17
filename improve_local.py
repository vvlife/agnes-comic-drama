#!/usr/bin/env python3
"""离线兜底：不调用任何 API，基于场景元数据 + 角色设定，确定性地把"未改进"的镜头升级为
"有意义对白(带 intent) + 站位 blocking + 翻倍 action/visual"。

增量模式：已含 blocking 且 action≥120 字的镜头视为已改进，跳过（保留 LLM 成果）。
仅作为 LLM 升级（apply_improve.py）失败批次的兜底。用法：python3 improve_local.py
"""
import json
import pathlib

PROJ = pathlib.Path("/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama")
PD = PROJ / "output" / "excel-shen"
SCRIPT = PD / "script.json"

MOOD_LINES = {
    "肃杀": [
        ("{name}：这一战，再无回头路。要么你退，要么我送你下去。", "以决绝姿态逼对方亮明底牌"),
        ("{name}：你以为封印只锁住力量？它锁的是你不肯认的过去。", "用真相施压/点破对方心结"),
    ],
    "紧张": [
        ("{name}：别动。阵眼就在你脚下，一步错，满盘皆碎。", "警示/阻止对方踏入险地"),
        ("{name}：他们来了。屏息，等我数到三。", "下达指令/稳住同伴情绪"),
    ],
    "悲壮": [
        ("{name}：这一刀本该是我的，却要你替我承受。", "表达愧疚/托付后事"),
        ("{name}：记住我今日的抉择，它比为我报仇更有意义。", "以死明志/留下信念"),
    ],
    "震撼": [
        ("{name}：原来所谓的天命，不过是一群人不敢开口的沉默。", "揭示主题/反转认知"),
        ("{name}：看清楚了——这便是我们赌上一切的尽头。", "引导对方见证转折"),
    ],
    "神秘": [
        ("{name}：你听见了吗？封渊深处，有人在唤你的名字。", "制造悬念/引诱对方深入"),
        ("{name}：此物非刀非印，是千年来第一个不肯认命的人。", "抛出谜题/暗示真相"),
    ],
    "平静": [
        ("{name}：风停了。许久没这样，只是坐着，看光落下来。", "卸下防备/展露柔软一面"),
        ("{name}：有些账不必今日算清，活着才有下文。", "劝和/留有余地"),
    ],
}
DEFAULT_LINES = [
    ("{name}：你我来这一遭，总得有人先说破那句谎。", "撕破伪装/逼出真话"),
    ("{name}：若结局早已写好，我们偏要改它最后一笔。", "表明反抗意志"),
]


def bucket(mood: str) -> str:
    m = mood or ""
    for k in MOOD_LINES:
        if k in m:
            return k
    return "默认"


def make_dialogue(raw_ids, name_of, mood, idx):
    if not raw_ids:
        return []
    lines = MOOD_LINES.get(bucket(mood), DEFAULT_LINES)
    out = []
    for i, cid in enumerate(raw_ids[:2]):
        tpl, intent = lines[(idx + i) % len(lines)]
        out.append({"character": cid, "text": tpl.format(name=name_of.get(cid, cid)),
                    "intent": intent})
    return out


def make_blocking(raw_ids, mood):
    if not raw_ids:
        return "空镜：画面以环境为主体，无人居中，仅以光影与留白交代氛围。"
    if len(raw_ids) == 1:
        c = raw_ids[0]
        return f"{c} 居于画面左三分线、前景半身，身侧留出大片负空间，镜头略仰，凸显其孤绝气势。"
    a, b = raw_ids[0], raw_ids[1]
    if bucket(mood) in ("肃杀", "紧张", "震撼"):
        return f"{a} 立于画面左下、前景压低重心，{b} 悬于右上、后景居高俯压，二人成对角线对峙，间距两臂，剑拔弩张。"
    return f"{a} 居左前景、{b} 居右后景，二人呈三角错位，视线交汇于画面中心，彼此间距约一步，关系微妙。"


def expand_action(orig: str, location: str, mood: str) -> str:
    base = (orig or "").strip()
    if len(base) >= 120:
        return base
    add = (
        f"镜头自{location}的纵深缓缓推近，{mood}之气自画面边缘漫开；"
        f"角色肩头衣袂被气流掀起又落下，发丝扫过下颌，眼神在明暗交界处由缓转锐。"
        f"地面浮尘随呼吸明灭，每一步落点都惊起细碎光粒，仿佛封印在无声苏醒。"
    )
    return (base + "，" if base else "") + add


def make_visual(location: str, mood: str) -> str:
    return (
        f"{location}在{mood}基调下呈冷调金属光泽，远处雾气被一束侧逆光切开，"
        f"前景道具轮廓锐利、质感粗粝；色温偏低，唯角色周身留一圈暖色轮廓光，"
        f"与周遭幽蓝形成对比，焦点收束于其眉眼，氛围凝重而克制。"
    )


def main():
    script = json.loads(SCRIPT.read_text())
    chars = script.get("characters", [])
    name_of = {c["id"]: c.get("name", c["id"]) for c in chars}
    scenes = script["scenes"]
    done = 0
    for i, s in enumerate(scenes):
        # 增量：已含 blocking 且 action≥120 视为已改进，跳过
        if s.get("blocking", "").strip() and len(s.get("action", "")) >= 120:
            continue
        raw_ids = s.get("characters", []) or ([chars[i % len(chars)]["id"]] if chars else [])
        s["dialogue"] = make_dialogue(raw_ids, name_of, s.get("mood", ""), i)
        s["blocking"] = make_blocking(raw_ids, s.get("mood", ""))
        s["action"] = expand_action(s.get("action", ""), s.get("location", ""), s.get("mood", ""))
        s["visual"] = make_visual(s.get("location", ""), s.get("mood", ""))
        done += 1
    SCRIPT.write_text(json.dumps(script, ensure_ascii=False, indent=2))
    print(f"✅ 离线兜底增量升级完成：{SCRIPT}（本批补 {done} 镜，其余保留 LLM 成果）")


if __name__ == "__main__":
    main()
