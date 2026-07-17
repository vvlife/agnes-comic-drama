#!/usr/bin/env python3
"""下游重渲染：剧本已定稿后，仅重做 TTS + 场景背景音 + 混音 + 字幕（不重跑 LLM 升级）。

用于 improve_local.py 增量补完脚本后，把新对白/站位落到成片音频与字幕上。
用法：python3 rebuild_audio.py
"""
import json
import pathlib
import sys

PROJ = pathlib.Path("/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama")
sys.path.insert(0, str(PROJ / "scripts"))
import agnes_client
import run as generator

CFG = json.loads((PROJ / "web" / "config.json").read_text())
KEY = CFG.get("AGNES_API_KEY", "")
client = agnes_client.AgnesClient(api_key=KEY)
PD = PROJ / "output" / "excel-shen"

script = json.loads((PD / "script.json").read_text())
cp = generator.Checkpoint(PD)
vd = PD / "videos"
vid_manifest = {s["id"]: str(vd / f"{s['id']}.mp4")
                for s in script["scenes"] if (vd / f"{s['id']}.mp4").exists()}

# 清掉音频/字幕相关 checkpoint（保留 script/characters/storyboard/videos）
for k in ("tts", "sfx", "mix", "edit"):
    cp.data.pop(k, None)
cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

print(f"剧本：{script.get('title')}（{len(script.get('scenes', []))} 镜）")

tts_manifest = generator.generate_tts(script, PD / "audio", cp,
                                      scene_duration=5, vid_manifest=vid_manifest)
sfx_manifest = generator.generate_sfx(client, script, PD / "sfx", cp,
                                      generator.RateLimiter(rpm=18),
                                      vid_manifest=vid_manifest, scene_duration=5)
final = generator.mix_audio(PD, script, tts_manifest, sfx_manifest, cp,
                            scene_duration=5, vid_manifest=vid_manifest)
srt = PD / "subtitle.srt"
generator.generate_srt(script, srt, scene_duration=5, vid_manifest=vid_manifest)
if final:
    generator.burn_subtitles(PD, srt, pathlib.Path(final))

print("\n✅ 下游重渲染完成：", final)
