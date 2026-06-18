# 🎭 Agnes 漫剧生成器

> 基于 [Agnes AI](https://platform.agnes-ai.com) 免费全模态 API，零成本生成 AI 漫剧。

**剧本 → 角色卡 → 分镜关键帧 → 图生视频 → TTS配音 → 成片拼接**，全流程免费。

---

## ✨ 特性

- 🆓 **完全免费** — 使用 Agnes AI 免费额度（RPM ≤ 20），不花一分钱
- 🎬 **全流程** — 一键从主题到成片，自动执行所有步骤
- 🎨 **多风格** — 三渲二国风、水墨、赛博朋克、日系动漫
- 🎭 **多类型** — 仙侠、宫斗、江湖、都市
- 📊 **实时进度** — Web UI 可视化进度条 + 实时日志
- 🔧 **续跑支持** — Checkpoint 机制，失败后不重做已完成步骤
- 🎙️ **TTS 配音** — edge-tts 跨平台语音合成，自动角色分配
- 📧 **邮件通知** — 生成完成后自动发送邮件（可选）
- 🔑 **本地配置** — API KEY 仅保存在本地，不上传任何服务器

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install flask flask-cors edge-tts
```

> 视频拼接需要本地安装 [FFmpeg](https://ffmpeg.org/)。

### 2. 获取 API KEY

前往 [platform.agnes-ai.com](https://platform.agnes-ai.com) 免费注册，获取 API KEY。

### 3. 启动 Web 服务

```bash
cd agnes-comic-drama/web
python3 server.py --port 7788
```

打开浏览器访问 **http://127.0.0.1:7788**，点击右上角 ⚙️ 配置 API KEY，即可开始生成。

### 3'. 命令行方式（可选）

```bash
export AGNES_API_KEY="sk-your-key-here"
cd agnes-comic-drama/scripts
python3 run.py --theme "少年剑仙三年归来" --style "三渲二国风" --genre "仙侠" --duration 15
```

---

## 📁 项目结构

```
agnes-comic-drama/
├── SKILL.md                  # 技能定义
├── README.md                 # 本文件
├── .gitignore
├── requirements.txt          # Python 依赖
├── scripts/
│   ├── agnes_client.py       # Agnes API 封装（文本/图像/视频）
│   ├── run.py                # 命令行全流程入口
│   └── supabase_storage.py   # Supabase 持久化（Vercel 用）
├── api/
│   ├── index.py              # Vercel Serverless 版本
│   └── index.html            # Vercel 前端
├── web/
│   ├── server.py             # Flask Web 后端
│   ├── static/               # 旧版前端
│   └── config.json           # 本地配置（API KEY，git 已忽略）
├── public/
│   └── index.html            # 新版前端 SPA
└── vercel.json               # Vercel 路由配置
```

---

## 🔧 配置

API KEY 优先级（从高到低）：

1. **Web UI 配置页面**（保存到 `web/config.json`）
2. **环境变量** `AGNES_API_KEY`
3. **OpenClaw 配置** `~/.qclaw/openclaw.json`

邮件配置（可选）：
- `RESEND_API_KEY` — Resend API Key
- `EMAIL_FROM` — 发件人地址

所有配置仅保存在本地，不会上传到任何远程服务器。

---

## 🎬 生成流程

| Step | 内容 | API 模型 | 免费 |
|------|------|----------|------|
| 1 | 📝 剧本生成 | agnes-2.0-flash | ✅ |
| 2 | 🎨 角色三联卡 | agnes-image-2.1-flash | ✅ |
| 3 | 🖼️ 分镜关键帧 | agnes-image-2.1-flash | ✅ |
| 4 | 📹 图生视频 | agnes-video-v2.0 | ✅ |
| 5 | 🎙️ TTS 配音 | edge-tts（本地） | ✅ |
| 6 | 🔊 音效描述 | agnes-2.0-flash | ✅ |
| 7 | 🎚️ 音频混音 | FFmpeg（本地） | ✅ |
| 8 | 🎬 成片拼接 | FFmpeg（本地） | ✅ |

---

## 🧩 API 模型映射

| 用途 | 模型 | 说明 |
|------|------|------|
| 文本生成 | `agnes-2.0-flash` | 256K 上下文，支持 Thinking 模式 |
| 图像生成 | `agnes-image-2.1-flash` | 文生图 + 图生图（多图参考） |
| 视频生成 | `agnes-video-v2.0` | 图生视频，异步任务 |

Base URL: `https://apihub.agnes-ai.com/v1`

---

## 🌐 部署

### 本地部署

```bash
cd web
python3 server.py --host 0.0.0.0 --port 8080
```

### Vercel 部署

1. Fork 本仓库
2. 在 Vercel Dashboard 添加环境变量 `AGNES_API_KEY`
3. 自动部署

### 服务器部署

```bash
# 安装依赖
apt-get install -y ffmpeg fonts-wqy-zenhei fonts-wqy-microhei
pip install flask flask-cors edge-tts

# 启动服务
nohup python3 web/server.py --host 0.0.0.0 --port 8080 > /tmp/agnes-comic-drama.log 2>&1 &
```

---

## ⚠️ 注意事项

- **RPM 限制**：免费账户 20 次/分钟，生成过程自动限流
- **视频生成**：异步任务，需轮询查询结果（使用 `video_id`）
- **图生图**：`image` 参数必须放在 `extra_body` 中
- **视频时长**：`num_frames` ≤ 441 且满足 8n+1（81/121/161/241/441）
- **角色一致性**：通过 Agnes Image 多图参考（最多 4 张角色卡）
- **中文字幕**：服务器需安装中文字体（`fonts-wqy-zenhei`）
- **邮件功能**：需配置 `RESEND_API_KEY`，视频附件限制 50MB

---

## 📜 License

MIT

---

## 🙏 致谢

- [Agnes AI](https://platform.agnes-ai.com) — 免费全模态 API
- [FFmpeg](https://ffmpeg.org/) — 视频处理
- [edge-tts](https://github.com/rany2/edge-tts) — 语音合成
