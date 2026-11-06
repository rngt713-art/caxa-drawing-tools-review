"""Per-task staging directory: uploads always send the PDF the user selected.

The uploader used to rename in place and, on a name collision, silently switch
to the pre-existing file with the target name.  Staging removes that class of
bug: the selected bytes are copied into a directory owned by this task, under
the canonical file name, and only that copy is handed to CAXA.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid

import drawing_bundle


class StagingError(Exception):
    pass


class StagingConflict(StagingError):
    """Two different contents want the same canonical name inside one task."""


class StagedFile:
    """A verified copy of one selected PDF."""

    __slots__ = ("path", "file_name", "sha256", "size", "source_path", "item_code")

    def __init__(self, path, file_name, sha256, size, source_path, item_code=""):
        self.path = path
        self.file_name = file_name
        self.sha256 = sha256
        self.size = size
        self.source_path = source_path
        self.item_code = item_code

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size": self.size,
            "source_path": self.source_path,
            "item_code": self.item_code,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "StagedFile(%r, %r)" % (self.file_name, self.sha256[:8])


def default_root() -> str:
    override = os.environ.get("CAXA_UPLOAD_STAGING") or ""
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "caxa_upload_staging")


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _hash_file(path: str) -> tuple[str, int]:
    return drawing_bundle.compute_fingerprint(path)


class TaskStaging:
    """Exclusive staging area for one upload request."""

    def __init__(self, task_id: str | None = None, root: str | None = None, log=print):
        self.task_id = task_id or new_task_id()
        self.root = root or default_root()
        self.log = log
        os.makedirs(self.root, exist_ok=True)
        # exist_ok=False guarantees exclusivity even if two tasks pick an id.
        for attempt in range(20):
            candidate = os.path.join(self.root, self.task_id)
            try:
                os.makedirs(candidate, exist_ok=False)
                self.directory = candidate
                break
            except FileExistsError:
                if attempt == 19:
                    raise StagingError("无法创建独占暂存目录: %s" % candidate)
                self.task_id = "%s-%d" % (self.task_id, attempt + 2)
        self._staged: dict[str, StagedFile] = {}

    # -- staging ---------------------------------------------------------
    def stage(self, item: dict, file_name: str | None = None) -> StagedFile:
        """Copy the selected source into the task directory and verify it."""
        source = str(item.get("file_path") or item.get("page_pdf") or "")
        if not source:
            raise StagingError("条目 %s 缺少文件路径" % (item.get("code") or "?"))
        if not os.path.isfile(source):
            raise StagingError("文件不存在: %s" % source)
        canonical = file_name or drawing_bundle.expected_drawing_filename(
            item.get("code"), item.get("name")
        )
        safe_name = os.path.basename(canonical)
        if safe_name != canonical or not safe_name.lower().endswith(".pdf"):
            raise StagingError("上传目标文件名无效: %r" % canonical)
        key = safe_name.casefold()  # Windows file names are case-insensitive
        target = os.path.join(self.directory, safe_name)
        temp_path = "%s.part-%s" % (target, uuid.uuid4().hex[:8])

        # 1) copy + hash into a temp file; nothing approved yet
        with open(source, "rb") as handle:
            digest = hashlib.sha256()
            size = 0
            with open(temp_path, "wb") as out:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            actual_hash = digest.hexdigest()

        # 2) verify the input BEFORE committing anything
        expected_hash = str(item.get("sha256") or "").lower()
        expected_size = item.get("size")
        if expected_hash and actual_hash != expected_hash:
            _remove_quiet(temp_path)
            raise StagingError(
                "文件内容与登记指纹不一致，拒绝上传: %s" % os.path.basename(source)
            )
        if expected_size is not None and int(expected_size) != size:
            _remove_quiet(temp_path)
            raise StagingError(
                "文件大小与登记记录不一致，拒绝上传: %s" % os.path.basename(source)
            )

        # 3) collision check on the committed set only
        existing = self._staged.get(key)
        if existing is not None:
            if existing.sha256 != actual_hash:
                _remove_quiet(temp_path)
                raise StagingConflict(
                    "本次任务中已有同名但内容不同的文件：%s（请修改图号/名称后重试）"
                    % safe_name
                )
            _remove_quiet(temp_path)
            return existing

        # 4) atomic commit of the verified bytes
        os.replace(temp_path, target)

        staged = StagedFile(
            path=target,
            file_name=safe_name,
            sha256=actual_hash,
            size=size,
            source_path=os.path.abspath(source),
            item_code=drawing_bundle.normalize_code(item.get("code")),
        )
        self._staged[key] = staged
        return staged

    def verify(self, staged: StagedFile) -> None:
        """Re-check the staged copy immediately before submitting it."""
        if not os.path.isfile(staged.path):
            raise StagingError("暂存文件已丢失: %s" % staged.file_name)
        digest, size = _hash_file(staged.path)
        if digest != staged.sha256 or size != staged.size:
            raise StagingError("暂存文件内容发生变化: %s" % staged.file_name)

    def close(self, keep: bool = False) -> None:
        if keep:
            return
        shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self) -> "TaskStaging":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(keep=exc_type is not None)
