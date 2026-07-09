#!/usr/bin/env python3
"""将 README 与首页的示例 #4 由『AI 觉醒保护人类』替换为新漫剧《社畜周报封印魔尊》。"""
import pathlib

PROJ = pathlib.Path("/Users/nxhuang/CodeBuddy/20260707102109/agnes-comic-drama")

# ---------- README ----------
readme = PROJ / "README.md"
t = readme.read_text(encoding="utf-8")
old_r = """### 4. AI 觉醒保护人类
![AI 觉醒保护人类](docs/gifs/FreeToken_preview.gif)
[▶ 观看完整视频](https://agnes-comic-drama.vercel.app/api/static-video/FreeToken_preview.mp4)"""
new_r = """### 4. 社畜周报封印魔尊
![社畜周报封印魔尊](docs/gifs/ex_excel-shen.gif)
[▶ 观看完整视频](https://agnes-comic-drama.vercel.app/api/static-video/ex_excel-shen.mp4)"""
assert old_r in t, "README #4 未匹配，请检查文本"
t = t.replace(old_r, new_r)
readme.write_text(t, encoding="utf-8")
print("✅ README 示例 #4 已替换")

# ---------- 首页 api/index.html ----------
idx = PROJ / "api/index.html"
t = idx.read_text(encoding="utf-8")
old_i = """  {
    theme: 'AI 觉醒后选择保护创造它的人类',
    style: '赛博朋克', genre: '都市', duration: 30,
    emoji: '🤖', gradient: 'linear-gradient(135deg, #0a0a2a, #2d1a5c, #6a2d8f)',
    desc: '未来都市中，AI 与人类的情感羁绊',
    video: '/api/static-video/FreeToken_preview.mp4'
  },"""
new_i = """  {
    theme: '社畜的周报PPT觉醒神力，用Excel封印万年魔尊',
    style: '三渲二国风', genre: '仙侠', duration: 180,
    emoji: '📊', gradient: 'linear-gradient(135deg, #1a2a0a, #2a5c2d, #4a8f6a)',
    desc: '离谱社畜用Excel表格逆袭仙门，封印万年魔尊',
    video: '/api/static-video/ex_excel-shen.mp4'
  },"""
assert old_i in t, "首页 #4 未匹配，请检查文本"
t = t.replace(old_i, new_i)
idx.write_text(t, encoding="utf-8")
print("✅ 首页示例 #4 已替换")
