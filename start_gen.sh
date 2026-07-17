#!/bin/bash
# 启动 3 分钟离谱漫剧生成（后台运行，日志落盘）
cd /Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama/scripts
KEY=$(python3 -c "import json;print(json.load(open('../web/config.json'))['AGNES_API_KEY'])")
export AGNES_API_KEY="$KEY"
rm -rf ../output/excel-shen
mkdir -p ../output/excel-shen
nohup python3 run.py --theme "社畜的周报PPT觉醒神力,用Excel表格封印万年魔尊" --style 三渲二国风 --genre 仙侠 --duration 180 --scene-duration 5 --output ../output/excel-shen > ../output/excel-shen/generate.log 2>&1 &
echo "STARTED pid $!"
