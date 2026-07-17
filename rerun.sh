#!/bin/bash
# 重跑管线：修复声画对齐后，只重做 edit/TTS/lipsync/mix（视频已存在会跳过）
PROJ=/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama
KEY=$(python3 -c "import json;print(json.load(open('$PROJ/web/config.json'))['AGNES_API_KEY'])")
export AGNES_API_KEY="$KEY"
cd "$PROJ/scripts"
OUT=$PROJ/output/excel-shen

# 清理需重做的产物
rm -f "$OUT/final.mp4" "$OUT/final_lipsync.mp4" "$OUT/final_with_audio.mp4" "$OUT/tts_mixed.m4a" "$OUT/subtitle.srt"
rm -rf "$OUT/lipsync" "$OUT/audio"

# 清 checkpoint 中 edit/tts/lipsync/mix 标记，保留 script/characters/storyboard/videos
python3 - "$OUT/.checkpoint.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for k in ("edit", "tts", "lipsync", "mix"):
    d.pop(k, None)
# 同时清镜头级 lipsync 标记（如有）
for k in list(d.keys()):
    if k.startswith("lipsync."):
        d.pop(k, None)
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("checkpoint 剩余:", sorted(d.keys()))
PY

nohup python3 run.py --theme "社畜的周报PPT觉醒神力,用Excel表格封印万年魔尊" --style 三渲二国风 --genre 仙侠 --duration 180 --scene-duration 5 --output ../output/excel-shen > "$OUT/generate.log" 2>&1 &
echo "RERUN STARTED pid $!"
