---
name: agnes-comic-drama
displayName: Agnes 漫剧生成器
description: |
  基于 Agnes AI 免费全模态 API 的漫剧生成器。一键生成 3-5 分钟漫剧（国风/任意风格）。
  主题→分幕剧本→角色卡→分镜关键帧→图生视频→TTS配音→BGM→字幕→拼接成片。
  全流程使用 Agnes AI 免费API（agnes-2.0-flash + agnes-image-2.1-flash + agnes-video-v2.0），
  无需火山方舟/Kling/Suno 等付费API。
  触发词：Agnes漫剧、免费漫剧、agnes comic、零成本漫剧。
version: 1.0.0
aliases:
  - Agnes漫剧
  - 免费漫剧
  - agnes-comic
---

# Agnes 漫剧生成器

> 基于 Agnes AI 免费全模态 API，零成本生成 3-5 分钟漫剧。

---

## ⚠️ 硬规则

1. **零成本**：Agnes AI 全模态免费（RPM ≤ 20），不收一分钱
2. **用户确认闸门**：开工前必须等用户确认
3. **RPM 限流**：免费账户 20 RPM，需要合理安排并发，避免 429
4. **Checkpoint 续跑**：失败从 `.checkpoint.json` 恢复，不重做已完成步骤
5. **视频查询用 video_id**：不要用 task_id！

---

## 一、API 映射（vs 原火山方舟体系）

| 原体系 | 模型 | Agnes 替代 | 模型 |
|--------|------|-----------|------|
| 火山方舟 Seedream 4.0 | `doubao-seedream-4-0-250828` | Agnes Image 2.1 Flash | `agnes-image-2.1-flash` |
| 火山方舟 Seedance 2.0 | `doubao-seedance-2-0-260128` | Agnes Video V2.0 | `agnes-video-v2.0` |
| Claude SDK 剧本生成 | `claude-opus-4-7` | Agnes 2.0 Flash | `agnes-2.0-flash` |
| 豆包 TTS | `doubao-tts-bigtts` | **保留** | 需火山方舟 ARK_API_KEY |
| Kling 对口型 | `kling-v2.6` | **保留** | 需 KLING_API_KEY |
| Suno BGM | `suno-v5.5` | **保留** | 需 SUNO API |

**结论**：剧本、图像、视频三大核心步骤可完全免费；TTS/口型/BGM 三步需付费 API（可降级跳过）。

---

## 二、Agent 工作流

### Step 0：收集输入

必填：`theme`（主题一句话）
可选：`duration_total`（秒数，默认 180）、`style`（默认 "三渲二国风"）、`genre`（默认 "仙侠"）
可选：`enable_tts`（默认 false，需 ARK_API_KEY）、`enable_lipsync`（默认 false）、`enable_bgm`（默认 false）

### Step 1：免费成本确认

```
收到！将生成 {duration_total}s 漫剧（{n_scenes} 镜头）
主题：{theme}
风格：{style} / 类型：{genre}

✅ 核心步骤全免费（Agnes AI 免费额度）：
  · 剧本生成 ¥0.00（agnes-2.0-flash）
  · 角色卡   ¥0.00（agnes-image-2.1-flash）
  · 分镜关键帧 ¥0.00（agnes-image-2.1-flash）
  · 图生视频  ¥0.00（agnes-video-v2.0）

⚠️ 可选付费步骤：
  · TTS 配音 ¥{tts}（需 ARK_API_KEY）
  · 对口型  ¥{lipsync}（需 KLING_API_KEY）
  · BGM    ¥{bgm}（需 Suno API）

RPM 限制：20 次/分钟，预计耗时 {est_minutes} 分钟

确认开始吗？
```

### Step 2-8：按顺序执行

| Step | 内容 | API | 免费？ |
|------|------|-----|--------|
| 1 | 剧本生成 | agnes-2.0-flash | ✅ |
| 2 | 角色三联卡 | agnes-image-2.1-flash | ✅ |
| 3 | 分镜关键帧 | agnes-image-2.1-flash | ✅ |
| 4 | 图生视频 | agnes-video-v2.0 | ✅ |
| 5 | TTS 配音 | 豆包 TTS / 跳过 | ❌/跳过 |
| 6 | 对口型 | Kling / 跳过 | ❌/跳过 |
| 7 | BGM | Suno / 跳过 | ❌/跳过 |
| 8 | 成片拼接 | FFmpeg（本地） | ✅ |

### Step 9：交付

输出 `output/{project_slug}/final.mp4` 路径。

---

## 三、API 调用规范

### 3.1 通用配置

```
Base URL: https://apihub.agnes-ai.com/v1
API Key: 环境变量 AGNES_API_KEY 或 openclaw.json 中的配置
Headers:
  Authorization: Bearer {AGNES_API_KEY}
  Content-Type: application/json
```

### 3.2 文本生成（剧本/提示词优化）

```bash
curl https://apihub.agnes-ai.com/v1/chat/completions \
  -H "Authorization: Bearer $AGNES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agnes-2.0-flash",
    "messages": [
      {"role": "system", "content": "你是一位专业的漫剧剧本编剧..."},
      {"role": "user", "content": "请根据以下主题生成分幕剧本..."}
    ],
    "temperature": 0.8,
    "max_tokens": 8192
  }'
```

**Thinking 模式**（复杂推理时开启）：
```json
{
  "model": "agnes-2.0-flash",
  "messages": [...],
  "chat_template_kwargs": {
    "enable_thinking": true
  }
}
```

### 3.3 图像生成（角色卡/关键帧）

**文生图**：
```bash
curl https://apihub.agnes-ai.com/v1/images/generations \
  -H "Authorization: Bearer $AGNES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agnes-image-2.1-flash",
    "prompt": "三渲二国风动画风格，角色全身立绘...",
    "size": "1024x1792",
    "extra_body": {
      "response_format": "url"
    }
  }'
```

**图生图**（关键帧，用角色卡做参考）：
```bash
curl https://apihub.agnes-ai.com/v1/images/generations \
  -H "Authorization: Bearer $AGNES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agnes-image-2.1-flash",
    "prompt": "三渲二国风动画风格，仙侠场景...",
    "size": "768x1344",
    "extra_body": {
      "image": [
        "https://example.com/character_full.png",
        "https://example.com/character_close.png"
      ],
      "response_format": "url"
    }
  }'
```

**⚠️ 关键规则**：
- `image` 参数必须放在 `extra_body` 中！放顶层会变成文生图
- `response_format` 也必须放在 `extra_body` 中！放根级会 400
- 图生图不需要传 `tags: ["img2img"]`
- 支持多图参考（最多 4 张角色卡）

### 3.4 视频生成（图生视频）

**创建视频任务**：
```bash
curl -X POST https://apihub.agnes-ai.com/v1/videos \
  -H "Authorization: Bearer $AGNES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agnes-video-v2.0",
    "prompt": "角色缓缓转身，镜头推进，仙侠氛围",
    "image": "https://example.com/storyboard_S01.png",
    "height": 1344,
    "width": 768,
    "num_frames": 121,
    "frame_rate": 24
  }'
```

**查询视频结果**（⚠️ 必须用 video_id）：
```bash
curl "https://apihub.agnes-ai.com/agnesapi?video_id={VIDEO_ID}" \
  -H "Authorization: Bearer $AGNES_API_KEY"
```

**关键帧动画模式**：
```bash
curl -X POST https://apihub.agnes-ai.com/v1/videos \
  -H "Authorization: Bearer $AGNES_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agnes-video-v2.0",
    "prompt": "平滑过渡，保持角色一致",
    "extra_body": {
      "image": [
        "https://example.com/keyframe1.png",
        "https://example.com/keyframe2.png"
      ],
      "mode": "keyframes"
    },
    "num_frames": 121,
    "frame_rate": 24
  }'
```

**视频时长参数**：

| 时长 | num_frames | frame_rate |
|------|-----------|------------|
| ~3s | 81 | 24 |
| ~5s | 121 | 24 |
| ~10s | 241 | 24 |
| ~18s | 441 | 24 |

**⚠️ 关键规则**：
- num_frames 必须 ≤ 441 且满足 8n+1（81, 121, 161, 241, 441）
- **必须用 video_id 查询**，用 task_id 会导致排队超 5 分钟
- 轮询间隔建议 5 秒
- 图生视频用顶层 `image` 参数传图片 URL

---

## 四、RPM 限流策略

Agnes 免费 API RPM 限制为 20 次/分钟。漫剧生成需要大量 API 调用，必须合理控制：

### 调用估算（180s 漫剧，36 镜头）

| 步骤 | 调用次数 | 预计耗时 |
|------|----------|----------|
| 剧本 | 1 次 | <1s |
| 角色卡（3角色×3张） | 9 次 | ~30s（串行） |
| 分镜关键帧（36张） | 36 次 | ~2min（串行） |
| 图生视频（36个） | 36次提交 + 36次查询 | ~10-20min |
| **合计** | ~118 次 | ~15-25min |

### 限流方案

1. **串行化关键步骤**：图像和视频生成串行执行，每分钟不超过 20 次
2. **视频任务间隔**：提交间隔 ≥ 3s，避免突发请求
3. **429 退避**：遇到 429 自动等待 60s 后重试
4. **批量优化**：角色卡可 3 张/批，关键帧可 5 张/批

---

## 五、角色一致性方案

### Agnes Image 多图参考

Agnes Image 2.1 支持 `extra_body.image` 传入多张参考图。角色一致性策略：

1. **角色三联卡**：每个角色生成 3 张图（全身/半身/Q版），作为参考源
2. **分镜关键帧**：传入该镜出场角色的全身立绘做图生图参考
3. **多角色场景**：在 `extra_body.image` 中传入多个角色的参考图

```python
# 分镜关键帧 - 单角色
extra_body = {
    "image": ["https://cdn.../C1_full.png"],
    "response_format": "url"
}

# 分镜关键帧 - 双角色
extra_body = {
    "image": ["https://cdn.../C1_full.png", "https://cdn.../C2_full.png"],
    "response_format": "url"
}
```

### 注意事项
- 最多 4 张参考图
- 超过 4 张时优先保留全身立绘
- 所有 URL 必须公网可访问

---

## 六、降级策略

### 无 TTS 时（默认免费方案）

跳过 TTS + 对口型，视频保留原声或静音，叠加字幕：
- 从 script.json 提取对白文本
- 生成 SRT 字幕文件
- FFmpeg 烧录字幕到视频

### 无 BGM 时

使用本地音乐素材库（如有的话），或生成无声视频。

### 视频排队过长

- 检查是否用了 video_id 而非 task_id 查询
- 轮询间隔 5s，超时 15min 后建议用户重试

---

## 七、Prompt 模板

### 7.1 剧本生成 Prompt

```
你是一位专业的漫剧剧本编剧。请根据以下信息生成分幕剧本：

主题：{theme}
风格：{style}
类型：{genre}
总时长：{duration_total}秒
镜头数：约 {n_scenes} 个

输出 JSON 格式：
{
  "title": "剧名",
  "characters": [
    {"id": "C1", "name": "角色名", "visual": "外观描述", "personality": "性格", "age": "年龄", "voice": "音色"}
  ],
  "scenes": [
    {
      "id": "S01",
      "location": "场景描述",
      "time": "时间",
      "characters": ["C1"],
      "action": "动作描述",
      "dialogue": [{"character": "C1", "text": "台词"}],
      "camera": "镜头描述",
      "mood": "氛围"
    }
  ]
}

要求：
1. 每个镜头约 5 秒
2. 对白简短有力
3. 镜头之间有连贯性
4. 保留悬念和冲突
```

### 7.2 角色卡 Prompt

```python
full_prompt = f"{STYLE_PREFIX}，角色全身立绘，{char.visual}，{char.personality}气质，站姿，居中构图，纯色背景，高质量角色设定图"
close_prompt = f"{STYLE_PREFIX}，角色半身特写，{char.visual}，{char.personality}表情，肩部以上，正面"
chibi_prompt = f"{STYLE_PREFIX}，Q版头像，{char.visual}简化版，可爱风格，圆润线条"
```

### 7.3 分镜关键帧 Prompt

```python
prompt = f"{STYLE_PREFIX}，{scene.location}，{scene.time}。{scene.action}。{scene.camera}。{scene.mood}氛围。"
# 图生图参考
extra_body = {
    "image": [char_images[c] for c in scene.characters[:4]],  # 最多4张
    "response_format": "url"
}
```

### 7.4 图生视频 Prompt

```python
video_prompt = f"Animate: {scene.action}，{scene.camera}，保持角色外观一致，{scene.mood}氛围，cinematic quality"
```

---

## 八、项目输出结构

```
output/{project_slug}/
├── script.json              # 剧本
├── characters/
│   ├── C1_full.png          # 全身立绘
│   ├── C1_close.png         # 半身特写
│   ├── C1_chibi.png         # Q版头像
│   └── manifest.json
├── storyboard/
│   ├── S01.png              # 关键帧
│   ├── S02.png
│   └── manifest.json
├── videos/
│   ├── S01.mp4
│   └── ...
├── audio/                   # 可选（需 TTS API）
│   └── ...
├── bgm.mp3                  # 可选（需 Suno API）
├── subtitle.srt
├── final.mp4
└── .checkpoint.json
```

---

## 九、与其他漫剧体系的关系

| 体系 | 图像 | 视频 | 成本 | 推荐场景 |
|------|------|------|------|----------|
| **agnes-comic-drama** | Agnes Image 2.1 | Agnes Video 2.0 | **¥0** | 零成本体验、快速原型 |
| huo15-comic-orchestrator | Seedream 4.0 | Seedance 2.0 | ¥300-600 | 高质量成品、国风仙侠 |
| comic-drama-generate | Nano Banana Pro | Sora 2 | $付费 | 国际向、连载漫剧 |

**组合方案**：先用 agnes-comic-drama 免费验证剧本和分镜效果，满意后再用 huo15 体系出高质量成品。
