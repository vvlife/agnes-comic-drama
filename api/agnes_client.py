#!/usr/bin/env python3
"""
Agnes AI API 客户端
支持：文本生成、图像生成（文生图/图生图）、视频生成（文生视频/图生视频/关键帧动画）

兼容 OpenAI 格式的 Agnes API：
  - Chat: POST /v1/chat/completions
  - Image: POST /v1/images/generations
  - Video: POST /v1/videos, GET /agnesapi?video_id=<ID>

环境变量：AGNES_API_KEY
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import time
import requests


BASE_URL = "https://apihub.agnes-ai.com/v1"
TIMEOUT_CHAT = 180  # 剧本生成等长输出需要更长超时
TIMEOUT_IMAGE = 180  # 图像生成可能较慢
TIMEOUT_VIDEO_SUBMIT = 180
TIMEOUT_VIDEO_POLL = 20
POLL_INTERVAL = 5  # 秒
POLL_TIMEOUT = 900  # 15 分钟


class AgnesClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("AGNES_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "缺少 AGNES_API_KEY 环境变量。\n"
                "请设置：export AGNES_API_KEY='your_key_here'\n"
                "或在 OpenClaw config 中配置 skills.entries.agnes-comic-drama.env.AGNES_API_KEY"
            )
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self.base_url = BASE_URL

    # ===================== Chat =====================

    def chat(
        self,
        messages: list[dict],
        model: str = "agnes-2.0-flash",
        temperature: float = 0.8,
        max_tokens: int = 8192,
        enable_thinking: bool = False,
        retries: int = 3,
    ) -> str:
        """调用 agnes-2.0-flash 生成文本，返回 content 字符串。含重试逻辑。"""
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if enable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}

        last_err = None
        for attempt in range(retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=body,
                    timeout=TIMEOUT_CHAT,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                wait = 10 * (attempt + 1)
                print(f"  ⚠️ Chat 请求超时/连接失败，{wait}s 后重试（{attempt+1}/{retries}）...")
                time.sleep(wait)
        raise RuntimeError(f"Chat 请求 {retries} 次均失败: {last_err}")

    # ===================== Image =====================

    def generate_image(
        self,
        prompt: str,
        size: str = "1024x1792",
        model: str = "agnes-image-2.1-flash",
        reference_images: list[str] | None = None,
        response_format: str = "url",  # "url" or "b64_json"
        retries: int = 2,
    ) -> str:
        """
        文生图 / 图生图。返回图片 URL 或 Base64 字符串。

        reference_images: 图片 URL 列表（最多 4 张）
            ⚠️ 必须放在 extra_body.image 中，不能放顶层！
        response_format: "url" | "b64_json"
            ⚠️ 必须放在 extra_body 中，放根级会 400！
        """
        # 构建请求体
        body: dict = {
            "model": model,
            "prompt": prompt,
            "size": size,
        }

        # 图生图：image 必须放在 extra_body 中
        extra_body: dict = {}
        if reference_images:
            # 支持本地文件路径 → data URI 自动转换
            urls = [self._to_data_uri_or_url(img) for img in reference_images]
            extra_body["image"] = urls

        # response_format 必须放在 extra_body 中
        if response_format == "b64_json":
            body["return_base64"] = True  # 文生图 Base64 用这个
        else:
            extra_body["response_format"] = "url"

        if extra_body:
            body["extra_body"] = extra_body

        last_err = None
        for attempt in range(retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/images/generations",
                    headers=self.headers,
                    json=body,
                    timeout=TIMEOUT_IMAGE,
                )
                resp.raise_for_status()
                break
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                if attempt < retries - 1:
                    print(f"  ⚠️ Image 请求超时，10s 后重试（{attempt+1}/{retries}）...")
                    time.sleep(10)
                else:
                    raise RuntimeError(f"Image 请求 {retries} 次均失败: {last_err}")
        data = resp.json()

        # 解析响应
        if response_format == "url":
            url = data.get("data", [{}])[0].get("url", "")
            if not url:
                raise RuntimeError(f"图像生成未返回 URL：{data}")
            return url
        else:
            b64 = data.get("data", [{}])[0].get("b64_json", "")
            if not b64:
                raise RuntimeError(f"图像生成未返回 Base64：{data}")
            return b64

    def generate_image_to_file(
        self,
        prompt: str,
        out_path: str | pathlib.Path,
        size: str = "1024x1792",
        reference_images: list[str] | None = None,
        response_format: str = "url",
    ) -> pathlib.Path:
        """生成图片并保存到本地文件。"""
        out = pathlib.Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        result = self.generate_image(
            prompt=prompt,
            size=size,
            reference_images=reference_images,
            response_format=response_format,
        )

        if response_format == "url":
            # 下载图片
            r = requests.get(result, stream=True, timeout=120)
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        else:
            # Base64 解码保存
            img_data = base64.b64decode(result)
            out.write_bytes(img_data)

        return out

    # ===================== Video =====================

    def submit_video(
        self,
        prompt: str,
        model: str = "agnes-video-v2.0",
        image: str | None = None,           # 图生视频：单张图片 URL
        reference_images: list[str] | None = None,  # 多图/关键帧
        mode: str | None = None,             # "keyframes"
        height: int = 768,
        width: int = 1152,
        num_frames: int = 121,
        frame_rate: int = 24,
        seed: int | None = None,
        negative_prompt: str | None = None,
    ) -> tuple[str, str]:
        """
        提交视频生成任务。
        返回 (task_id, video_id)。
        ⚠️ 查询时请用 video_id，不要用 task_id！
        """
        body: dict = {
            "model": model,
            "prompt": prompt,
            "height": height,
            "width": width,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }

        # 图生视频：单张图片用顶层 image 参数
        if image:
            body["image"] = image

        # 多图视频/关键帧：用 extra_body.image
        if reference_images:
            if "extra_body" not in body:
                body["extra_body"] = {}
            body["extra_body"]["image"] = reference_images
        if mode:
            if "extra_body" not in body:
                body["extra_body"] = {}
            body["extra_body"]["mode"] = mode

        if seed is not None:
            body["seed"] = seed
        if negative_prompt:
            body["negative_prompt"] = negative_prompt

        resp = requests.post(
            f"{self.base_url}/videos",
            headers=self.headers,
            json=body,
            timeout=TIMEOUT_VIDEO_SUBMIT,
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id") or data.get("id", "")
        video_id = data.get("video_id", "")
        if not video_id:
            raise RuntimeError(f"视频提交响应缺少 video_id：{data}")
        return task_id, video_id

    def poll_video(
        self,
        video_id: str,
        timeout_s: int = POLL_TIMEOUT,
        interval_s: int = POLL_INTERVAL,
    ) -> dict:
        """
        用 video_id 轮询视频任务状态。
        ⚠️ 必须用 video_id，不要用 task_id！
        返回完整响应 dict（含 video_url）。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(interval_s)
            try:
                resp = requests.get(
                    f"{BASE_URL.replace('/v1', '')}/agnesapi",
                    params={"video_id": video_id},
                    headers=self.headers,
                    timeout=TIMEOUT_VIDEO_POLL,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  轮询请求失败：{e}，重试中...")
                continue

            status = data.get("status", "")
            progress = data.get("progress", 0)
            print(f"  视频任务状态：{status}，进度：{progress}%")

            if status == "completed":
                return data
            if status == "failed":
                error = data.get("error", "未知错误")
                raise RuntimeError(f"视频任务失败：{error}，video_id={video_id}")

        raise TimeoutError(f"视频任务 {video_id} 超时（{timeout_s}s）")

    def download_video(self, url: str, out_path: pathlib.Path) -> pathlib.Path:
        """下载视频到本地。"""
        out_path = pathlib.Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  下载视频：{url[:80]}...")
        r = requests.get(url, stream=True, timeout=600)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        print(f"  视频已保存：{out_path}")
        return out_path

    def generate_video_full(
        self,
        prompt: str,
        out_path: str | pathlib.Path,
        image: str | None = None,
        reference_images: list[str] | None = None,
        mode: str | None = None,
        height: int = 768,
        width: int = 1152,
        num_frames: int = 121,
        frame_rate: int = 24,
        seed: int | None = None,
    ) -> pathlib.Path:
        """
        一站式：提交视频任务 → 轮询 → 下载。
        返回本地视频路径。
        """
        out_path = pathlib.Path(out_path)
        print(f"📹 提交视频任务...")
        print(f"  Prompt: {prompt[:80]}...")
        if image:
            print(f"  参考图: {image[:80]}...")

        task_id, video_id = self.submit_video(
            prompt=prompt,
            image=image,
            reference_images=reference_images,
            mode=mode,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=frame_rate,
            seed=seed,
        )
        print(f"  task_id={task_id}, video_id={video_id}")
        print(f"⏳ 轮询视频结果（video_id={video_id}）...")

        result = self.poll_video(video_id)
        video_url = result.get("remixed_from_video_id") or result.get("video_url", "")
        if not video_url:
            raise RuntimeError(f"视频完成但未返回 URL：{result}")

        print(f"✅ 视频生成完成：{video_url[:80]}...")
        return self.download_video(video_url, out_path)

    # ===================== 工具 =====================

    @staticmethod
    def _to_data_uri_or_url(path_or_url: str) -> str:
        """如果是 URL 直接返回；如果是本地文件转 data URI。"""
        if path_or_url.startswith(("http://", "https://", "data:")):
            return path_or_url
        p = pathlib.Path(path_or_url)
        if not p.exists():
            # 可能是 URL 但没加协议头
            if "agnes" in path_or_url or "storage.googleapis" in path_or_url:
                return "https://" + path_or_url.lstrip("/")
            raise FileNotFoundError(f"图片文件不存在：{path_or_url}")
        ext = p.suffix.lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/{ext};base64,{b64}"


# ===================== CLI 入口 =====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agnes AI API 客户端")
    subparsers = parser.add_subparsers(dest="command")

    # chat
    p_chat = subparsers.add_parser("chat", help="文本生成")
    p_chat.add_argument("--prompt", required=True)
    p_chat.add_argument("--model", default="agnes-2.0-flash")
    p_chat.add_argument("--temperature", type=float, default=0.8)
    p_chat.add_argument("--max-tokens", type=int, default=8192)

    # image
    p_img = subparsers.add_parser("image", help="图像生成")
    p_img.add_argument("--prompt", required=True)
    p_img.add_argument("--out", required=True, help="输出文件路径")
    p_img.add_argument("--size", default="1024x1792")
    p_img.add_argument("--model", default="agnes-image-2.1-flash")
    p_img.add_argument("--reference", nargs="*", default=[], help="参考图片 URL/路径")
    p_img.add_argument("--format", default="url", choices=["url", "b64_json"])

    # video
    p_vid = subparsers.add_parser("video", help="视频生成")
    p_vid.add_argument("--prompt", required=True)
    p_vid.add_argument("--out", required=True, help="输出视频路径")
    p_vid.add_argument("--image", help="参考图片 URL（图生视频）")
    p_vid.add_argument("--height", type=int, default=768)
    p_vid.add_argument("--width", type=int, default=1152)
    p_vid.add_argument("--num-frames", type=int, default=121)
    p_vid.add_argument("--frame-rate", type=int, default=24)

    args = parser.parse_args()

    client = AgnesClient()

    if args.command == "chat":
        messages = [{"role": "user", "content": args.prompt}]
        result = client.chat(messages, model=args.model, temperature=args.temperature, max_tokens=args.max_tokens)
        print(result)

    elif args.command == "image":
        out = client.generate_image_to_file(
            prompt=args.prompt,
            out_path=args.out,
            size=args.size,
            reference_images=args.reference or None,
            response_format=args.format,
        )
        print(f"✅ 图片已保存：{out}")

    elif args.command == "video":
        out = client.generate_video_full(
            prompt=args.prompt,
            out_path=args.out,
            image=args.image,
            height=args.height,
            width=args.width,
            num_frames=args.num_frames,
            frame_rate=args.frame_rate,
        )
        print(f"✅ 视频已保存：{out}")

    else:
        parser.print_help()
