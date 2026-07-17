#!/usr/bin/env python3
"""就地优化 excel-shen 漫剧：升级剧本(对白/站位/描述翻倍) + 重做 TTS + 合成场景背景音 + 混音。

不重新生成视频，直接在当前视频上套用新的对白与背景音，快速见效。
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
if not KEY:
    raise SystemExit("未找到 AGNES_API_KEY")
# 兼容 openclaw
if not KEY:
    oc = pathlib.Path.home() / ".qclaw" / "openclaw.json"
    if oc.exists():
        KEY = json.loads(oc.read_text()).get("skills", {}).get("entries", {}).get("agnes-comic-drama", {}).get("env", {}).get("AGNES_API_KEY", "")

PD = PROJ / "output" / "excel-shen"
client = agnes_client.AgnesClient(api_key=KEY)
rl = generator.RateLimiter(rpm=18)
cp = generator.Checkpoint(PD)

script = json.loads((PD / "script.json").read_text())
print(f"原剧本：{script.get('title')}（{len(script.get('scenes', []))} 镜）")

# 1) 升级剧本（保留剧情，重写对白/站位/描述）
improved = generator.improve_existing_script(client, script, rl, PD / "script.json")
print(f"升级后剧本：{improved.get('title')}（{len(improved.get('scenes', []))} 镜）")

# 视频清单
vd = PD / "videos"
vid_manifest = {s["id"]: str(vd / f"{s['id']}.mp4")
                for s in improved["scenes"] if (vd / f"{s['id']}.mp4").exists()}

# 清理需重做的音频/字幕 checkpoint（保留 script/characters/storyboard/videos）
for k in ("tts", "sfx", "mix", "edit"):
    cp.data.pop(k, None)
cp.path.write_text(json.dumps(cp.data, ensure_ascii=False, indent=2))

# 2) 重新生成 TTS（基于新对白）
tts_manifest = generator.generate_tts(improved, PD / "audio", cp,
                                      scene_duration=5, vid_manifest=vid_manifest)
# 3) 合成场景背景音
sfx_manifest = generator.generate_sfx(client, improved, PD / "sfx", cp, rl,
                                      vid_manifest=vid_manifest, scene_duration=5)
# 4) 混音（视频 + 配音 + 背景音）
final = generator.mix_audio(PD, improved, tts_manifest, sfx_manifest, cp,
                            scene_duration=5, vid_manifest=vid_manifest)
# 5) 重新烧录字幕（基于新对白）
srt = PD / "subtitle.srt"
generator.generate_srt(improved, srt, scene_duration=5, vid_manifest=vid_manifest)
if final:
    generator.burn_subtitles(PD, srt, pathlib.Path(final))

print("\n✅ 就地优化完成")
print("成片：", final)
