#!/bin/bash
# 收尾脚本：run.py 生成完成后，自动压缩成片、生成 GIF、替换示例 #4、提交推送并部署 Vercel
PROJ=/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama
LOG=$PROJ/output/excel-shen/finish.log
exec > "$LOG" 2>&1

echo "$(date) finish.sh 启动，等待 run.py 结束..."
while pgrep -f "run.py --theme" >/dev/null; do sleep 30; done
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

# GIF 预览（前 8 秒）
GIF=$PROJ/docs/gifs/ex_excel-shen.gif
ffmpeg -y -i "$TARGET" -t 8 -vf "fps=10,scale=360:-2:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse" -loop 0 "$GIF"
echo "GIF: $(du -h "$GIF" | cut -f1)"

# 替换 README / 首页 示例 #4
python3 $PROJ/scripts/patch_excel.py

# 提交推送 GitHub
cd $PROJ
git add api/videos/ex_excel-shen.mp4 docs/gifs/ex_excel-shen.gif README.md api/index.html scripts/run.py scripts/patch_excel.py
git commit -m "feat: 新增 3 分钟离谱漫剧《社畜周报封印魔尊》替代示例#4

- scripts/run.py: 剧本 max_tokens 提至 16000 并加解析失败重试(支持长剧本完整输出)
- 新增 api/videos/ex_excel-shen.mp4(压缩后)与 docs/gifs/ex_excel-shen.gif
- README/首页示例#4 由『AI 觉醒保护人类』替换为新漫剧"
git push origin main

# 部署 Vercel（使完整视频在线可播放）
cd $PROJ
vercel deploy --prod --yes || echo "WARN: vercel deploy 失败（不影响 GitHub）"

echo "$(date) 全部完成"
