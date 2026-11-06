"""Local task/operation journal (SQLite, standard library only).

Semantics that matter for correctness:

* ``OP_RUNNING`` is treated as *pending*: a row left running by a crash means
  the remote write may have happened.  Nothing is auto-replayed.
* a task holds one run lock at a time, so two processes cannot both resume and
  write the same batch;
* confirmed relation rows carry the link type; a relation is only "already
  done" when the confirmed type matches the current effective type;
* successful uploads persist the remote object id, class id/name and iteration
  number, so a later phase (relation edit, workflow later) never re-uploads.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import threading

TASK_RUNNING = "进行中"
TASK_DONE = "已完成"
TASK_PARTIAL = "部分完成"
TASK_PENDING = "待核实"
TASK_FAILED = "失败"

OP_RUNNING = "执行中"
OP_OK = "已确认成功"
OP_FAILED = "失败"
OP_PENDING = "待核实"
OP_SKIPPED = "已跳过"

#: An orphan "running" row is as uncertain as an explicit pending row.
PENDING_STATUSES = (OP_PENDING, OP_RUNNING)

DONE_RELATION_STATUSES = ("已借用", "已归属", "已存在")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        TEXT PRIMARY KEY,
    digest         TEXT NOT NULL,
    server         TEXT NOT NULL DEFAULT '',
    port           INTEGER NOT NULL DEFAULT 0,
    login_user     TEXT NOT NULL DEFAULT '',
    bundle_path    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '进行中',
    stage          TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_digest
    ON tasks (digest, server, port, login_user);
CREATE TABLE IF NOT EXISTS operations (
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    phase        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    item_code    TEXT NOT NULL DEFAULT '',
    parent_code  TEXT NOT NULL DEFAULT '',
    child_code   TEXT NOT NULL DEFAULT '',
    link_type    TEXT NOT NULL DEFAULT '',
    remote_id    TEXT NOT NULL DEFAULT '',
    class_id     TEXT NOT NULL DEFAULT '',
    iterator_no  TEXT NOT NULL DEFAULT '',
    class_name   TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (task_id, kind, key)
);
"""

_OPERATION_COLUMNS = ("link_type", "class_id")


class JournalBusy(Exception):
    """Another process/thread currently owns this task."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def default_path() -> str:
    override = os.environ.get("CAXA_UPLOAD_JOURNAL") or ""
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "caxa_upload_journal", "journal.sqlite3")


def relation_key(parent_code: str, child_code: str) -> str:
    return "%s|%s" % (parent_code or "", child_code or "")


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a lock holder."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class TaskJournal:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.executescript(_SCHEMA)
            self._migrate_columns()
            self._conn.commit()

    def _migrate_columns(self):
        """Keep older journal files usable after the schema grew."""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(operations)").fetchall()
        }
        for column in _OPERATION_COLUMNS:
            if column not in existing:
                try:
                    self._conn.execute(
                        "ALTER TABLE operations ADD COLUMN %s TEXT NOT NULL DEFAULT ''"
                        % column
                    )
                except sqlite3.DatabaseError:
                    pass

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "TaskJournal":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- single-run lock -------------------------------------------------
    def lock_path(self, task_id: str) -> str:
        directory = os.path.join(os.path.dirname(self.path) or ".", "locks")
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, task_id + ".lock")

    @contextlib.contextmanager
    def run_lock(self, key: str):
        path = self.lock_path(key)
        handle = None
        for attempt in range(2):
            try:
                handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                # A crashed process leaves a lock behind; clear it only when the
                # recorded pid is provably gone.
                holder = 0
                try:
                    with open(path, "r", encoding="utf-8") as existing:
                        holder = int((existing.read() or "0").strip() or 0)
                except (OSError, ValueError):
                    holder = 0
                if attempt == 0 and holder and not _pid_alive(holder):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                raise JournalBusy("任务 %s 正在本机另一进程执行" % key)
        if handle is None:
            raise JournalBusy("任务 %s 正在本机另一进程执行" % key)
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            os.close(handle)
        except OSError:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            yield
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def release_lock(self, key: str) -> bool:
        try:
            os.remove(self.lock_path(key))
            return True
        except OSError:
            return False

    def release_stale_locks(self) -> int:
        """Remove lock files whose recorded process no longer exists."""
        directory = os.path.join(os.path.dirname(self.path) or ".", "locks")
        removed = 0
        try:
            names = os.listdir(directory)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".lock"):
                continue
            path = os.path.join(directory, name)
            holder = 0
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    holder = int((handle.read() or "0").strip() or 0)
            except (OSError, ValueError):
                holder = 0
            if holder and not _pid_alive(holder):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed

    # -- tasks -----------------------------------------------------------
    def create_task(self, *, digest: str, server: str = "", port: int = 0,
                    user: str = "", bundle_path: str = "",
                    note: str = "") -> dict:
        task_id = "T%s-%s" % (
            datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            os.urandom(3).hex(),
        )
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, digest, server, port, login_user,"
                " bundle_path, status, stage, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, digest, server or "", int(port or 0), user or "",
                    bundle_path or "", TASK_RUNNING, "plan", note or "", now, now,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_task(self, *, digest: str, server: str = "", port: int = 0,
                  user: str = "") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE digest=? AND server=? AND port=?"
                " AND login_user=? ORDER BY rowid DESC LIMIT 1",
                (digest, server or "", int(port or 0), user or ""),
            ).fetchone()
        return dict(row) if row else None

    def tasks_for_digest(self, *, digest: str, server: str = "", port: int = 0,
                         user: str = "") -> list[dict]:
        """All attempts for one batch content (newest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE digest=? AND server=? AND port=?"
                " AND login_user=? ORDER BY rowid DESC",
                (digest, server or "", int(port or 0), user or ""),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_task_status(self, task_id: str, status: str, stage: str = "",
                        note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, stage=?, note=?, updated_at=?"
                " WHERE task_id=?",
                (status, stage or "", note or "", _now(), task_id),
            )
            self._conn.commit()

    def set_task_stage(self, task_id: str, stage: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET stage=?, updated_at=? WHERE task_id=?",
                (stage or "", _now(), task_id),
            )
            self._conn.commit()

    # -- operations ------------------------------------------------------
    def record_operation(self, task_id: str, kind: str, key: str, *,
                         status: str, phase: str = "", item_code: str = "",
                         parent_code: str = "", child_code: str = "",
                         link_type: str = "",
                         remote_id: str | int | None = "",
                         class_id: str | int | None = "",
                         iterator_no: str = "", class_name: str = "",
                         detail: str = "", error: str = "") -> dict:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO operations (task_id, kind, key, phase, status,"
                " item_code, parent_code, child_code, link_type, remote_id,"
                " class_id, iterator_no, class_name, detail, error, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id, kind, key) DO UPDATE SET"
                " phase=excluded.phase, status=excluded.status,"
                " item_code=excluded.item_code, parent_code=excluded.parent_code,"
                " child_code=excluded.child_code, link_type=excluded.link_type,"
                " remote_id=excluded.remote_id, class_id=excluded.class_id,"
                " iterator_no=excluded.iterator_no, class_name=excluded.class_name,"
                " detail=excluded.detail, error=excluded.error,"
                " updated_at=excluded.updated_at",
                (
                    task_id, kind, key, phase or "", status, item_code or "",
                    parent_code or "", child_code or "", link_type or "",
                    "" if remote_id is None else str(remote_id),
                    "" if class_id is None else str(class_id),
                    iterator_no or "", class_name or "", detail or "", error or "",
                    now,
                ),
            )
            self._conn.commit()
        return self.get_operation(task_id, kind, key) or {}

    def get_operation(self, task_id: str, kind: str, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE task_id=? AND kind=? AND key=?",
                (task_id, kind, key),
            ).fetchone()
        return dict(row) if row else None

    def operations(self, task_id: str, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM operations WHERE task_id=?"
        args: list = [task_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def pending_operations(self, task_id: str) -> list[dict]:
        """Rows whose remote effect is unknown (including orphan running)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM operations WHERE task_id=? AND status IN (%s)"
                " ORDER BY rowid" % ",".join("?" for _ in PENDING_STATUSES),
                (task_id, *PENDING_STATUSES),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirmed_uploads(self, task_id: str) -> dict[str, dict]:
        return {
            row["item_code"]: row
            for row in self.operations(task_id, "upload")
            if row["status"] == OP_OK and row["item_code"]
        }

    def confirmed_relations(self, task_id: str) -> dict[tuple[str, str], dict]:
        """Confirmed relation executions keyed by (parent, child).

        Skipped rows are not evidence of success and are excluded.
        """
        done: dict[tuple[str, str], dict] = {}
        for row in self.operations(task_id, "relation"):
            if row["status"] == OP_OK:
                done[(row["parent_code"], row["child_code"])] = row
        return done

    def relation_decisions(self, task_id: str) -> dict[tuple[str, str], str]:
        """Own/borrow decisions snapshotted before any remote write."""
        decisions: dict[tuple[str, str], str] = {}
        for row in self.operations(task_id, "decision"):
            if row["status"] == OP_OK and row["link_type"] in ("own", "borrow"):
                decisions[(row["parent_code"], row["child_code"])] = row["link_type"]
        return decisions

    # -- workflow --------------------------------------------------------
    def record_workflow(self, task_id: str, *, status: str, process_id: str | int | None,
                        stage: str = "", drawing_ids: list[int] | None = None,
                        started: bool = False, detail: str = "",
                        error: str = "") -> dict:
        payload = {
            "drawing_ids": [int(v) for v in (drawing_ids or [])],
            "stage": stage or "",
            "started": bool(started),
        }
        if detail:
            payload["note"] = detail
        return self.record_operation(
            task_id,
            "workflow",
            "workflow",
            status=status,
            phase=stage or "workflow",
            remote_id=process_id,
            class_name="workflow",
            detail=json.dumps(payload, ensure_ascii=False),
            error=error or "",
        )

    def workflow(self, task_id: str) -> dict | None:
        return self.get_operation(task_id, "workflow", "workflow")

    # -- explicit human recovery ----------------------------------------
    def resolve_operation(self, task_id: str, kind: str, key: str, *,
                          result: str, remote_id: str | int | None = None,
                          class_id: str | int | None = None,
                          iterator_no: str = "",
                          link_type: str = "",
                          note: str = "") -> dict:
        """Apply an audited human decision to exactly one pending operation.

        ``result`` is ``success`` (remote write verified) or ``no_write``
        (verified that nothing was written, safe to retry).
        """
        current = self.get_operation(task_id, kind, key)
        if current is None:
            raise ValueError("找不到操作 %s/%s" % (kind, key))
        if current["status"] not in (OP_PENDING, OP_RUNNING):
            raise ValueError("操作 %s/%s 不是待核实状态" % (kind, key))
        if result == "success":
            status = OP_OK
        elif result == "no_write":
            status = OP_FAILED
        else:
            raise ValueError("未知的核实结果: %s" % result)
        record = dict(current)
        record.update(
            {
                "status": status,
                "remote_id": "" if remote_id is None else str(remote_id),
                "class_id": "" if class_id is None else str(class_id),
                "iterator_no": iterator_no or record.get("iterator_no") or "",
                "link_type": link_type or record.get("link_type") or "",
                "detail": note or record.get("detail") or "",
            }
        )
        self.record_operation(
            task_id,
            kind,
            key,
            status=status,
            phase=record.get("phase") or "",
            item_code=record.get("item_code") or "",
            parent_code=record.get("parent_code") or "",
            child_code=record.get("child_code") or "",
            link_type=record.get("link_type") or "",
            remote_id=record.get("remote_id") or "",
            class_id=record.get("class_id") or "",
            iterator_no=record.get("iterator_no") or "",
            class_name=record.get("class_name") or "",
            detail=record.get("detail") or "",
            error="" if status == OP_OK else (note or "人工确认未写入"),
        )
        remaining = self.pending_operations(task_id)
        if not remaining:
            self.set_task_status(task_id, TASK_PARTIAL, stage="recovered")
        return self.get_operation(task_id, kind, key) or {}

    def task_summary(self, task_id: str) -> dict:
        task = self.get_task(task_id) or {}
        return {
            "task": task,
            "uploads": self.operations(task_id, "upload"),
            "relations": self.operations(task_id, "relation"),
            "decisions": self.operations(task_id, "decision"),
            "workflow": self.workflow(task_id),
            "pending": self.pending_operations(task_id),
        }
