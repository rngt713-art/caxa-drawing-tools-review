import base64
import os
import re
import subprocess
import time

import requests


class VisionError(Exception):
    pass


def image_message(image_path: str) -> dict:
    """构造 OpenAI 格式的图片消息块（data URL），供多轮对话 history 复用。"""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    return {"type": "image_url",
            "image_url": {"url": "data:image/%s;base64,%s" % (ext, b64)}}


class LocalVision:
    def __init__(self, server: str = "http://127.0.0.1:8081",
                 start_script: str = r"C:\path\to\local-vision\start_server.ps1",
                 enable_thinking: bool = False):
        self.server = server.rstrip("/")
        self.start_script = start_script
        # 思维链默认关闭：转录/定位类任务不需要深度推理，关闭后
        # ① 上下文预算可控（开启时 thinking token 可能数千，4k/8k 槽会溢出）；
        # ② 输出更稳定（避免模型“想多了”改写字段）；
        # ③ 单请求耗时大幅下降（35B MoE 生成慢）。可用设置 vision_thinking 开起来 A/B 对比。
        self.enable_thinking = enable_thinking

    def healthy(self, timeout: float = 5.0) -> bool:
        try:
            r = requests.get(self.server + "/health", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _is_local_server(self) -> bool:
        """是否本机地址。局域网客户端指向中央部署机时不是本机，不应自动启动服务。"""
        host = self.server.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        return host in ("", "127.0.0.1", "localhost", "::1")

    def _server_port(self) -> str:
        """从配置地址里取端口，自动启动本地服务时按同一端口起（避免端口不一致连不上）。"""
        m = re.match(r"^https?://[^/:]+:(\d+)", self.server)
        return m.group(1) if m else "8081"

    def ensure_server(self, wait: float = 180.0):
        if self.healthy():
            return True
        if not self._is_local_server():
            raise VisionError(
                "远程视觉服务不可达：%s\n请确认中央部署机的视觉服务已启动，"
                "且防火墙已放行该端口（局域网客户端不会自动启动视觉服务）。" % self.server)
        if not self.start_script or not os.path.exists(self.start_script):
            raise VisionError("本地视觉服务未运行，且找不到启动脚本: %s" % self.start_script)
        try:
            subprocess.Popen(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", self.start_script,
                 "-Port", self._server_port()],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as ex:
            raise VisionError("启动视觉服务失败: %s" % ex)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.healthy():
                return True
            time.sleep(3)
        raise VisionError("视觉服务启动超时")

    def ask(self, image_path: str, prompt: str, max_tokens: int = 1200,
            timeout: float = 180.0, history: list = None) -> str:
        """单轮带图问答；history 为 OpenAI 格式的历史消息列表（可含图片），
        用于同一窗口内的多轮带图对话（如先定位区域、再对裁剪图转录）。"""
        self.ensure_server()
        messages = list(history or [])
        messages.append({
            "role": "user",
            "content": [image_message(image_path),
                        {"type": "text", "text": prompt}],
        })
        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "chat_template_kwargs": {"enable_thinking": self.enable_thinking},
        }
        try:
            r = requests.post(self.server + "/v1/chat/completions", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as ex:
            raise VisionError("视觉识别失败: %s" % ex)
