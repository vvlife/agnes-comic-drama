#!/bin/bash
# 收尾脚本：run.py 修复重跑结束后，压缩成片、重生成 GIF、提交推送并部署 Vercel
PROJ=/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama
LOG=$PROJ/output/excel-shen/finish.log
exec > "$LOG" 2>&1

echo "$(date) fix_finish 启动，等待 run.py 结束..."
while pgrep -f "run.py --theme" >/dev/null; do sleep 20; done
echo "$(date) run.py 已结束"

OUT=$PROJ/output/excel-shen
FINAL=""
for f in final_with_audio.mp4 final.mp4; do
  if [ -f "$OUT/$f" ]; then FINAL="$OUT/$f"; break; fi
done
if [ -z "$FINAL" ]; then echo "ERROR: 未找到成片，终止"; exit 1; fi
echo "成片: $FINAL ($(du -h "$FINAL" | cut -f1))"

# 压缩到 < 100MB（GitHub 单文件硬限制）
TARGET=$PROJ/api/videos/ex_excel-shen.mp4
ffmpeg -y -i "$FINAL" -vf "scale=960:-2" -c:v libx264 -crf 30 -preset medium -c:a aac -b:a 96k "$TARGET"
echo "一次压缩后: $(du -h "$TARGET" | cut -f1)"
SIZE=$(stat -f%z "$TARGET")
if [ "$SIZE" -gt 95000000 ]; then
  ffmpeg -y -i "$FINAL" -vf "scale=854:-2" -c:v libx264 -crf 32 -preset medium -c:a aac -b:a 80k "$TARGET"
  echo "二次压缩后: $(du -h "$TARGET" | cut -f1)"
fi

# GIF 预览（前 8 秒，含字幕可体现对齐）
GIF=$PROJ/docs/gifs/ex_excel-shen.gif
ffmpeg -y -i "$TARGET" -t 8 -vf "fps=10,scale=360:-2:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse" -loop 0 "$GIF"
echo "GIF: $(du -h "$GIF" | cut -f1)"

# 提交推送 GitHub（README/首页#4 此前已替换，此处仅更新视频/GIF/脚本）
cd "$PROJ"
git add api/videos/ex_excel-shen.mp4 docs/gifs/ex_excel-shen.gif scripts/run.py
git commit -m "fix: 修复配音与字幕时间轴不一致

- 字幕 SRT 与配音 TTS 统一使用成片真实时间轴(精确模拟 xfade 拼接)
- 消除 lipsync 视频内已混音 TTS 被 mix 阶段二次叠加导致的双重配音
- 成片声画现在严格对齐" -q
git push origin main

# 部署 Vercel（更新在线完整视频）
cd "$PROJ"
vercel deploy --prod --yes || echo "WARN: vercel deploy 失败（不影响 GitHub）"

echo "$(date) 修复完成"
