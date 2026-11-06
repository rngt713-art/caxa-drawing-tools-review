import base64
import ctypes
from ctypes import wintypes
import json
import os
import sys

try:
    import win32crypt
except Exception:
    win32crypt = None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "caxa-uploader",
        None,
        None,
        None,
        0x1,
        ctypes.byref(target),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(target),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)

APP_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "caxa_server_ip": "caxa.example.internal",
    "caxa_server_port": 8900,
    "caxa_user": "",
    "caxa_password_enc": "",
    "vision_server": "http://127.0.0.1:8081",
    "vision_script": r"C:\path\to\local-vision\start_server.ps1",
    "vision_thinking": False,
    "timeout_seconds": 60,
    "pending_folder": "",
    "excel_output": "",
    "dup_new_version": False,
    "borrow_only_missing": True,
    "duplicate_policy": "append_iteration",
    "start_workflow_after_upload": False,
    "workflow_performer": "审核人",
    "workflow_template_id": 1,
    "workflow_template_instance_id": 31,
    "workflow_process_def_id": "2",
    "workflow_activity_def_id": "4",
}


def _protect(text: str) -> str:
    if not text:
        return ""
    try:
        raw = text.encode("utf-16-le")
        if win32crypt is not None:
            blob = win32crypt.CryptProtectData(
                raw, "caxa-uploader", None, None, None, 0
            )
        else:
            blob = _dpapi_protect(raw)
        return base64.b64encode(blob).decode("ascii")
    except Exception:
        return ""


def _unprotect(enc: str) -> str:
    if not enc:
        return ""
    try:
        blob = base64.b64decode(enc)
        if win32crypt is not None:
            data = win32crypt.CryptUnprotectData(
                blob, None, None, None, 0
            )[1]
        else:
            data = _dpapi_unprotect(blob)
        return data.decode("utf-16-le")
    except Exception:
        return ""


def load() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except Exception:
            pass
    # 局域网客户端：启动脚本可用环境变量 VISION_SERVER 覆盖视觉服务地址，
    # 用户不用进设置（如“客户端启动.bat”）
    env_vision = os.environ.get("VISION_SERVER", "").strip()
    if env_vision:
        cfg["vision_server"] = env_vision
    # 思维链开关只留在中央机：环境变量 VISION_THINKING=1 可开启（客户端无此入口）
    env_think = os.environ.get("VISION_THINKING", "").strip()
    if env_think:
        cfg["vision_thinking"] = env_think.lower() in ("1", "true", "yes", "on")
    return cfg


def save(cfg: dict):
    clean = {k: cfg.get(k, DEFAULTS.get(k, "")) for k in DEFAULTS}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def set_credentials(user: str, password: str):
    cfg = load()
    cfg["caxa_user"] = user
    cfg["caxa_password_enc"] = _protect(password)
    save(cfg)


def get_password(cfg: dict) -> str:
    return _unprotect(cfg.get("caxa_password_enc", ""))
