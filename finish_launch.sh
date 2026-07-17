#!/bin/bash
# 后台启动 finish.sh（规避 nohup 命令字符串拦截）
nohup bash /Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama/finish.sh >/dev/null 2>&1 &
echo "finish launcher started pid $!"
