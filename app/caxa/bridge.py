"""Python side of the CAXA helper process.

Rules encoded here:

* a helper process that was just started has no session - every new process is
  logged in before it receives a business command;
* process handle and login state are managed under one lock;
* helper errors keep their structure (``stage``, ``detail``,
  ``created_process_instance_id``, ``partial``), so a workflow that was created
  and then failed halfway is visible instead of being reduced to a string;
* non-idempotent writes are never retried blindly: a timeout or a lost reply
  raises an error marked ``response_lost`` so the caller can mark the operation
  "待核实" instead of replaying it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading

HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "caxa_helper", "caxa_helper.exe")

#: Commands that create or change remote state.  Never retried automatically.
WRITE_COMMANDS = frozenset(
    {
        "upload",
        "create_part",
        "link_part",
        "reference",
        "start_workflow",
        "new_release",
        "release_drawing",
        "set_part",
        "set_drawing",
        "delete_drawing",
    }
)


class CaxaError(Exception):
    """Structured helper/CAXA failure."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        detail: str | None = None,
        created_process_instance_id: int | None = None,
        response_lost: bool = False,
        timed_out: bool = False,
        partial: bool = False,
        result: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.detail = detail
        self.created_process_instance_id = created_process_instance_id
        self.response_lost = bool(response_lost)
        self.timed_out = bool(timed_out)
        self.partial = bool(partial)
        self.result = result or {}

    @property
    def uncertain(self) -> bool:
        """True when the write may have happened but cannot be confirmed."""
        return (
            self.response_lost
            or self.timed_out
            or bool(self.result.get("uncertain"))
        )

    def as_dict(self) -> dict:
        """Structured error for logs/journal; never contains credentials."""
        return {
            "message": self.message,
            "stage": self.stage,
            "detail": self.detail,
            "created_process_instance_id": self.created_process_instance_id,
            "response_lost": self.response_lost,
            "timed_out": self.timed_out,
            "partial": self.partial,
            "uncertain": self.uncertain,
            "result": self.result,
        }


class CaxaBridge:
    def __init__(self, helper_path: str = HELPER_PATH):
        self.helper_path = helper_path
        self._proc = None
        self._lock = threading.RLock()
        self._login_params: dict | None = None
        self._logged_in = False
        self._user = ""

    # ---------- process management (call with the lock held) ----------
    def _spawn(self):
        self._proc = subprocess.Popen(
            [self.helper_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._logged_in = False

    def start(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _drop_process(self):
        proc = self._proc
        self._proc = None
        self._logged_in = False
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def stop(self):
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.write('{"cmd":"logout"}\n')
                    self._proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None
            self._logged_in = False
            self._login_params = None

    # ---------- login ----------
    def login(self, server_ip: str, server_port: int, user: str, password: str) -> dict:
        with self._lock:
            self._login_params = {
                "cmd": "login",
                "server_ip": server_ip,
                "server_port": int(server_port),
                "user": user,
                "password": password,
            }
            self._user = user
            self._logged_in = False
            return self._login_locked()

    def _login_locked(self) -> dict:
        params = self._login_params
        if not params:
            raise CaxaError("尚未提供登录参数", stage="login")
        if not self._alive():
            self._spawn()
        try:
            self._send_raw(params)
            line = self._readline(60.0)
        except CaxaError as exc:
            self._logged_in = False
            raise CaxaError(
                "CAXA 助手登录无响应：%s" % exc,
                stage="login",
                detail=exc.detail,
                timed_out=exc.timed_out,
            ) from exc
        if line is None:
            self._drop_process()
            raise CaxaError("CAXA 助手登录无响应（进程已退出）", stage="login")
        try:
            resp = json.loads(line)
        except ValueError as exc:
            self._drop_process()
            raise CaxaError(
                "CAXA 助手登录返回无法解析", stage="login", detail=str(exc)
            ) from exc
        if not resp.get("ok") or resp.get("logged_in") is False:
            self._logged_in = False
            raise CaxaError(
                resp.get("error") or "登录失败",
                stage=resp.get("stage") or "login",
                detail=resp.get("detail"),
            )
        self._logged_in = True
        return resp

    def ensure_logged_in(self):
        with self._lock:
            self._ensure_ready()

    def _ensure_ready(self):
        """Guarantee: alive process + completed login, before any business call."""
        if not self._alive():
            self._spawn()
        if not self._logged_in:
            if not self._login_params:
                raise CaxaError("CAXA 助手未登录", stage="login")
            self._login_locked()

    # ---------- command dispatch ----------
    def call(self, cmd: dict, timeout: float = 240.0, idempotent: bool | None = None) -> dict:
        if idempotent is None:
            idempotent = cmd.get("cmd") not in WRITE_COMMANDS
        attempts = 2 if idempotent else 1
        stage = str(cmd.get("cmd") or "")
        with self._lock:
            last_error = None
            for attempt in range(attempts):
                self._ensure_ready()
                try:
                    self._send_raw(cmd)
                    line = self._readline(timeout)
                except CaxaError as exc:
                    last_error = exc
                    if idempotent and attempt + 1 < attempts:
                        continue
                    raise CaxaError(
                        str(exc),
                        stage=stage,
                        detail=exc.detail,
                        timed_out=exc.timed_out,
                        response_lost=not idempotent,
                    ) from exc
                except OSError as exc:
                    self._drop_process()
                    if idempotent and attempt + 1 < attempts:
                        continue
                    raise CaxaError(
                        "CAXA 助手进程异常退出：%s" % exc,
                        stage=stage,
                        response_lost=not idempotent,
                    ) from exc
                if line is None:
                    self._drop_process()
                    if idempotent and attempt + 1 < attempts:
                        continue
                    raise CaxaError(
                        "CAXA 助手进程无响应（可能已退出）",
                        stage=stage,
                        response_lost=not idempotent,
                    )
                try:
                    resp = json.loads(line)
                except ValueError as exc:
                    self._drop_process()
                    raise CaxaError(
                        "CAXA 助手返回内容无法解析",
                        stage=stage,
                        response_lost=not idempotent,
                    ) from exc
                if not resp.get("ok"):
                    raise self._helper_error(resp, cmd)
                return resp
            raise last_error or CaxaError("CAXA 调用失败", stage=stage)

    @staticmethod
    def _helper_error(resp: dict, cmd: dict) -> CaxaError:
        return CaxaError(
            resp.get("error") or "未知错误",
            stage=resp.get("stage") or str(cmd.get("cmd") or ""),
            detail=resp.get("detail"),
            created_process_instance_id=resp.get("created_process_instance_id"),
            partial=bool(resp.get("partial")),
            result=resp,
        )

    def _send_raw(self, cmd: dict):
        if self._proc is None or self._proc.stdin is None:
            raise OSError("助手进程不可写")
        self._proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _readline(self, timeout: float):
        if self._proc is None or self._proc.stdout is None:
            return None
        proc = self._proc
        q: queue.Queue = queue.Queue()

        def reader():
            try:
                q.put(proc.stdout.readline())
            except Exception:
                q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            self._drop_process()
            raise CaxaError(
                "CAXA 调用超时（%ss），已终止助手进程" % int(timeout),
                timed_out=True,
            )
        return line if line not in ("", None) else None

    # ---------- convenience wrappers ----------
    def set_auto_confirm(self, enabled: bool, mode: str = "", request_id: str = "") -> dict:
        return self.call(
            {
                "cmd": "set_auto_confirm",
                "enabled": bool(enabled),
                "mode": mode or "",
                "request_id": request_id or "",
            },
            timeout=30.0,
        )

    def dump_tree(self, max_depth: int = 6) -> dict:
        return self.call({"cmd": "dump_tree", "max_depth": max_depth})

    def find(self, identity: str, kind: str = "product") -> dict:
        return self.call({"cmd": "find", "identity": identity, "kind": kind})

    def create_part(self, identity_no: str, name: str = "") -> dict:
        return self.call({"cmd": "create_part", "identity_no": identity_no, "name": name})

    def upload(self, parent_kind: str, parent_id: int, file_path: str,
               draw_no: str = "", name: str = "", drawing_type: int = 3,
               on_duplicate: str = "reuse") -> dict:
        return self.call({
            "cmd": "upload",
            "parent_kind": parent_kind,
            "parent_id": parent_id,
            "file_path": file_path,
            "draw_no": draw_no,
            "name": name,
            "drawing_type": drawing_type,
            "on_duplicate": on_duplicate,
        })

    def reference(self, kind: str, target_id: int, source_id: int) -> dict:
        return self.call({"cmd": "reference", "kind": kind, "target_id": target_id, "source_id": source_id})

    def link_part(self, parent_id: int, child_id: int, reference: bool,
                  product_link: bool = False, quantity: int = 1) -> dict:
        return self.call({
            "cmd": "link_part",
            "parent_id": parent_id,
            "child_id": child_id,
            "reference": reference,
            "product_link": product_link,
            "quantity": quantity,
        })

    def new_release(self, drawing_id: int) -> dict:
        return self.call({"cmd": "new_release", "drawing_id": drawing_id})

    def release_drawing(self, drawing_id: int) -> dict:
        return self.call({"cmd": "release_drawing", "drawing_id": drawing_id})

    def start_workflow(self, drawing_ids: list[int], server_ip: str, server_port: int,
                       user_id: int, performer_id: int = 66,
                       template_id: int = 1, template_instance_id: int = 31,
                       process_def_id: str = "2", activity_def_id: str = "4",
                       execute: bool = False) -> dict:
        return self.call({
            "cmd": "start_workflow",
            "drawing_ids": drawing_ids,
            "server_ip": server_ip,
            "server_port": server_port,
            "user_id": user_id,
            "performer_id": performer_id,
            "template_id": template_id,
            "template_instance_id": template_instance_id,
            "process_def_id": process_def_id,
            "activity_def_id": activity_def_id,
            "execute": execute,
        })
