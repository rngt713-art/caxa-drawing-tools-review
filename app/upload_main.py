"""Distributable CAXA uploader for reviewed drawing bundles.

This entry point deliberately has no dependency on the Qwen vision pipeline.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinterdnd2 import DND_FILES, TkinterDnD


APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import config  # noqa: E402
import drawing_bundle  # noqa: E402
import journal as journal_module  # noqa: E402
import upload_ui_logic as ui_logic  # noqa: E402
from upload_core import (  # noqa: E402
    apply_manual_borrows,
    precheck_with_caxa,
    preview_bundle,
    upload_bundle,
)


KIND_LABEL = {
    "process_doc": "工艺文档",
    "machine_drawing": "整机图纸",
    "component": "组件图纸",
    "unknown": "未知",
}

LINK_TYPE_LABEL = {
    "own": "归属子件 (树中无)",
    "borrow": "借用 (树中已有)",
}

SOURCE_LABEL = {
    "drag": "本批拖拽",
    "manual": "手工指定",
    "z03_bom": "Z03明细表",
}
UPLOAD_COLUMN = "#7"


class UploadApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("图纸上传助手")
        self.geometry("1240x780")
        self.cfg = config.load()
        self.manifest_path = None
        self.bundle = None
        self.items = []
        self.manual_borrows = []
        self.relations = []
        self.events = queue.Queue()
        self.busy = False
        self._last_identity = None
        self._active_request = 0
        self._last_result = None
        self._drag_source_row = ""
        self._drag_started = False
        self._drag_origin = None
        self._drag_target_row = ""
        self._drag_ghost = None
        self._tree_items = {}
        self._tree_relations = {}
        self._tree_codes = {}
        self._build_ui()
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)
        self._poll_after_id = self.after(150, self._poll_events)

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.top_frame = top

        ttk.Button(top, text="打开上传包", command=self.open_bundle).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )
        ttk.Button(top, text="打开上传包目录", command=self.open_bundle_dir).grid(
            row=0, column=1, padx=(0, 8), sticky="w"
        )
        self.bundle_var = tk.StringVar(value="未打开上传包")
        ttk.Label(top, textvariable=self.bundle_var).grid(
            row=0, column=2, columnspan=6, sticky="w"
        )

        ttk.Label(top, text="CAXA IP").grid(row=1, column=0, sticky="e", pady=3)
        self.ip_var = tk.StringVar(value=str(self.cfg.get("caxa_server_ip") or ""))
        ttk.Entry(top, textvariable=self.ip_var, width=18).grid(
            row=1, column=1, sticky="w", pady=3
        )
        ttk.Label(top, text="端口").grid(row=1, column=2, sticky="e", padx=(10, 2))
        self.port_var = tk.StringVar(value=str(self.cfg.get("caxa_server_port") or "8900"))
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(
            row=1, column=3, sticky="w", pady=3
        )
        ttk.Label(top, text="用户名").grid(row=1, column=4, sticky="e", padx=(10, 2))
        self.user_var = tk.StringVar(value=str(self.cfg.get("caxa_user") or ""))
        ttk.Entry(top, textvariable=self.user_var, width=18).grid(
            row=1, column=5, sticky="w", pady=3
        )
        ttk.Label(top, text="密码").grid(row=1, column=6, sticky="e", padx=(10, 2))
        self.password_var = tk.StringVar(value=config.get_password(self.cfg))
        ttk.Entry(top, textvariable=self.password_var, show="*", width=18).grid(
            row=1, column=7, sticky="w", pady=3
        )

        ttk.Label(top, text="重复图号").grid(
            row=2, column=0, sticky="e", pady=3
        )
        self.duplicate_var = tk.StringVar(
            value=ui_logic.duplicate_policy_label(
                self.cfg.get("duplicate_policy")
            )
        )
        self.duplicate_combo = ttk.Combobox(
            top,
            textvariable=self.duplicate_var,
            state="readonly",
            width=18,
            values=tuple(ui_logic.DUPLICATE_POLICY_LABELS.values()),
        )
        self.duplicate_combo.grid(
            row=2, column=1, columnspan=2, sticky="w", pady=3
        )

        self.workflow_var = tk.BooleanVar(
            value=bool(self.cfg.get("start_workflow_after_upload", False))
        )
        ttk.Checkbutton(
            top,
            text="上传成功后发起“审核打印”流程",
            variable=self.workflow_var,
        ).grid(row=2, column=3, columnspan=3, sticky="w")
        ttk.Label(top, text="审核人").grid(row=2, column=6, sticky="e")
        self.performer_var = tk.StringVar(
            value=str(self.cfg.get("workflow_performer") or "审核人")
        )
        ttk.Entry(top, textvariable=self.performer_var, width=18).grid(
            row=2, column=7, sticky="w"
        )

        buttons = ttk.Frame(top)
        buttons.grid(row=3, column=0, columnspan=8, sticky="w", pady=(8, 0))
        self.action_buttons = []
        self.action_buttons.append(
            ttk.Button(buttons, text="添加/拖入PDF", command=self.add_pdfs)
        )
        self.action_buttons[-1].pack(side="left", padx=(0, 6))
        self.upload_button = ttk.Button(
            buttons, text="上传图纸", command=self.start_upload
        )
        self.upload_button.pack(side="left")
        for text, command in (
            ("一键规范文件名", self.normalize_filenames),
            ("检查上传包/设计树预检", self.check_bundle),
            ("执行手动借用", self.start_manual_borrow),
            ("核实待定结果", self.recover_pending),
            ("清空日志", self.clear_log),
        ):
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(side="left", padx=6)
            self.action_buttons.append(button)
        ttk.Label(
            top,
            text="数字图号需“图号 名称.pdf”，单字母工艺（Z03/M03/T03/J03/P03）为“图号.pdf”（不带中文名）",
            wraplength=980,
        ).grid(row=4, column=0, columnspan=8, sticky="w", pady=(4, 0))

        work_area = ttk.Frame(self)
        work_area.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        relation_actions = ttk.Frame(work_area)
        relation_actions.pack(fill="x", pady=(0, 5))
        for text, command in (
            ("增加次级条目", lambda: self.add_relation_dialog()),
            ("切换 归属子件 / 借用", self.toggle_selected_relation_type),
            ("删除选中关系", self.delete_selected_relation),
        ):
            button = ttk.Button(relation_actions, text=text, command=command)
            button.pack(side="left", padx=(0, 6))
            self.action_buttons.append(button)
        ttk.Label(
            relation_actions,
            text="拖拽图纸到上级条目可建立关系；经过时上级行高亮，松开后清除。",
        ).pack(side="left", padx=8)

        columns = (
            "source",
            "page",
            "kind",
            "machine",
            "relation",
            "filename_state",
            "upload_sel",
            "status",
            "note",
        )
        tree_frame = ttk.Frame(work_area)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", height=18
        )
        headers = {
            "#0": "图纸（图号 名称）",
            "source": "来源文件",
            "page": "页",
            "kind": "类型",
            "machine": "成品代码",
            "relation": "关系",
            "filename_state": "命名",
            "upload_sel": "上传",
            "status": "状态",
            "note": "结果/备注",
        }
        widths = {
            "#0": 360,
            "source": 150,
            "page": 40,
            "kind": 80,
            "machine": 130,
            "relation": 150,
            "filename_state": 60,
            "upload_sel": 45,
            "status": 120,
            "note": 220,
        }
        for column in columns:
            self.tree.heading(column, text=headers[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.heading("#0", text=headers["#0"])
        self.tree.column("#0", width=widths["#0"], anchor="w", minwidth=240)
        self.tree.tag_configure("invalid", foreground="#b00020")
        self.tree.tag_configure("drop_target", background="#cfe8ff")
        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._toggle_upload)
        self.tree.bind("<ButtonPress-1>", self._begin_row_drag, add="+")
        self.tree.bind("<B1-Motion>", self._begin_row_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._finish_row_drag, add="+")
        self.tree.bind("<Button-3>", self._show_row_menu)
        self.tree.bind("<Double-1>", self._edit_item)
        self.dnd_bind("<<DropEnter>>", self._on_drop_hover, add="+")
        self.dnd_bind("<<DropPosition>>", self._on_drop_hover, add="+")
        self.dnd_bind("<<DropLeave>>", lambda _event: self._set_drag_target(""), add="+")

        self.log_text = tk.Text(self, height=9, state="disabled")
        self.log_text.pack(fill="x", padx=8, pady=(0, 4))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(
            self, textvariable=self.status_var, relief="sunken", anchor="w"
        ).pack(fill="x", side="bottom")

    def log(self, message: str):
        self.events.put(("log", message))

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "refresh":
                    self.refresh_tree()
                elif kind == "precheck_done":
                    snapshot, info = payload
                    ui_logic.apply_relation_result(
                        self.relations, snapshot
                    )
                    self.set_busy(False)
                    self.refresh_tree()
                    self.status_var.set(
                        "预检完成：共 %d 条关系（归属 %d，借用 %d）"
                        % (
                            info["checked_relations"],
                            info["own_count"],
                            info["borrow_count"],
                        )
                    )
                    messagebox.showinfo(
                        "CAXA 设计树预检完成",
                        "共 %d 条关系：归属子件 %d 条，借用 %d 条。\n"
                        "可在层级树中双击关系行进行人工勘误切换。"
                        % (
                            info["checked_relations"],
                            info["own_count"],
                            info["borrow_count"],
                        ),
                    )
                elif kind == "upload_done":
                    snapshot, result, task_settings = payload
                    identity_now = None
                    try:
                        identity_now = ui_logic.identity_key(self._build_settings())
                    except Exception:
                        identity_now = None
                    same_identity = (
                        identity_now is None
                        or identity_now == ui_logic.identity_key(task_settings)
                    )
                    if same_identity:
                        ui_logic.apply_upload_result(self.items, snapshot)
                        ui_logic.apply_relation_result(self.relations, snapshot)
                    else:
                        ui_logic.invalidate_successes(
                            self.items, "上传期间账号/服务器已变更，结果未合并"
                        )
                    self._last_result = result
                    self.set_busy(False)
                    self.refresh_tree()
                    summary = ui_logic.summary_message(result)
                    if result.get("ok"):
                        self.status_var.set("完成")
                        messagebox.showinfo("上传结果", summary)
                    else:
                        self.status_var.set("上传未全部成功")
                        messagebox.showwarning("上传结果（需人工确认）", summary)
                    if result.get("duplicate"):
                        if messagebox.askyesno(
                            "相同批次已完成",
                            summary + "\n\n是否作为“新任务”重新提交一次？\n"
                            "（内容或账号变化会自动视为新任务；重传会新建版次）",
                        ):
                            self.start_upload(force_new_task=True)
                elif kind == "manual_done":
                    snapshot, result = payload
                    ui_logic.apply_relation_result(self.relations, snapshot)
                    self.set_busy(False)
                    self.refresh_tree()
                    self.status_var.set(
                        "手动关系：完成 %d，已存在 %d，缺失 %d，失败 %d"
                        % (
                            result["borrowed"],
                            result["skipped"],
                            result["missing"],
                            result["failed"],
                        )
                    )
                    messagebox.showinfo(
                        "手动借用结果",
                        "已建立/确认 %d 条，已存在 %d 条，缺对象 %d 条，失败 %d 条"
                        % (
                            result["borrowed"],
                            result["skipped"],
                            result["missing"],
                            result["failed"],
                        ),
                    )
                elif kind == "done":
                    self.set_busy(False)
                    self.status_var.set("完成")
                    messagebox.showinfo("完成", str(payload))
                elif kind == "error":
                    self.set_busy(False)
                    self.status_var.set("失败")
                    messagebox.showerror("错误", str(payload))
                elif kind == "task_error":
                    self.set_busy(False)
                    self.refresh_tree()
                    self.status_var.set("失败")
                    messagebox.showerror("任务失败", str(payload))
        except queue.Empty:
            pass
        self._poll_after_id = self.after(150, self._poll_events)

    def destroy(self):
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        super().destroy()

    def set_busy(self, busy: bool):
        """Freeze every mutating entry point for the duration of one task."""
        self.busy = bool(busy)
        state = "disabled" if busy else "normal"
        for button in getattr(self, "action_buttons", []):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass
        # Account/server/widget settings are frozen too: a task must not be
        # shown as successful under an account the user switched to mid-run.
        for widget, normal_state in self._config_widget_states().items():
            try:
                widget.configure(state=normal_state if not busy else "disabled")
            except tk.TclError:
                pass
        try:
            self.tree.configure(selectmode="none" if busy else "browse")
        except tk.TclError:
            pass
    def _blocked_by_busy(self) -> bool:
        if self.busy:
            self.status_var.set("当前任务进行中，暂时不能修改清单或再次提交")
            return True
        return False

    def _config_widget_states(self) -> dict:
        """Editable account/widgets in the top form, with their normal state."""
        if getattr(self, "_config_states_cache", None) is not None:
            return self._config_states_cache
        states = {}

        def walk(widget):
            for child in widget.winfo_children():
                if child in getattr(self, "action_buttons", []):
                    continue
                try:
                    cls = child.winfo_class()
                except tk.TclError:
                    continue
                if cls in ("TEntry", "TCombobox", "TCheckbutton", "Entry", "Checkbutton"):
                    try:
                        states[child] = str(child.cget("state")) or "normal"
                    except tk.TclError:
                        states[child] = "normal"
                walk(child)

        walk(self.top_frame)
        self._config_states_cache = states
        return states

    def _on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        target_code = ""
        target_label = ""
        try:
            y = int(event.y_root) - self.tree.winfo_rooty()
            row = self.tree.identify_row(y)
            if row:
                target_item = self._tree_items.get(row)
                candidate = self._tree_codes.get(row, "")
                if drawing_bundle.is_valid_code(candidate):
                    target_code = candidate
                    target_label = " ".join(
                        value
                        for value in (
                            candidate,
                            (target_item or {}).get("name", ""),
                        )
                        if value
                    )
        except Exception:
            target_code = ""
        self._set_drag_target("")
        self.add_pdfs(paths, parent_code=target_code)
        if target_code:
            self.log(
                "拖入的图纸作为 %s 的次级条目" % target_label
            )

    def _on_drop_hover(self, event):
        try:
            y = int(event.y_root) - self.tree.winfo_rooty()
            row = self.tree.identify_row(y)
            if row and drawing_bundle.is_valid_code(self._tree_codes.get(row, "")):
                self._set_drag_target(row)
            else:
                self._set_drag_target("")
        except Exception:
            self._set_drag_target("")

    def add_pdfs(self, paths=None, parent_code=""):
        if self._blocked_by_busy():
            return
        if paths is None:
            paths = filedialog.askopenfilenames(
                title="选择已拆分并重命名的 PDF",
                filetypes=[("PDF", "*.pdf")],
            )
        if not paths:
            return
        existing = {
            os.path.normcase(os.path.abspath(item.get("file_path") or ""))
            for item in self.items
        }
        added = 0
        for path in paths:
            path = os.path.abspath(str(path))
            if not path.lower().endswith(".pdf") or not os.path.isfile(path):
                continue
            if os.path.normcase(path) in existing:
                continue
            code, name, errors = drawing_bundle.parse_drawing_filename(path)
            kind, machine = drawing_bundle.infer_kind(code)
            parent_code = drawing_bundle.normalize_code(parent_code)
            borrow = bool(parent_code and kind == "component")
            if borrow:
                machine = parent_code if parent_code.startswith("03.") else ""
            elif parent_code and kind == "process_doc" and not machine:
                machine = parent_code if parent_code.startswith("03.") else ""
            item = {
                "source": os.path.basename(path),
                "page": 1,
                "file_path": path,
                "code": code,
                "name": "" if drawing_bundle.is_single_letter_process_code(code) else name,
                "machine": machine,
                "kind": kind,
                "borrow": borrow,
                "borrow_to": parent_code if borrow else "",
                "parent_code": parent_code if borrow else "",
                "upload_sel": not errors,
                "status": (
                    "文件名不符合"
                    if errors
                    else "待上传（次级条目）"
                    if borrow
                    else "待上传"
                ),
                "target": "",
                "note": "；".join(errors),
            }
            self.items.append(item)
            existing.add(os.path.normcase(path))
            added += 1
            if borrow:
                self._add_relation(parent_code, code, source="drag", refresh=False)
        if added:
            if self.bundle is None:
                self.bundle = {
                    "schema": "caxa-drawing-upload-bundle",
                    "schema_version": 1,
                    "items": self.items,
                    "assemblies": [],
                    "manual_borrows": self.manual_borrows,
                    "relations": self.relations,
                    "warnings": [],
                }
            else:
                self.bundle["items"] = self.items
                self.bundle["relations"] = self.relations
            self.bundle_var.set("手工拖入的 PDF：%d 项" % len(self.items))
            self.refresh_tree()
            self.status_var.set(
                "已加入 %d 项；标红行可双击修正或点“一键规范文件名”"
                % added
            )
            self.log("已加入 %d 个 PDF" % added)

    def normalize_filenames(self):
        """Fill code/name/machine from the file name; never rename the source.

        Uploads always use the canonical target name through staging, so the
        user's files stay untouched.
        """
        if self._blocked_by_busy():
            return
        if not self.items:
            messagebox.showinfo("提示", "当前列表中没有图纸条目")
            return
        updated_count = 0
        skipped_errors = []
        for item in self.items:
            file_path = item.get("file_path") or ""
            if not file_path or not os.path.isfile(file_path):
                continue
            code, name, errors = drawing_bundle.parse_drawing_filename(file_path)
            if errors:
                skipped_errors.append(
                    "%s：%s" % (os.path.basename(file_path), "；".join(errors))
                )
                continue
            kind, inferred_machine = drawing_bundle.infer_kind(code)
            if kind == "unknown":
                skipped_errors.append(
                    "%s：无法从文件名确定类型，请双击修正图号" % os.path.basename(file_path)
                )
                continue
            item["code"] = code
            item["name"] = "" if drawing_bundle.is_single_letter_process_code(code) else name
            item["kind"] = kind
            if inferred_machine and not item.get("machine"):
                item["machine"] = inferred_machine
            drawing_bundle.register_fingerprint(item, force=True)
            item["filename_error"] = ""
            item["note"] = ""
            item["upload_sel"] = True
            if item.get("status") == "文件名不符合":
                item["status"] = "待上传（次级条目）" if item.get("borrow") else "待上传"
            updated_count += 1

        self.refresh_tree()
        msg = "已按文件名补全 %d 项；上传时按规范目标名提交，源文件不改名" % updated_count
        if skipped_errors:
            msg += "；另有 %d 项需人工双击修正：\n%s" % (
                len(skipped_errors),
                "\n".join(skipped_errors[:8]),
            )
            messagebox.showwarning("一键规范文件名结果", msg)
        else:
            self.status_var.set(msg)
            self.log(msg)

    def open_bundle(self):
        path = filedialog.askopenfilename(
            title="选择 manifest.json 或上传包目录内的 manifest.json",
            filetypes=[("上传包清单", "manifest.json"), ("JSON", "*.json")],
        )
        if not path:
            return
        self._load_bundle_from_path(path)

    def open_bundle_dir(self):
        path = filedialog.askdirectory(title="选择图纸上传包目录")
        if not path:
            return
        self._load_bundle_from_path(path)

    def _load_bundle_from_path(self, path: str):
        if self._blocked_by_busy():
            return
        try:
            manifest_path, bundle = drawing_bundle.load_bundle(path)
            self.manifest_path = manifest_path
            self.bundle = bundle
            self.items = list(bundle.get("items") or [])
            self.manual_borrows = list(bundle.get("manual_borrows") or [])
            self.relations = drawing_bundle.normalize_manifest_relations(bundle)
            self.bundle_var.set(str(manifest_path.parent))
            self.refresh_tree()
            self.status_var.set(
                "已加载 %d 项，可点击“上传”列切换选择"
                % len(self.items)
            )
            self.log("已打开上传包：%s" % manifest_path.parent)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def refresh_tree(self):
        selected_rows = self.tree.selection()
        selected_code = (
            self._tree_codes.get(selected_rows[0]) if selected_rows else ""
        )
        collapsed = {
            code
            for iid, code in self._tree_codes.items()
            if self.tree.parent(iid) and not self.tree.item(iid, "open")
        }
        self.tree.delete(*self.tree.get_children())
        self._tree_items = {}
        self._tree_relations = {}
        self._tree_codes = {}

        batch: dict[str, dict] = {}
        for index, item in enumerate(self.items):
            problems = drawing_bundle.item_problems(item, check_content=False)
            warnings = drawing_bundle.filename_warnings(item)
            item["filename_valid"] = not problems
            item["filename_warnings"] = warnings
            if problems:
                item["filename_error"] = "；".join(problems)
                item["upload_sel"] = False
                if item.get("status") in ("", "待上传", "待上传（次级条目）"):
                    item["status"] = "文件名不符合"
            else:
                item["filename_error"] = "；".join(warnings)
                if item.get("status") == "文件名不符合":
                    item["status"] = "待上传（次级条目）" if item.get("borrow") else "待上传"
                    item["upload_sel"] = True
            item["_tree_problem"], item["_tree_warning"] = problems, warnings
            code = drawing_bundle.normalize_code(item.get("code"))
            if code:
                batch.setdefault(code, item)

        grouped: dict[str, list[dict]] = {}
        for relation in self.relations:
            parent = drawing_bundle.normalize_code(relation.get("parent_code"))
            child = drawing_bundle.normalize_code(relation.get("child_code"))
            if parent and child:
                grouped.setdefault(parent, []).append(relation)

        child_codes = {
            drawing_bundle.normalize_code(rel.get("child_code"))
            for rel in self.relations
            if drawing_bundle.normalize_code(rel.get("child_code"))
        }
        root_codes = [
            drawing_bundle.normalize_code(item.get("code"))
            for item in self.items
            if drawing_bundle.normalize_code(item.get("code")) not in child_codes
        ]
        root_codes.extend(
            parent for parent in sorted(grouped) if parent not in batch and parent not in child_codes
        )

        row_number = 0
        rendered_codes = set()

        def insert_node(
            code: str,
            parent_iid: str = "",
            relation: dict | None = None,
            ancestors: tuple[str, ...] = (),
        ):
            nonlocal row_number
            if not code or code in ancestors:
                return
            item = batch.get(code)
            row_number += 1
            iid = "n%d" % row_number
            name = str(item.get("name") or "") if item else ""
            label = " ".join(value for value in (code, name) if value)
            if item:
                problems = item.pop("_tree_problem", [])
                warnings = item.pop("_tree_warning", [])
                relation_label = ""
                if relation is not None:
                    link_type = str(relation.get("link_type") or "borrow")
                    relation_label = LINK_TYPE_LABEL.get(link_type, link_type)
                    if relation.get("manual_override"):
                        relation_label += " [人工]"
                values = (
                    item.get("source", ""),
                    item.get("page", ""),
                    KIND_LABEL.get(item.get("kind"), item.get("kind", "")),
                    item.get("machine", ""),
                    relation_label or ("父级" if grouped.get(code) else ""),
                    "异常" if problems else ("需规范" if warnings else "OK"),
                    "是" if item.get("upload_sel", True) else "否",
                    item.get("status", ""),
                    item.get("note", "") or item.get("filename_error", ""),
                )
                tags = ("invalid",) if problems else ()
            else:
                link_type = str((relation or {}).get("link_type") or "borrow")
                relation_label = LINK_TYPE_LABEL.get(link_type, link_type)
                if (relation or {}).get("manual_override"):
                    relation_label += " [人工]"
                values = (
                    "设计树节点",
                    "",
                    "外部节点",
                    code if code.startswith("03.") else "",
                    relation_label,
                    "",
                    "—",
                    (relation or {}).get("link_status", "") or "待处理",
                    (relation or {}).get("note", ""),
                )
                tags = ("external",)
            self.tree.insert(
                parent_iid,
                "end",
                iid=iid,
                text=label,
                values=values,
                tags=tags,
                open=code not in collapsed,
            )
            self._tree_items[iid] = item
            self._tree_relations[iid] = relation
            self._tree_codes[iid] = code
            rendered_codes.add(code)
            for child_relation in grouped.get(code, []):
                child = drawing_bundle.normalize_code(
                    child_relation.get("child_code")
                )
                insert_node(
                    child,
                    iid,
                    child_relation,
                    ancestors + (code,),
                )

        for code in root_codes:
            insert_node(code)
        for item in self.items:
            code = drawing_bundle.normalize_code(item.get("code"))
            if code and code not in rendered_codes:
                insert_node(code)

        if selected_code:
            for iid, code in self._tree_codes.items():
                if code == selected_code:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break

    def _batch_codes(self) -> set[str]:
        return {
            drawing_bundle.normalize_code(it.get("code"))
            for it in self.items
            if drawing_bundle.is_valid_code(it.get("code"))
        }

    def _add_relation(
        self,
        parent_code: str,
        child_code: str,
        *,
        link_type: str | None = None,
        source: str = "manual",
        manual_override: bool = False,
        refresh: bool = True,
    ) -> bool:
        parent = drawing_bundle.normalize_code(parent_code)
        child = drawing_bundle.normalize_code(child_code)
        if (
            not drawing_bundle.is_valid_code(parent)
            or not drawing_bundle.is_valid_code(child)
            or parent == child
        ):
            return False
        resolved_type = (
            link_type
            if link_type in ("own", "borrow")
            else drawing_bundle.default_link_type(parent, child, self._batch_codes())
        )
        for existing in self.relations:
            if (
                drawing_bundle.normalize_code(existing.get("parent_code")) == parent
                and drawing_bundle.normalize_code(existing.get("child_code")) == child
            ):
                if manual_override or link_type in ("own", "borrow"):
                    existing["link_type"] = resolved_type
                    existing["manual_override"] = bool(manual_override)
                    if refresh:
                        self.refresh_relations()
                return False

        relation = {
            "parent_code": parent,
            "child_code": child,
            "link_type": resolved_type,
            "source": source,
            "manual_override": bool(manual_override),
            "link_status": "",
        }
        self.relations.append(relation)
        if self.bundle is None:
            self.bundle = {
                "schema": "caxa-drawing-upload-bundle",
                "schema_version": 1,
                "items": self.items,
                "assemblies": [],
                "manual_borrows": self.manual_borrows,
                "warnings": [],
            }
        self.bundle["relations"] = self.relations
        if refresh:
            self.refresh_relations()
        return True

    def _code_name_map(self):
        result = {}
        for item in self.items:
            code = drawing_bundle.normalize_code(item.get("code"))
            if code:
                result[code] = str(item.get("name") or "")
        return result

    def refresh_relations(self):
        self.refresh_tree()

    def toggle_selected_relation_type(self):
        if self._blocked_by_busy():
            return
        selection = self.tree.selection()
        if not selection:
            return
        rel = self._tree_relations.get(selection[0])
        if not rel:
            return
        current = str(rel.get("link_type") or "borrow")
        new_type = "borrow" if current == "own" else "own"
        rel["link_type"] = new_type
        rel["manual_override"] = True
        self.refresh_tree()
        self.log(
            "关系类型已手动切换：%s ← %s 设为【%s】"
            % (
                rel.get("parent_code"),
                rel.get("child_code"),
                LINK_TYPE_LABEL.get(new_type, new_type),
            )
        )

    def _begin_row_drag(self, event):
        row = self.tree.identify_row(event.y)
        if self.busy or not row:
            self._drag_source_row = ""
            return
        self._drag_source_row = row
        self._drag_started = False
        self._drag_origin = (event.x_root, event.y_root)

    def _begin_row_drag_motion(self, event):
        if not self._drag_source_row or self.busy:
            return
        if not self._drag_started and self._drag_origin:
            dx = abs(int(event.x_root) - int(self._drag_origin[0]))
            dy = abs(int(event.y_root) - int(self._drag_origin[1]))
            if max(dx, dy) < 6:
                return
            self._drag_started = True
            self._show_drag_ghost(event)
            try:
                self.tree.grab_set()
            except tk.TclError:
                pass
        self._move_drag_ghost(event)
        row = self.tree.identify_row(event.y)
        target_code = self._tree_codes.get(row, "")
        source_code = self._tree_codes.get(self._drag_source_row, "")
        if row and target_code and target_code != source_code:
            self._set_drag_target(row)
        else:
            self._set_drag_target("")

    def _finish_row_drag(self, event):
        source = self._drag_source_row
        started = self._drag_started
        target = self._drag_target_row or self.tree.identify_row(event.y)
        self._clear_drag_visuals()
        self._drag_source_row = ""
        self._drag_started = False
        self._drag_origin = None
        if self.busy or not started or not source or not target or source == target:
            return
        parent_code = self._tree_codes.get(target, "")
        child_code = self._tree_codes.get(source, "")
        child_item = self._tree_items.get(source)
        if child_item is not None and parent_code and child_code:
            changed = self._add_relation(
                parent_code, child_code, source="drag", refresh=False
            )
            child_item["borrow"] = True
            child_item["borrow_to"] = parent_code
            child_item["parent_code"] = parent_code
            self.refresh_tree()
            self.status_var.set(
                "%s：%s ← %s"
                % (
                    "已建立次级条目" if changed else "关系已存在",
                    parent_code,
                    child_code,
                )
            )
            self.log(self.status_var.get())

    def _show_drag_ghost(self, event):
        if self._drag_ghost is not None:
            return
        item = self._tree_items.get(self._drag_source_row) or {}
        code = drawing_bundle.normalize_code(item.get("code"))
        name = str(item.get("name") or "")
        text = "PDF  " + " ".join(value for value in (code, name) if value)
        self._drag_ghost = tk.Label(
            self.tree,
            text=text,
            bg="#eef7ff",
            fg="#17324d",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        )
        self._drag_ghost.lift()
        self._move_drag_ghost(event)

    def _move_drag_ghost(self, event):
        if self._drag_ghost is None:
            return
        x = int(event.x_root) - self.tree.winfo_rootx() + 14
        y = int(event.y_root) - self.tree.winfo_rooty() + 12
        x = max(0, min(x, max(0, self.tree.winfo_width() - 260)))
        y = max(0, min(y, max(0, self.tree.winfo_height() - 32)))
        self._drag_ghost.place(x=x, y=y, anchor="nw")
        self._drag_ghost.lift()

    def _set_drag_target(self, row: str):
        if row == self._drag_target_row:
            return
        if self._drag_target_row:
            try:
                tags = tuple(
                    tag
                    for tag in self.tree.item(self._drag_target_row, "tags")
                    if tag != "drop_target"
                )
                self.tree.item(self._drag_target_row, tags=tags)
            except tk.TclError:
                pass
        self._drag_target_row = row
        if row:
            try:
                tags = tuple(self.tree.item(row, "tags"))
                self.tree.item(row, tags=tags + ("drop_target",))
            except tk.TclError:
                self._drag_target_row = ""

    def _clear_drag_visuals(self):
        self._set_drag_target("")
        if self._drag_ghost is not None:
            try:
                self._drag_ghost.destroy()
            except tk.TclError:
                pass
            self._drag_ghost = None
        try:
            self.tree.grab_release()
        except tk.TclError:
            pass

    def _extract_code_text(self, text):
        value = str(text or "").strip()
        if not value:
            return ""
        return drawing_bundle.normalize_code(value.split()[0])

    def add_relation_dialog(self, parent_code=""):
        if self._blocked_by_busy():
            return
        selection = self.tree.selection()
        if not parent_code and selection:
            parent_code = self._tree_codes.get(selection[0], "")
        dialog = tk.Toplevel(self)
        dialog.title("增加次级条目")
        dialog.transient(self)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill="both", expand=True)
        ttk.Label(form, text="父级 PDF 图号").grid(
            row=0, column=0, sticky="e", pady=4
        )
        parent_var = tk.StringVar(value=parent_code)
        ttk.Entry(form, textvariable=parent_var, width=52).grid(
            row=0, column=1, sticky="w", pady=4
        )
        ttk.Label(form, text="次级条目").grid(
            row=1, column=0, sticky="e", pady=4
        )
        choices = [
            " ".join(
                value
                for value in (
                    drawing_bundle.normalize_code(item.get("code")),
                    str(item.get("name") or ""),
                )
                if value
            )
            for item in self.items
            if item.get("code")
        ]
        child_var = tk.StringVar()
        combo = ttk.Combobox(
            form,
            textvariable=child_var,
            values=choices,
            width=52,
        )
        combo.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(form, text="关系类型").grid(
            row=2, column=0, sticky="e", pady=4
        )
        link_type_var = tk.StringVar(value="auto")
        type_frame = ttk.Frame(form)
        type_frame.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Radiobutton(
            type_frame, text="自动（树中无=归属，树中有=借用）", variable=link_type_var, value="auto"
        ).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(
            type_frame, text="归属子件", variable=link_type_var, value="own"
        ).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(
            type_frame, text="借用", variable=link_type_var, value="borrow"
        ).pack(side="left")

        ttk.Label(
            form,
            text="可从本批图纸下拉选择，也可直接输入设计树中已有的图号。",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 8))

        def save():
            parent = drawing_bundle.normalize_code(parent_var.get())
            child = self._extract_code_text(child_var.get())
            if not drawing_bundle.is_valid_code(parent):
                messagebox.showwarning("父级图号无效", "请填写有效的父级 PDF 图号。")
                return
            if not drawing_bundle.is_valid_code(child):
                messagebox.showwarning(
                    "次级图号无效", "请选择本批图纸或输入有效的设计树图号。"
                )
                return
            chosen = link_type_var.get()
            manual_override = chosen in ("own", "borrow")
            ltype = chosen if manual_override else None
            if not self._add_relation(
                parent,
                child,
                link_type=ltype,
                source="manual",
                manual_override=manual_override,
            ):
                messagebox.showinfo("提示", "该次级关系已更新或已存在。")
            for item in self.items:
                if drawing_bundle.normalize_code(item.get("code")) == child:
                    item["borrow"] = True
                    item["borrow_to"] = parent
                    item["parent_code"] = parent
                    break
            self.refresh_tree()
            self.log("已增加次级条目：%s ← %s" % (parent, child))
            dialog.destroy()

        ttk.Button(form, text="添加", command=save).grid(
            row=4, column=0, columnspan=2, pady=(6, 0)
        )
        dialog.after(50, combo.focus_set)

    def delete_selected_relation(self):
        if self._blocked_by_busy():
            return
        selection = self.tree.selection()
        if not selection:
            return
        row = selection[0]
        rel = self._tree_relations.get(row)
        if rel:
            self._remove_relations([rel])
            if self.bundle is not None:
                self.bundle["relations"] = self.relations
            self.refresh_tree()
            return
        parent = self._tree_codes.get(row)
        if parent:
            removed = [
                rel for rel in self.relations
                if drawing_bundle.normalize_code(rel.get("parent_code")) == parent
            ]
            self._remove_relations(removed)
            if self.bundle is not None:
                self.bundle["relations"] = self.relations
            self.refresh_tree()
            self.log("已删除父级 %s 下的 %d 条关系" % (parent, len(removed)))

    def _remove_relations(self, relations: list[dict]):
        """Canonical removal: relations list and item convenience fields."""
        pairs = {
            (
                drawing_bundle.normalize_code(rel.get("parent_code")),
                drawing_bundle.normalize_code(rel.get("child_code")),
            )
            for rel in relations
        }
        self.relations = [
            rel for rel in self.relations
            if (
                drawing_bundle.normalize_code(rel.get("parent_code")),
                drawing_bundle.normalize_code(rel.get("child_code")),
            )
            not in pairs
        ]
        remaining_parents: dict[str, str] = {}
        for rel in self.relations:
            child = drawing_bundle.normalize_code(rel.get("child_code"))
            parent = drawing_bundle.normalize_code(rel.get("parent_code"))
            if child and parent:
                remaining_parents.setdefault(child, parent)
        removed_children = {child for _, child in pairs}
        for item in self.items:
            code = drawing_bundle.normalize_code(item.get("code"))
            if code in removed_children:
                remaining_parent = remaining_parents.get(code, "")
                item["borrow"] = bool(remaining_parent)
                item["borrow_to"] = remaining_parent
                item["parent_code"] = remaining_parent

    def _show_row_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        item = self._tree_items.get(row)
        relation = self._tree_relations.get(row)
        code = self._tree_codes.get(row, "")
        menu = tk.Menu(self, tearoff=False)
        if code:
            menu.add_command(
                label="增加次级条目…",
                command=lambda: self.add_relation_dialog(code),
            )
        if item is not None:
            menu.add_command(label="修改图纸信息…", command=self._edit_item)
            menu.add_command(
                label="一键规范文件名",
                command=self.normalize_filenames,
            )
        if relation is not None:
            menu.add_separator()
            menu.add_command(
                label="切换 归属子件 / 借用",
                command=self.toggle_selected_relation_type,
            )
            menu.add_command(
                label="删除此关系",
                command=self.delete_selected_relation,
            )
        elif self.tree.get_children(row):
            menu.add_separator()
            menu.add_command(
                label="删除此父级下的全部关系",
                command=self.delete_selected_relation,
            )
        menu.tk_popup(event.x_root, event.y_root)

    def _show_relation_menu(self, event):
        self._show_row_menu(event)

    def _toggle_upload(self, event):
        if self._blocked_by_busy():
            return
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != UPLOAD_COLUMN:
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        item = self._tree_items.get(row)
        if item is None:
            return
        if not item.get("filename_valid", True):
            messagebox.showwarning(
                "文件名不符合",
                "请先双击修改或点击“一键规范文件名”修正后再勾选。",
            )
            return
        item["upload_sel"] = not bool(item.get("upload_sel", True))
        self.refresh_tree()

    def _edit_item(self, _event=None):
        if self._blocked_by_busy():
            return
        selection = self.tree.selection()
        if not selection:
            return
        row = selection[0]
        item = self._tree_items.get(row)
        if item is None:
            if self._tree_relations.get(row) is not None:
                self.toggle_selected_relation_type()
            return
        old_code = drawing_bundle.normalize_code(item.get("code"))
        old_parent = drawing_bundle.normalize_code(
            item.get("parent_code") or item.get("borrow_to")
        )
        dialog = tk.Toplevel(self)
        dialog.title("修改上传信息")
        dialog.transient(self)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=12)
        form.pack()
        values = {}
        for row, (key, label) in enumerate(
            (("code", "图号"), ("name", "名称"), ("machine", "成品代码"))
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="e", pady=3)
            value = tk.StringVar(value=str(item.get(key) or ""))
            ttk.Entry(form, textvariable=value, width=44).grid(
                row=row, column=1, sticky="w", pady=3
            )
            values[key] = value

        ttk.Label(form, text="类型").grid(row=3, column=0, sticky="e", pady=3)
        kind_var = tk.StringVar(value=str(item.get("kind") or "component"))
        ttk.Combobox(
            form,
            textvariable=kind_var,
            values=("process_doc", "machine_drawing", "component", "unknown"),
            state="readonly",
            width=41,
        ).grid(row=3, column=1, sticky="w", pady=3)

        borrow_var = tk.BooleanVar(value=bool(item.get("borrow")))
        ttk.Checkbutton(
            form, text="作为次级条目挂接", variable=borrow_var
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(form, text="上级 PDF 图号").grid(
            row=5, column=0, sticky="e", pady=3
        )
        borrow_to_var = tk.StringVar(
            value=str(
                item.get("borrow_to")
                or item.get("parent_code")
                or (item.get("machine") if item.get("borrow") else "")
                or ""
            )
        )
        ttk.Entry(form, textvariable=borrow_to_var, width=44).grid(
            row=5, column=1, sticky="w", pady=3
        )

        def save():
            new_code = drawing_bundle.normalize_code(values["code"].get())
            new_name = values["name"].get().strip()
            new_machine = drawing_bundle.normalize_code(values["machine"].get())
            new_kind = kind_var.get()
            makes_process = drawing_bundle.is_single_letter_process_code(new_code)
            if makes_process:
                new_name = ""
                if not new_machine:
                    new_machine = new_code[1:]
                new_kind = "process_doc"
            new_borrow = bool(borrow_var.get())
            new_parent = drawing_bundle.normalize_code(borrow_to_var.get())

            # Validate everything BEFORE touching the item, so a rejected edit
            # never leaves a half-applied row behind.
            candidate = dict(item)
            candidate.update(
                code=new_code,
                name=new_name,
                machine=new_machine,
                kind=new_kind,
            )
            problems = drawing_bundle.item_problems(candidate, check_content=False)
            if problems:
                messagebox.showwarning("信息不完整", "；".join(problems))
                return
            if new_borrow and not drawing_bundle.is_valid_code(new_parent):
                messagebox.showwarning(
                    "上级图号无效",
                    "作为次级条目时必须填写有效的上级 PDF 图号。",
                )
                return
            if new_borrow and new_parent.startswith("03."):
                candidate["machine"] = new_parent

            old_name = str(item.get("name") or "").strip()
            old_kind = str(item.get("kind") or "")
            old_machine = drawing_bundle.normalize_code(item.get("machine"))
            old_borrow = bool(item.get("borrow"))
            changed = (
                old_code,
                old_name,
                old_kind,
                old_machine,
                old_parent,
                old_borrow,
            ) != (
                new_code,
                new_name,
                new_kind,
                candidate["machine"],
                new_parent if new_borrow else "",
                new_borrow,
            )
            if not changed:
                dialog.destroy()
                return

            # Migrate the canonical graph: incoming (child) and outgoing
            # (parent) edges follow the renamed code; the previous parent edge
            # is dropped when it no longer applies.  Legacy item.parent_code is
            # derived from the canonical edge, never re-imported.
            if new_code and old_code and old_code != new_code:
                for relation in self.relations:
                    if drawing_bundle.normalize_code(relation.get("child_code")) == old_code:
                        relation["child_code"] = new_code
                    if drawing_bundle.normalize_code(relation.get("parent_code")) == old_code:
                        relation["parent_code"] = new_code
                for other in self.items:
                    if other is item:
                        continue
                    if drawing_bundle.normalize_code(other.get("parent_code")) == old_code:
                        other["parent_code"] = new_code
                        other["borrow_to"] = new_code
            if old_parent and (not new_borrow or new_parent != old_parent):
                self.relations = [
                    rel for rel in self.relations
                    if not (
                        drawing_bundle.normalize_code(rel.get("parent_code")) == old_parent
                        and drawing_bundle.normalize_code(rel.get("child_code")) == new_code
                    )
                ]

            item.update(
                code=new_code,
                name=new_name,
                machine=candidate["machine"],
                kind=new_kind,
                borrow=new_borrow,
                borrow_to=new_parent if new_borrow else "",
                parent_code=new_parent if new_borrow else "",
            )
            if new_borrow and new_parent:
                self._add_relation(
                    new_parent, new_code, source="manual", refresh=False
                )
            if self.bundle is not None:
                self.bundle["relations"] = self.relations
            item["ok"] = bool(item.get("code"))
            ui_logic.invalidate_item(item, "条目已修改，需重新上传")
            self.refresh_tree()
            dialog.destroy()

        ttk.Button(form, text="保存", command=save).grid(
            row=6, column=0, columnspan=2, pady=(10, 0)
        )

    def _build_settings(self) -> dict:
        return ui_logic.snapshot_settings(
            ip=self.ip_var.get(),
            port=self.port_var.get(),
            user=self.user_var.get(),
            password=self.password_var.get(),
            duplicate_policy=ui_logic.duplicate_policy_value(self.duplicate_var.get()),
            start_workflow=bool(self.workflow_var.get()),
            performer=self.performer_var.get(),
        )

    def _note_identity(self, settings: dict):
        """Another server/account invalidates every previous success state."""
        key = ui_logic.identity_key(settings)
        if self._last_identity is not None and key != self._last_identity:
            changed = ui_logic.invalidate_successes(
                self.items, "服务器/账号已变更，需重新上传"
            )
            for relation in self.relations:
                if relation.get("link_status") in ("已借用", "已归属", "已存在"):
                    relation["link_status"] = ""
                    relation["note"] = "服务器/账号已变更，需重新执行"
            if changed:
                self.log("CAXA 服务器或账号已变更，%d 项原成功状态已失效" % changed)
                self.refresh_tree()
        self._last_identity = key

    def check_bundle(self):
        if self._blocked_by_busy():
            return
        if not self.bundle:
            messagebox.showwarning("提示", "请先打开上传包或添加 PDF")
            return
        snapshot = ui_logic.snapshot_manifest(self.bundle)
        result = preview_bundle(snapshot)
        self.status_var.set(
            "总项 %(items)d，选择 %(selected)d，文件缺失 %(missing_files)d，"
            "命名/字段异常 %(invalid_names)d，内容校验失败 %(fingerprint_errors)d"
            % result
        )
        if not result["ready"]:
            details = list(result.get("invalid_details") or [])[:8]
            details += list(result.get("fingerprint_details") or [])[:8]
            details += [issue["message"] for issue in result.get("relation_issues") or []][:8]
            messagebox.showwarning(
                "本地检查未通过",
                self.status_var.get()
                + ("\n\n" + "\n".join(details) if details else "")
                + "\n\n可双击行修正，或点击“一键规范文件名”。",
            )
            return
        if not self.relations:
            self.status_var.set("本地检查通过；没有次级关系，无需连接 CAXA 预检")
            return
        try:
            settings = self._build_settings()
        except ui_logic.SettingsError as exc:
            messagebox.showwarning("检查上传包", str(exc))
            return
        self._note_identity(settings)
        self._save_settings()
        self.set_busy(True)
        self.status_var.set("正在连接 CAXA 预检次级条目（树中无=归属，树中有=借用）…")
        threading.Thread(
            target=self._precheck_worker,
            args=(snapshot, settings),
            daemon=True,
        ).start()

    def _precheck_worker(self, snapshot, settings):
        try:
            info = precheck_with_caxa(
                snapshot, ui_logic.account_from(settings), log=self.log
            )
            self.events.put(("precheck_done", (snapshot, info)))
        except Exception as exc:
            self.events.put(("task_error", "预检失败 %s: %s" % (type(exc).__name__, exc)))

    def start_manual_borrow(self):
        if self._blocked_by_busy():
            return
        snapshot = ui_logic.snapshot_manifest(self.bundle)
        batch_codes = set(self._code_name_map())
        existing_only = [
            relation for relation in snapshot.get("relations") or []
            if drawing_bundle.normalize_code(relation.get("child_code"))
            not in batch_codes
            or relation.get("source") == "manual"
        ]
        if not self.manual_borrows and not existing_only:
            messagebox.showwarning("提示", "请先增加次级/借用条目")
            return
        try:
            settings = self._build_settings()
        except ui_logic.SettingsError as exc:
            messagebox.showwarning("提示", str(exc))
            return
        self._note_identity(settings)
        self._save_settings()
        self.set_busy(True)
        self.status_var.set("执行手动借用…")
        threading.Thread(
            target=self._manual_borrow_worker,
            args=(snapshot, settings),
            daemon=True,
        ).start()

    def _manual_borrow_worker(self, snapshot, settings):
        try:
            result = apply_manual_borrows(
                snapshot,
                ui_logic.account_from(settings),
                log=self.log,
            )
            self.events.put(("manual_done", (snapshot, result)))
        except Exception as exc:
            self.events.put(("task_error", "%s: %s" % (type(exc).__name__, exc)))

    def _save_settings(self):
        self.cfg["caxa_server_ip"] = self.ip_var.get().strip()
        self.cfg["caxa_server_port"] = self.port_var.get().strip()
        self.cfg["caxa_user"] = self.user_var.get().strip()
        self.cfg["start_workflow_after_upload"] = bool(self.workflow_var.get())
        self.cfg["workflow_performer"] = self.performer_var.get().strip()
        self.cfg["duplicate_policy"] = ui_logic.duplicate_policy_value(
            self.duplicate_var.get()
        )
        password = self.password_var.get()
        if password:
            self.cfg["caxa_password_enc"] = config._protect(password)
        config.save(self.cfg)

    def start_upload(self, force_new_task: bool = False):
        if self.busy:
            self.status_var.set("当前任务仍在进行，请等待完成")
            return
        if not self.bundle:
            messagebox.showwarning("提示", "请先打开上传包")
            return
        try:
            settings = self._build_settings()
        except ui_logic.SettingsError as exc:
            messagebox.showwarning("提示", str(exc))
            return
        self._note_identity(settings)
        snapshot = ui_logic.snapshot_manifest(self.bundle)
        if force_new_task:
            snapshot["force_new_task"] = True
        preview = preview_bundle(snapshot)
        if not preview["ready"]:
            details = list(preview.get("invalid_details") or [])[:8]
            details += list(preview.get("fingerprint_details") or [])[:8]
            details += [issue["message"] for issue in preview.get("relation_issues") or []][:8]
            messagebox.showerror(
                "检查未通过",
                "上传包内没有选中的图纸，或存在缺失 PDF/字段/内容校验异常。\n"
                + ("\n".join(details) if details else "")
                + "\n可点击“一键规范文件名”或双击行修正。",
            )
            return
        if settings["start_workflow"]:
            answer = messagebox.askyesno(
                "确认发起流程",
                "图纸上传完成后将立即发起“审核打印”流程，审核人：%s。\n"
                "本批任一项上传失败、关系未成功或结果待核实时不会发起流程。是否继续？"
                % settings["performer"],
            )
            if not answer:
                return

        self._save_settings()
        options = {
            "template_id": int(self.cfg.get("workflow_template_id", 1)),
            "template_instance_id": int(
                self.cfg.get("workflow_template_instance_id", 31)
            ),
            "process_def_id": str(self.cfg.get("workflow_process_def_id", "2")),
            "activity_def_id": str(self.cfg.get("workflow_activity_def_id", "4")),
            "default_performer": str(self.cfg.get("workflow_performer") or ""),
        }
        self.set_busy(True)
        self.status_var.set("上传中…")
        threading.Thread(
            target=self._upload_worker,
            args=(snapshot, settings, options),
            daemon=True,
        ).start()

    def _upload_worker(self, snapshot, settings, options):
        """Worker thread: reads only the frozen snapshot, never Tk variables."""
        try:
            result = upload_bundle(
                snapshot,
                ui_logic.account_from(settings),
                on_duplicate=settings["duplicate_policy"],
                borrow_missing=True,
                start_workflow=settings["start_workflow"],
                workflow_template_id=options["template_id"],
                workflow_template_instance_id=options["template_instance_id"],
                workflow_process_def_id=options["process_def_id"],
                workflow_activity_def_id=options["activity_def_id"],
                performer_name=(
                    settings["performer"] or options.get("default_performer") or ""
                ),
                log=self.log,
                progress=lambda done, total, text: self.events.put(
                    ("status", "%s (%d/%d)" % (text, done, total))
                ),
                force_new_task=bool(snapshot.get("force_new_task")),
                auto_confirm=ui_logic.should_auto_confirm(
                    settings["duplicate_policy"]
                ),
                bundle_path=(
                    str(self.manifest_path.parent) if self.manifest_path else ""
                ),
            )
            self.events.put(("upload_done", (snapshot, result, settings)))
        except Exception as exc:
            self.events.put(("task_error", "%s: %s" % (type(exc).__name__, exc)))

    def recover_pending(self):
        """Explicit, audited recovery of operations with unknown outcome.

        Confirmed successes are kept; only the addressed operation changes.
        Equivalent to app/upload_recover.py, but usable from the UI.
        """
        if self._blocked_by_busy():
            return
        journal = None
        try:
            journal = journal_module.TaskJournal()
        except Exception as exc:
            messagebox.showerror("待核实结果", "无法打开本机任务日志：%s" % exc)
            return
        try:
            tasks = [
                (task, journal.pending_operations(task["task_id"]))
                for task in journal.list_tasks(limit=20)
            ]
            pending_tasks = [(task, rows) for task, rows in tasks if rows]
            if not pending_tasks:
                messagebox.showinfo("待核实结果", "没有待核实的操作。")
                return
            for task, rows in pending_tasks:
                for row in rows:
                    label = (
                        "任务 %s\n操作：%s %s\n阶段：%s\n错误：%s\n\n"
                        "请在 CAXA 中实际核对后再选择。"
                        % (
                            task["task_id"],
                            row["kind"],
                            row["key"],
                            row["phase"],
                            row["error"] or "-",
                        )
                    )
                    if messagebox.askyesno("待核实操作：已确认成功？", label):
                        remote = simpledialog.askstring(
                            "远端对象 ID",
                            "CAXA 中该图纸/流程的 ID（可留空）：",
                            parent=self,
                        )
                        class_id = simpledialog.askstring(
                            "文档类型 ID",
                            "Drawing=1002，CappDocument=1003（可留空）：",
                            parent=self,
                        )
                        iterator = simpledialog.askstring(
                            "实际版次", "实际版次号（可留空）：", parent=self
                        )
                        journal.resolve_operation(
                            task["task_id"],
                            row["kind"],
                            row["key"],
                            result="success",
                            remote_id=remote or None,
                            class_id=class_id or None,
                            iterator_no=iterator or "",
                            note="UI 人工核实成功",
                        )
                        self.log(
                            "已记录人工核实成功：%s %s" % (row["kind"], row["key"])
                        )
                    elif messagebox.askyesno(
                        "待核实操作：确认未写入？",
                        label + "\n\n如果确认 CAXA 中没有对应写入，可选择“是”以便安全重试。",
                    ):
                        journal.resolve_operation(
                            task["task_id"],
                            row["kind"],
                            row["key"],
                            result="no_write",
                            note="UI 人工确认未写入",
                        )
                        self.log(
                            "已记录人工确认未写入：%s %s" % (row["kind"], row["key"])
                        )
            self.status_var.set("待核实处理完成")
        except Exception as exc:
            messagebox.showerror("待核实结果", "%s: %s" % (type(exc).__name__, exc))
        finally:
            journal.close()

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


def main():
    app = UploadApp()
    paths = [path for path in sys.argv[1:] if os.path.isfile(path)]
    if paths:
        app.after(100, lambda: app.add_pdfs(paths))
    app.mainloop()


if __name__ == "__main__":
    main()
