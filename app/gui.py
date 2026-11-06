import datetime
import json
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinterdnd2 import DND_FILES, TkinterDnD

from PIL import Image, ImageTk

import config
import drawing_bundle

KIND_LABEL = {"process_doc": "工艺文档", "machine_drawing": "整机图纸", "component": "组件图纸", "unknown": "未知"}


class App(TkinterDnD.Tk):
    def __init__(self, mode="all"):
        super().__init__()
        self.mode = mode
        self.title(
            "扫描图纸识别与拆分助手"
            if mode == "recognize"
            else "扫描图纸识别上传助手"
        )
        self.geometry("1100x680")
        self.cfg = config.load()
        self.input_pdfs = []
        self.readable_pdf = ""
        self.assembly_pdfs = []
        self.assemblies = []
        self.items = []
        self.batch_dir = ""
        self.queue = queue.Queue()
        self.bridge = None
        self.uploader = None
        self._dialog_evt = threading.Event()
        self._dialog_result = None

        self._build_ui()
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)
        self.after(200, self._poll_queue)

    def _on_drop(self, event):
        try:
            files = self.tk.splitlist(event.data)
        except Exception:
            files = [event.data]
        added = 0
        for f in files:
            if str(f).lower().endswith(".pdf") and os.path.exists(str(f)):
                if f not in self.input_pdfs:
                    self.input_pdfs.append(f)
                    added += 1
        if added:
            self.queue.put(("files", self.input_pdfs))
            self.queue.put(("log", "已拖入 %d 个扫描PDF" % added))

    # ---------- UI ----------
    def _build_ui(self):
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="设置", command=self.open_settings).pack(side="left", padx=2)
        if self.mode != "upload":
            ttk.Button(bar, text="添加扫描件(PDF)", command=self.add_scans).pack(side="left", padx=2)
            ttk.Button(bar, text="添加可读原图PDF(可选)", command=self.add_readable).pack(side="left", padx=2)
            ttk.Button(bar, text="添加总装图(可读PDF)", command=self.add_assembly).pack(side="left", padx=2)
            ttk.Button(bar, text="开始识别", command=self.start_recognize).pack(side="left", padx=2)
            ttk.Button(bar, text="人工补录", command=self.start_manual).pack(side="left", padx=2)
        if self.mode == "all":
            ttk.Button(bar, text="上传到CAXA", command=self.start_upload).pack(side="left", padx=2)
        if self.mode != "upload":
            ttk.Button(bar, text="导出上传包", command=self.export_upload_bundle).pack(side="left", padx=2)
            ttk.Button(bar, text="导出识别结果", command=self.export_results).pack(side="left", padx=2)
            ttk.Button(bar, text="导入识别结果", command=self.import_results).pack(side="left", padx=2)
        if self.mode == "all":
            ttk.Button(bar, text="导出Excel", command=self.export_excel).pack(side="left", padx=2)
        ttk.Button(bar, text="清空", command=self.clear_all).pack(side="left", padx=2)

        mid = ttk.Panedwindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, padx=6, pady=4)

        top = ttk.Frame(mid)
        mid.add(top, weight=3)
        ttk.Label(top, text="输入文件：").pack(anchor="w")
        self.file_list = tk.Listbox(top, height=5)
        self.file_list.pack(fill="x", side="top")

        ttk.Label(top, text="识别结果（双击行可修改，右侧可预览）：").pack(anchor="w")
        cols = ("source", "page", "code", "name", "machine", "kind", "borrow", "up", "status")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=10)
        headers = {"source": "来源", "page": "页", "code": "图号", "name": "名称",
                   "machine": "成品代码", "kind": "类型", "borrow": "借用", "up": "上传", "status": "状态"}
        widths = {"source": 165, "page": 44, "code": 145, "name": 125, "machine": 145, "kind": 72, "borrow": 44, "up": 44, "status": 105}
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.bind("<Button-1>", lambda e: self._on_tree_click(e))
        self.tree.bind("<Double-1>", lambda e: self.edit_item())
        self.preview_label = ttk.Label(top, text="预览", relief="sunken")
        self.preview_label.pack(side="right", fill="y", padx=(6, 0))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_preview())

        ttk.Label(self, text="日志：").pack(anchor="w", padx=6)
        self.log_text = tk.Text(self, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=4)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    def log(self, msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.queue.put(("log", "[%s] %s" % (ts, msg)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "files":
                    self.file_list.delete(0, "end")
                    for f in payload:
                        self.file_list.insert("end", f)
                elif kind == "items":
                    self._refresh_table(payload)
                elif kind == "done":
                    messagebox.showinfo("完成", payload)
                elif kind == "error":
                    messagebox.showerror("错误", payload)
                elif kind == "warn":
                    messagebox.showwarning("提醒", payload)
                elif kind == "manual":
                    idx, timeout = payload
                    it = self.items[idx]
                    self._dialog_result = self._manual_dialog(it, timeout)
                    self._dialog_evt.set()
                elif kind == "edit_assembly":
                    machine, rows = payload
                    self._dialog_result = self._assembly_edit_dialog(machine, rows)
                    self._dialog_evt.set()
                elif kind == "ask_machine":
                    machine = payload
                    self._dialog_result = self._ask_machine_dialog(machine)
                    self._dialog_evt.set()
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ---------- 输入 ----------
    def add_scans(self):
        files = filedialog.askopenfilenames(title="选择扫描件PDF", filetypes=[("PDF", "*.pdf")])
        if files:
            self.input_pdfs.extend(files)
            self.queue.put(("files", self.input_pdfs))

    def add_readable(self):
        f = filedialog.askopenfilename(title="选择可读原图PDF（可选）", filetypes=[("PDF", "*.pdf")])
        if f:
            self.readable_pdf = f
            self.queue.put(("log", "可读原图：%s" % f))

    def add_assembly(self):
        files = filedialog.askopenfilenames(title="选择总装图可读PDF（Z03.xx 开头）", filetypes=[("PDF", "*.pdf")])
        for f in files:
            if f not in self.assembly_pdfs:
                self.assembly_pdfs.append(f)
                self.queue.put(("log", "总装图：%s" % f))

    def clear_all(self):
        self.input_pdfs = []
        self.readable_pdf = ""
        self.assembly_pdfs = []
        self.items = []
        self.queue.put(("files", []))
        self.queue.put(("items", self.items))
        self.queue.put(("status", "已清空"))

    # ---------- 识别 ----------
    def start_recognize(self):
        if not self.input_pdfs:
            messagebox.showwarning("提示", "请先添加扫描件PDF")
            return
        self.queue.put(("status", "识别中…"))
        threading.Thread(target=self._recognize_worker, daemon=True).start()

    def _recognize_worker(self):
        from pipeline import extract, pdf_tools, vision

        try:
            self.batch_dir = os.path.join(os.path.dirname(config.APP_DIR), "work", "batch_%s" % int(time.time()))
            os.makedirs(self.batch_dir, exist_ok=True)
            v = vision.LocalVision(self.cfg.get("vision_server", ""), self.cfg.get("vision_script", ""),
                                   enable_thinking=bool(self.cfg.get("vision_thinking", False)))
            assemblies = []
            for ap in self.assembly_pdfs:
                if os.path.exists(ap):
                    from pipeline import matcher
                    try:
                        a = matcher.load_assembly(ap)
                        if a.get("invalid"):
                            self.log("已忽略 %s：%s" % (os.path.basename(ap), a.get("reason", "非 Z03 总装图")))
                            continue
                        assemblies.append(a)
                        base = os.path.basename(ap)
                        self.log("总装图 %s → 整机 %s，明细表 %d 项" % (base, a["machine"] or "?", len(a["codes"])))
                    except Exception as ex:
                        self.log("总装图解析失败 %s：%s" % (os.path.basename(ap), ex))
            self.assemblies = assemblies
            if assemblies:
                self.log("总装图明细表已加载：%s" % "、".join(a["machine"] or os.path.basename(a["file"]) for a in assemblies))
            candidates = []
            if self.readable_pdf and os.path.exists(self.readable_pdf):
                from pipeline import matcher
                candidates = matcher.build_candidates_from_texts(pdf_tools.extract_all_text(self.readable_pdf))
                self.log("可读原图候选 %d 条" % len(candidates))
            items = []
            recognized_z_machines = set()
            for src in self.input_pdfs:
                self.log("处理：%s" % os.path.basename(src))
                pages = pdf_tools.split_pdf(src, os.path.join(self.batch_dir, "pages"))
                for idx, page_pdf in enumerate(pages):
                    png = os.path.join(self.batch_dir, "render_%d.png" % idx)
                    pdf_tools.render_pdf_page(page_pdf, 0, png)
                    raw = v.ask(png, extract.PROMPT)
                    fields = extract.parse_vision_output(raw)
                    resolved = extract.resolve_document(fields, source_filename=src, raw=raw)
                    cand = None
                    if candidates:
                        from pipeline import matcher
                        cand, score = matcher.match_scanned(fields, candidates)
                        if cand and score >= 0.85:
                            # 只填空、不覆盖：视觉读出的字段优先，候选仅补缺
                            if not resolved["code"]:
                                resolved["code"] = cand["code"]
                            if not resolved["name"]:
                                resolved["name"] = cand["name"]
                            if not resolved["machine"]:
                                resolved["machine"] = cand["machine"]
                            if resolved["kind"] == "unknown" and cand["kind"] != "unknown":
                                resolved["kind"] = cand["kind"]
                            self.log("可读原图匹配候选：%s（%s）" % (cand["code"], cand["name"]))
                    items.append({
                        "source": os.path.basename(src),
                        "page": idx + 1,
                        "page_pdf": page_pdf,
                        "png": png,
                        "code": resolved["code"],
                        "name": resolved["name"],
                        "machine": resolved["machine"],
                        "kind": resolved["kind"],
                        "ok": resolved["ok"],
                        "calc_doc": resolved.get("calc_doc", False),
                        "upload_sel": True,
                        "status": "待确认" if not resolved["ok"] else "待上传",
                        "target": "",
                        "note": "",
                    })
                    need_code = resolved["kind"] == "component" and not resolved["code"].startswith(("01.", "02.", "04."))
                    need_machine = resolved["kind"] in ("component", "machine_drawing") and not resolved["machine"]
                    need_z = resolved["kind"] == "machine_drawing" and resolved["code"].startswith("03.")
                    # 组件图号以标题栏为准：全页模型可能把材料明细表里的图号读成“图号”字段，
                    # 紧裁剪（标题栏本身）取出的最后一个 01/02/04 代码才是组件自己的图号
                    verify_component = resolved["kind"] == "component"
                    if need_code or need_machine or need_z or verify_component:
                        try:
                            cand_code = ""
                            cand_machine = ""
                            cand_z = ""
                            try:
                                from PIL import Image
                                img = Image.open(png).convert("RGB")
                                w, h = img.size
                                # 紧裁剪：只圈标题栏本身（明细表通常在标题栏上方，避免误取明细表图号）
                                tb = img.crop((int(w * 0.45), int(h * 0.70), w, h))
                                tb = tb.resize((int(tb.width * 1.8), int(tb.height * 1.8)), Image.LANCZOS)
                                tb_png = png.replace(".png", "_tb.png")
                                tb.save(tb_png)
                                raw2 = v.ask(tb_png, extract.PROMPT_TITLEBLOCK, max_tokens=800)
                                import re as _re
                                found_codes = []
                                for m in _re.finditer(r"(?<![0-9A-Z.])(\d{2}\.[A-Z0-9.\-]+)", raw2.upper()):
                                    c = extract.normalize_code(m.group(1))
                                    if c.startswith(("01.", "02.", "04.")):
                                        found_codes.append(c)
                                if found_codes:
                                    cand_code = found_codes[-1]  # 标题栏在最底部，图号应出现在最后
                                for m in _re.finditer(r"(?<![0-9A-Z.])03\.[A-Z0-9.\-]+", raw2.upper()):
                                    c = extract.normalize_code(m.group(0))
                                    if c.startswith("03.") and not c.startswith("03.GPZ."):
                                        cand_machine = c
                                        break
                                if need_z:
                                    # Z03/明细表判定用宽裁剪（含材料明细表区域）
                                    tbw = img.crop((int(w * 0.40), int(h * 0.45), w, h))
                                    tbw = tbw.resize((int(tbw.width * 1.5), int(tbw.height * 1.5)), Image.LANCZOS)
                                    tbw_png = png.replace(".png", "_tbw.png")
                                    tbw.save(tbw_png)
                                    raw2z = v.ask(tbw_png, extract.PROMPT_TITLEBLOCK, max_tokens=800)
                                    mz = _re.search(r"Z03\.[A-Z0-9.\-]+", raw2z.upper())
                                    if mz:
                                        cand_z = extract.normalize_code(mz.group(0))
                                    else:
                                        up2 = raw2z.upper()
                                        if "物料代码" in up2 or ("序号" in up2 and "物料" in up2):
                                            cand_z = "Z" + items[-1]["code"]
                            except Exception as ex:
                                self.log("标题栏放大识别失败：%s" % ex)
                            if (need_code and not cand_code) or (need_machine and not cand_machine):
                                raw3 = v.ask(png, extract.PROMPT_MACHINE, max_tokens=120)
                                fields3 = extract.parse_vision_output(raw3)
                                if not cand_code:
                                    cand_code = extract.normalize_code(fields3.get("图号") or "")
                                if not cand_machine:
                                    cand_machine = extract.normalize_code(fields3.get("成品代码") or "")
                            if cand_code.startswith(("01.", "02.", "04.")) and (need_code or verify_component):
                                if cand_code != items[-1]["code"]:
                                    self.log("标题栏校正图号：%s" % cand_code)
                                items[-1]["code"] = cand_code
                                items[-1]["ok"] = True
                                items[-1]["status"] = "待上传"
                            if need_machine and cand_machine.startswith("03."):
                                items[-1]["machine"] = cand_machine
                                self.log("补充识别成品代码：%s" % items[-1]["machine"])
                            if need_z and cand_z:
                                items[-1]["code"] = cand_z
                                items[-1]["kind"] = "process_doc"
                                items[-1]["machine"] = cand_z[1:]
                                items[-1]["ok"] = True
                                items[-1]["status"] = "待上传"
                                recognized_z_machines.add(cand_z[1:])
                                self.log("识别为总装图：%s" % cand_z)
                            if need_code and not items[-1]["code"].startswith(("01.", "02.", "04.")):
                                self.log("组件图号仍缺失，请在补录中手动填写")
                            elif need_machine and not items[-1]["machine"]:
                                self.log("未找到 03. 开头的整机代码（通用件，无需借用）")
                        except Exception as ex:
                            self.log("成品代码重试失败：%s" % ex)
                    # 扫描件识别出的 Z03 总装图（含 03→Z 转换与直接读出 Z03.xx 两种路径）：
                    # 统一提取明细表并弹窗确认，避免只走转换分支时漏提取
                    if items[-1]["kind"] == "process_doc" and items[-1]["code"].startswith("Z03."):
                        z_machine = items[-1]["code"][1:]
                        recognized_z_machines.add(z_machine)
                        if not any(a.get("machine") == z_machine and a.get("from_scan") for a in assemblies):
                            try:
                                bom_rows = self._extract_bom_from_scan(png, v)
                            except Exception as ex:
                                bom_rows = []
                                self.log("总装图明细表提取失败：%s" % ex)
                            if bom_rows:
                                rows = self._ask_edit_assembly(z_machine, bom_rows)
                                if rows:
                                    assemblies.append({
                                        "machine": z_machine,
                                        "codes": [r["code"] for r in rows],
                                        "names": {r["code"]: r["name"] for r in rows},
                                        "file": page_pdf,
                                        "from_scan": True,
                                    })
                                    self.log("扫描总装图明细表 %d 项已确认，用于借用：%s" % (
                                        len(rows), "、".join(r["code"] for r in rows)))
                                else:
                                    self.log("扫描总装图明细表未确认，该整机组件将不借用")
                            else:
                                self.log("扫描总装图未提取到明细表（如需借用请添加可读总装图）")
            # 借用判定：放在全部页面识别完后，确保扫描识别出的总装图明细表也已确认
            for it in items:
                if it["kind"] == "component":
                    borrow, reason = self._decide_borrow(it, assemblies)
                    it["borrow"] = borrow
                    self.log("组件 %s 借用=%s（%s）" % (it["code"], "是" if borrow else "否", reason))
            if recognized_z_machines:
                added_z_machines = {a.get("machine") for a in assemblies if a.get("machine")}
                missing_z = sorted(recognized_z_machines - added_z_machines)
                if missing_z:
                    self.log("注意：识别到总装图 %s，但未添加可读总装图且扫描明细表未确认，"
                             "这些整机的组件将不借用；"
                             "如需借用请添加 Z03 开头的可读总装图" % "、".join(missing_z))
            self._group_two_pagers(items)
            self.items = items
            calc_items = [it for it in items if it.get("calc_doc")]
            if calc_items:
                names = "、".join("第%s页 %s" % (it["page"], it["name"] or it["code"]) for it in calc_items)
                self.queue.put(("warn", "检测到计算书类文档（正常不应出现）：%s\n请确认是否误放，再决定是否上传。" % names))
            debug_out = os.path.join(os.path.dirname(config.APP_DIR), "work", "gui_last_recognition.json")
            try:
                debug_rows = []
                for it in items:
                    debug_rows.append({
                        "source": it.get("source", ""), "page": it.get("page", ""),
                        "code": it.get("code", ""), "name": it.get("name", ""),
                "machine": it.get("machine", ""), "kind": it.get("kind", ""),
                "borrow": it.get("borrow", False),
                "status": it.get("status", ""), "ok": it.get("ok", False),
                "caxa_path": it.get("caxa_path", ""),
                    })
                with open(debug_out, "w", encoding="utf-8") as f:
                    json.dump(debug_rows, f, ensure_ascii=False, indent=1)
            except Exception as ex:
                self.log("识别结果落盘失败：%s" % ex)
            cache_out = os.path.join(os.path.dirname(config.APP_DIR), "work", "recognition_cache.json")
            try:
                with open(cache_out, "w", encoding="utf-8") as f:
                    json.dump({
                        "input_pdfs": self.input_pdfs,
                        "readable_pdf": self.readable_pdf,
                        "assembly_pdfs": self.assembly_pdfs,
                        "assemblies": self.assemblies,
                        "items": items,
                    }, f, ensure_ascii=False, indent=1)
                self.log("识别结果已缓存：%s" % cache_out)
            except Exception as ex:
                self.log("识别结果缓存失败：%s" % ex)
            self.queue.put(("items", items))
            self.queue.put(("status", "识别完成，共 %d 项（可先人工补录）" % len(items)))
        except Exception as ex:
            self.queue.put(("error", "识别失败：%s" % ex))

    def _group_two_pagers(self, items):
        """只合并同一来源中相邻、图号相同且名称不冲突的页。"""
        from pipeline import pdf_tools

        items.sort(key=lambda x: (x["source"], int(str(x["page"]).split(",")[0])))
        merged = []
        i = 0
        while i < len(items):
            first = items[i]
            j = i
            while j + 1 < len(items):
                nxt = items[j + 1]
                same_source = nxt["source"] == first["source"]
                same_code = nxt["code"] == first["code"]
                same_kind = nxt["kind"] == first["kind"]
                adjacent = int(str(nxt["page"]).split(",")[0]) == int(str(items[j]["page"]).split(",")[0]) + 1
                name_ok = (not first["name"]) or (not nxt["name"]) or (nxt["name"] == first["name"])
                if same_source and same_code and same_kind and adjacent and name_ok:
                    j += 1
                else:
                    break
            if j > i:
                group = items[i:j + 1]
                first = dict(group[0])
                first["page"] = ",".join(str(x["page"]) for x in group)
                out = os.path.join(self.batch_dir, "merged_%s.pdf" % (first["code"] or "unknown"))
                pdf_tools.merge_pdfs([x["page_pdf"] for x in group], out)
                first["page_pdf"] = out
                first["status"] = "待上传"
                merged.append(first)
            else:
                merged.append(first)
            i = j + 1
        items[:] = merged

    def _decide_borrow(self, it, assemblies):
        """借用判定：只有 Z03 整机总装图（工艺文档）的明细表驱动借用；
        组件图纸（如传动组件）里的材料明细表不参与；无 Z03 一律不借用。
        返回 (borrow, reason)，可能由总装图补全 it['machine']。"""
        code = (it.get("code") or "").upper()
        machine = (it.get("machine") or "").upper()
        borrow = None
        for a in assemblies:
            codes = [str(c).upper() for c in (a.get("codes") or [])]
            if code in codes:
                borrow = True
                if not machine and a.get("machine"):
                    machine = str(a["machine"]).upper()
                    it["machine"] = machine
                break
        if borrow is None:
            borrow = False  # 无 Z03 总装图 → 不借用（移除 04.xx 回退）
        if machine and machine.startswith("03.GPZ."):
            borrow = False
        if borrow:
            reason = "总装图明细表"
        elif machine and machine.startswith("03.GPZ."):
            reason = "03.GPZ钢瓶组"
        elif machine and any(str(a.get("machine", "")).upper() == machine for a in assemblies):
            reason = "有总装图未列入"
        else:
            reason = "无Z03总装图/未命中"
        return bool(borrow), reason

    def _extract_bom_from_scan(self, png, v):
        """两步提取扫描总装图明细表（不依赖固定裁剪，兼容 A4 与不同布局）：
        1) 整页缩小后让 qwen 定位材料明细表区域（返回 0~1 比例坐标）；
        2) 按坐标裁剪原图，把第一轮作为 history 发在同一窗口，再让 qwen 转录 markdown。
        定位失败回退到页面下部 45%~100% 裁剪（A3 常见布局）。"""
        from PIL import Image
        from pipeline import extract, vision
        img = Image.open(png).convert("RGB")
        w, h = img.size
        # ---- 第 1 步：整页缩小，让模型定位明细表区域 ----
        locate_png = png.replace(".png", "_locate.png")
        scale = min(1.0, 1280.0 / max(w, h))
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        small.save(locate_png)
        raw_loc = v.ask(locate_png, extract.PROMPT_BOM_LOCATE, max_tokens=120)
        region = extract.parse_bom_region(raw_loc)
        history = None
        if region:
            x0, y0, x1, y1 = region
            pad = 0.015
            bx0 = max(0, int((x0 - pad) * w)); by0 = max(0, int((y0 - pad) * h))
            bx1 = min(w, int((x1 + pad) * w)); by1 = min(h, int((y1 + pad) * h))
            crop = img.crop((bx0, by0, bx1, by1))
            cw, ch = crop.size
            if max(cw, ch) < 1000:
                k = 1000.0 / max(cw, ch)
                crop = crop.resize((int(cw * k), int(ch * k)), Image.LANCZOS)
            elif max(cw, ch) > 1800:
                k = 1800.0 / max(cw, ch)
                crop = crop.resize((int(cw * k), int(ch * k)), Image.LANCZOS)
            bom_png = png.replace(".png", "_bom.png")
            crop.save(bom_png)
            history = [
                {"role": "user", "content": [vision.image_message(locate_png),
                                             {"type": "text", "text": extract.PROMPT_BOM_LOCATE}]},
                {"role": "assistant", "content": raw_loc},
            ]
            self.log("已由模型定位明细表区域：(%s)（原回答：%s）" % (
                "%.2f,%.2f,%.2f,%.2f" % region, raw_loc.strip()[:60]))
        else:
            crop = img.crop((0, int(h * 0.45), w, h))
            crop = crop.resize((int(crop.width * 1.6), int(crop.height * 1.6)), Image.LANCZOS)
            bom_png = png.replace(".png", "_bom.png")
            crop.save(bom_png)
            self.log("明细表区域定位失败（%s），回退页面下部裁剪" % raw_loc.strip()[:60])
        # ---- 第 2 步：同窗口（history 含第 1 轮的整页图与定位回答）转录裁剪区域 ----
        raw = v.ask(bom_png, extract.PROMPT_BOM, max_tokens=1500, history=history)
        return extract.parse_bom_markdown(raw)

    def _ask_edit_assembly(self, machine, rows):
        """把扫描总装图明细表交给主线程弹窗编辑；返回确认后的 rows 或 None（跳过/超时）。"""
        timeout = float(self.cfg.get("timeout_seconds", 60))
        self.queue.put(("edit_assembly", (machine, rows)))
        self._dialog_evt.clear()
        self._dialog_evt.wait(timeout + 15)
        return self._dialog_result

    def _assembly_edit_dialog(self, machine, rows):
        """主线程弹窗：编辑扫描总装图明细表（图号+名称）。
        确定→返回编辑后的 rows；跳过/超时→None（该整机不借用）。"""
        result = {"v": None}
        data = [dict(r) for r in rows]
        timeout = float(self.cfg.get("timeout_seconds", 60))

        def run():
            dlg = tk.Toplevel(self)
            dlg.title("总装图明细表确认 - %s" % machine)
            dlg.geometry("580x460")
            dlg.transient(self)
            dlg.grab_set()
            ttk.Label(dlg, text="已从扫描总装图提取材料明细表（图号+名称），请核对/修改后点“确定”；"
                                "“跳过”则本整机不借用。",
                      wraplength=540).pack(anchor="w", padx=10, pady=(10, 4))
            tree = ttk.Treeview(dlg, columns=("code", "name"), show="headings", height=12)
            tree.heading("code", text="图号")
            tree.heading("name", text="名称")
            tree.column("code", width=190)
            tree.column("name", width=320)
            tree.pack(fill="both", expand=True, padx=10)

            def refresh():
                tree.delete(*tree.get_children())
                for i, r in enumerate(data):
                    tree.insert("", "end", iid=str(i), values=(r["code"], r["name"]))

            refresh()
            count = {"n": 0}

            def tick():
                if timeout <= 0:
                    return
                count["n"] += 1
                left = timeout - count["n"]
                if left <= 0:
                    result["v"] = "timeout"
                    dlg.destroy()
                    return
                btn_ok.configure(text="确定 (%ds)" % int(left))
                dlg.after(1000, tick)

            def edit_selected():
                sel = tree.selection()
                if not sel:
                    return
                i = int(sel[0])
                d2 = tk.Toplevel(dlg)
                d2.title("编辑明细表行")
                d2.transient(dlg)
                d2.grab_set()
                f = ttk.Frame(d2, padding=12)
                f.pack()
                cv = tk.StringVar(value=data[i]["code"])
                nv = tk.StringVar(value=data[i]["name"])
                ttk.Label(f, text="图号").grid(row=0, column=0, sticky="w")
                ttk.Entry(f, textvariable=cv, width=42).grid(row=0, column=1, pady=3)
                ttk.Label(f, text="名称").grid(row=1, column=0, sticky="w")
                ttk.Entry(f, textvariable=nv, width=42).grid(row=1, column=1, pady=3)

                def ok():
                    data[i]["code"] = extract.normalize_code(cv.get())
                    data[i]["name"] = nv.get().strip()
                    refresh()
                    d2.destroy()

                ttk.Button(f, text="确定", command=ok).grid(row=2, column=0, columnspan=2, pady=8)
                d2.wait_window()

            def add_row():
                data.append({"code": "", "name": ""})
                refresh()
                tree.selection_set(str(len(data) - 1))
                edit_selected()

            def del_selected():
                sel = tree.selection()
                if not sel:
                    return
                data.pop(int(sel[0]))
                refresh()

            btns = ttk.Frame(dlg)
            btns.pack(pady=8)
            ttk.Button(btns, text="添加行", command=add_row).pack(side="left", padx=3)
            ttk.Button(btns, text="编辑选中", command=edit_selected).pack(side="left", padx=3)
            ttk.Button(btns, text="删除选中", command=del_selected).pack(side="left", padx=3)
            btn_ok = ttk.Button(btns, text="确定",
                                command=lambda: (result.__setitem__("v", "ok"), dlg.destroy()))
            btn_ok.pack(side="left", padx=3)
            ttk.Button(btns, text="跳过",
                       command=lambda: (result.__setitem__("v", "skip"), dlg.destroy())).pack(side="left", padx=3)
            dlg.after(1000, tick)
            dlg.wait_window()

        run()
        if result["v"] == "ok":
            out = [r for r in data if r.get("code")]
            return out or None
        return None

    def _refresh_table(self, items):
        self.tree.delete(*self.tree.get_children())
        for it in items:
            self.tree.insert("", "end", iid=str(items.index(it)), values=(
                it["source"], it["page"], it["code"], it["name"], it["machine"],
                KIND_LABEL.get(it["kind"], it["kind"]),
                "是" if it.get("borrow") else ("否" if it.get("borrow") is not None else "—"),
                "是" if it.get("upload_sel", True) else "否",
                it["status"]))

    def _on_tree_click(self, event):
        try:
            if self.tree.identify("region", event.x, event.y) != "cell":
                return
            col = self.tree.identify_column(event.x)
            up_index = "#%d" % (self.tree["columns"].index("up") + 1)
            if col != up_index:
                return
            row = self.tree.identify_row(event.y)
            if not row:
                return
            it = self.items[int(row)]
            it["upload_sel"] = not it.get("upload_sel", True)
            self.queue.put(("items", self.items))
        except Exception:
            pass

    def update_preview(self):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            it = self.items[int(sel[0])]
            img = Image.open(it["png"]).convert("RGB")
            img.thumbnail((260, 340))
            self._preview_img = ImageTk.PhotoImage(img)
            self.preview_label.configure(image=self._preview_img)
        except Exception:
            pass

    # ---------- 人工补录 ----------
    def start_manual(self):
        if not self.items:
            messagebox.showwarning("提示", "请先识别")
            return
        threading.Thread(target=self._manual_worker, daemon=True).start()

    def _manual_worker(self):
        timeout = float(self.cfg.get("timeout_seconds", 60))
        pending_dir = self.cfg.get("pending_folder") or os.path.join(os.path.dirname(config.APP_DIR), "work", "待审")
        os.makedirs(pending_dir, exist_ok=True)
        for idx, it in enumerate(self.items):
            if it["status"] in ("已上传", "失败", "已跳过"):
                continue
            if it["ok"] and it["code"] and it["name"]:
                continue
            self.queue.put(("status", "人工补录：%s 第 %s 页" % (it["source"], it["page"])))
            self.queue.put(("manual", (idx, timeout)))
            self._dialog_evt.clear()
            self._dialog_evt.wait(timeout + 15)
            result = self._dialog_result
            if result == "ok":
                it["status"] = "待上传"
                self.queue.put(("items", self.items))
            elif result == "skip":
                it["status"] = "已跳过"
                it["note"] = "人工跳过"
            elif result == "timeout":
                dst = os.path.join(pending_dir, "%s_p%s.pdf" % (os.path.splitext(it["source"])[0], it["page"]))
                try:
                    shutil.copyfile(it["page_pdf"], dst)
                    it["status"] = "已移入待审"
                    it["note"] = dst
                except Exception as ex:
                    it["status"] = "失败"
                    it["note"] = str(ex)
                self.queue.put(("items", self.items))
        self.queue.put(("done", "人工补录完成"))

    def _manual_dialog(self, it, timeout):
        """模态弹窗，返回 ok/skip/timeout。在主线程运行。"""
        result = {"v": None}

        def run():
            dlg = tk.Toplevel(self)
            dlg.title("人工补录 - %s 第%s页" % (it["source"], it["page"]))
            dlg.geometry("680x420")
            dlg.transient(self)
            dlg.grab_set()
            row = ttk.Frame(dlg)
            row.pack(side="left", fill="y", padx=8, pady=8)
            try:
                img = Image.open(it["png"]).convert("RGB")
                img.thumbnail((360, 420))
                tkimg = ImageTk.PhotoImage(img)
                ttk.Label(row, image=tkimg).pack()
                row.image = tkimg
            except Exception:
                pass
            form = ttk.Frame(dlg)
            form.pack(side="left", fill="both", expand=True, padx=8, pady=8)
            vars_ = {}
            for i, key in enumerate(("code", "name", "machine")):
                ttk.Label(form, text={"code": "图号", "name": "名称", "machine": "成品代码"}[key]).grid(row=i, column=0, sticky="w")
                var = tk.StringVar(value=it.get(key) or "")
                ttk.Entry(form, textvariable=var, width=42).grid(row=i, column=1, pady=3)
                vars_[key] = var
            ttk.Label(form, text="类型").grid(row=3, column=0, sticky="w")
            kind_var = tk.StringVar(value=it.get("kind") or "component")
            cb = ttk.Combobox(form, textvariable=kind_var, values=["process_doc", "machine_drawing", "component"], state="readonly", width=40)
            cb.grid(row=3, column=1, pady=3)
            count = {"n": 0}

            def tick():
                if timeout <= 0:
                    return
                count["n"] += 1
                left = timeout - count["n"]
                if left <= 0:
                    result["v"] = "timeout"
                    dlg.destroy()
                    return
                btn_ok.configure(text="确认 (%ds)" % int(left))
                dlg.after(1000, tick)

            btns = ttk.Frame(form)
            btns.grid(row=4, column=0, columnspan=2, pady=10)
            btn_ok = ttk.Button(btns, text="确认", command=lambda: (vars_ and self._apply_manual(it, vars_, kind_var), result.__setitem__("v", "ok"), dlg.destroy()))
            btn_ok.pack(side="left", padx=4)
            ttk.Button(btns, text="跳过", command=lambda: (result.__setitem__("v", "skip"), dlg.destroy())).pack(side="left", padx=4)
            dlg.after(1000, tick)
            dlg.wait_window()

        run()
        return result["v"] or ("timeout" if timeout > 0 else "skip")

    def _apply_manual(self, it, vars_, kind_var):
        from pipeline import extract

        it["code"] = extract.normalize_code(vars_["code"].get())
        it["name"] = vars_["name"].get().strip()
        it["machine"] = extract.normalize_code(vars_["machine"].get())
        it["kind"] = kind_var.get()
        it["ok"] = bool(it["code"])

    def edit_item(self):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0])]
        self._manual_dialog(it, 0)
        self.queue.put(("items", self.items))

    # ---------- 上传 ----------
    def start_upload(self):
        if not self.items:
            messagebox.showwarning("提示", "请先识别")
            return
        if not self.cfg.get("caxa_user") or not self.cfg.get("caxa_password_enc"):
            messagebox.showwarning("提示", "请先在“设置”里填写 CAXA 账号密码")
            return
        self.queue.put(("status", "上传中…"))
        threading.Thread(target=self._upload_worker, daemon=True).start()

    def _upload_worker(self):
        from caxa.bridge import CaxaBridge, CaxaError
        from caxa.uploader import CaxaUploader, MachineMissing

        try:
            self.bridge = CaxaBridge()
            on_dup = "new_version" if self.cfg.get("dup_new_version") else "reuse"
            self.uploader = CaxaUploader(self.bridge, log=self.log, on_duplicate=on_dup)
            if on_dup == "new_version":
                self.log("重复图号策略：上传后新增版本/版次（仅未发布文档，无需反审，尚未真机验证）")
            self.log("登录 CAXA …")
            self.uploader.bridge.login(self.cfg["caxa_server_ip"], int(self.cfg["caxa_server_port"]),
                                       self.cfg["caxa_user"], config.get_password(self.cfg))
            self.log("登录成功")
            try:
                ci = self.bridge.call({"cmd": "class_info", "class_ids": [1002, 1003, 1001]})
                for c in ci.get("classes", []):
                    self.log("文档类 %s：%s（%s）" % (c.get("id"), c.get("name"), c.get("ext")))
            except Exception as ex:
                self.log("类名查询失败：%s" % ex)
            batch_codes = {it["code"].upper() for it in self.items if it["code"]}
            uploaded_pairs = set()
            order = sorted(enumerate(self.items), key=lambda p: 0 if p[1]["kind"] == "machine_drawing" else 1)
            for idx, it in order:
                if it["status"] in ("已上传", "已移入待审", "已跳过"):
                    continue
                if not it.get("upload_sel", True):
                    it["status"] = "未勾选"
                    continue
                if not it["code"]:
                    it["status"] = "失败"
                    it["note"] = "缺少图号"
                    continue
                for attempt in range(3):
                    try:
                        r = self.uploader.upload_item(it, batch_codes, on_machine_missing=None)
                        if it["kind"] == "component" and it.get("borrow"):
                            uploaded_pairs.add((str(it.get("machine") or "").upper(), it["code"].upper()))
                        it["status"] = "已上传"
                        it["target"] = r.get("target", "")
                        it["note"] = r.get("file_name", "")
                        it["caxa_path"] = r.get("caxa_path", "")
                        self.log("已上传 %s -> %s（CAXA路径：%s）" % (
                            it["code"], it["target"], it.get("caxa_path", "")))
                        break
                    except MachineMissing as ex:
                        if attempt >= 2:
                            it["status"] = "失败"
                            it["note"] = str(ex)
                            break
                        self.queue.put(("ask_machine", ex.machine))
                        self._dialog_evt.clear()
                        self._dialog_evt.wait(600)
                        if not self._dialog_result:
                            it["status"] = "失败"
                            it["note"] = str(ex)
                            break
                    except CaxaError as ex:
                        it["status"] = "失败"
                        it["note"] = str(ex)
                        self.log("上传失败 %s：%s" % (it["code"], ex))
                        break
                    except Exception as ex:
                        it["status"] = "失败"
                        it["note"] = str(ex)
                        self.log("上传异常 %s：%s" % (it["code"], ex))
                        break
                self.queue.put(("items", self.items))
            borrow_total = 0
            if self.cfg.get("borrow_only_missing", True) and getattr(self, "assemblies", None):
                self.log("按总装图明细表借用未收录组件（只借用不新建）…")
                for a in self.assemblies:
                    machine = (a.get("machine") or "").upper()
                    codes = [str(c).upper() for c in (a.get("codes") or [])]
                    if machine and codes:
                        borrow_total += self.uploader.borrow_missing_components(
                            machine, codes, batch_codes, uploaded_pairs)
                if borrow_total:
                    self.log("明细表只借用完成：共 %d 项" % borrow_total)
            self.queue.put(("status", "上传完成"))
            self.queue.put(("done", "上传完成%s，可导出 Excel 核对" %
                            ("（明细表借用 %d 项）" % borrow_total if borrow_total else "")))
        except Exception as ex:
            self.queue.put(("error", "上传流程异常：%s" % ex))
        finally:
            if self.bridge:
                self.bridge.stop()

    def _ask_machine_dialog(self, machine):
        """主线程弹窗：请用户在 CAXA 新建整机后点继续。返回是否继续。"""
        box = {}

        def run():
            root = tk.Toplevel(self)
            root.title("需要新建整机")
            root.transient(self)
            root.grab_set()
            ttk.Label(root, text="整机 %s 在 CAXA 中不存在。\n请在 CAXA 客户端中新建该整机（产品），完成后点“继续”。" % machine,
                      wraplength=420).pack(padx=16, pady=14)
            btns = ttk.Frame(root)
            btns.pack(pady=8)
            ttk.Button(btns, text="继续", command=lambda: (box.__setitem__("v", True), root.destroy())).pack(side="left", padx=4)
            ttk.Button(btns, text="跳过此项", command=lambda: (box.__setitem__("v", False), root.destroy())).pack(side="left", padx=4)
            root.wait_window()

        run()
        return box.get("v", False)

    # ---------- 导出 ----------
    def export_upload_bundle(self):
        if not self.items:
            messagebox.showwarning("提示", "当前没有识别结果")
            return
        initial = os.path.join(
            os.path.dirname(config.APP_DIR),
            "work",
            "图纸上传包_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        )
        destination = filedialog.askdirectory(
            title="选择上传包输出目录（将写入 manifest.json 和 PDF）",
            initialdir=os.path.dirname(initial),
        )
        if not destination:
            return
        destination = os.path.join(
            destination, "图纸上传包_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        try:
            manifest_path = drawing_bundle.export_bundle(
                self.items,
                destination,
                source_files=self.input_pdfs,
                assemblies=self.assemblies,
            )
            self.log("上传包已导出：%s" % manifest_path.parent)
            messagebox.showinfo(
                "导出完成",
                "上传包已生成：\n%s\n\n可以把整个目录交给上传助手使用。"
                % manifest_path.parent,
            )
        except Exception as ex:
            messagebox.showerror("导出失败", str(ex))

    def export_results(self):
        if not self.items:
            messagebox.showwarning("提示", "当前没有识别结果")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile="识别结果.json",
            filetypes=[("JSON", "*.json")],
            initialdir=os.path.join(os.path.dirname(config.APP_DIR), "work"))
        if not path:
            return
        data = {
            "input_pdfs": self.input_pdfs,
            "readable_pdf": self.readable_pdf,
            "assembly_pdfs": self.assembly_pdfs,
            "assemblies": self.assemblies,
            "items": self.items,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            self.log("识别结果已导出：%s" % path)
        except Exception as ex:
            messagebox.showerror("导出失败", str(ex))

    def import_results(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json")],
            initialdir=os.path.join(os.path.dirname(config.APP_DIR), "work"))
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as ex:
            messagebox.showerror("导入失败", str(ex))
            return
        self.items = data.get("items", [])
        self.input_pdfs = data.get("input_pdfs", [])
        self.readable_pdf = data.get("readable_pdf", "")
        self.assembly_pdfs = data.get("assembly_pdfs", [])
        self.assemblies = data.get("assemblies", [])
        if not self.assemblies:
            # 旧版本导出的结果没有 assemblies：路径仍在时重新解析
            from pipeline import matcher
            for ap in self.assembly_pdfs:
                if os.path.exists(ap):
                    try:
                        a = matcher.load_assembly(ap)
                        if not a.get("invalid"):
                            self.assemblies.append(a)
                    except Exception:
                        pass
        if self.items and self.items[0].get("page_pdf") and os.path.exists(self.items[0]["page_pdf"]):
            self.batch_dir = os.path.dirname(self.items[0]["page_pdf"])
        self.queue.put(("files", self.input_pdfs))
        self.queue.put(("items", self.items))
        self.log("已导入 %d 项识别结果（%s），可直接上传" % (len(self.items), path))

    def export_excel(self):
        from pipeline import excel_out

        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
                                            initialfile="上传结果_%s.xlsx" % datetime.date.today())
        if not path:
            return
        rows = []
        for it in self.items:
            rows.append({
                "source": it["source"], "page": it["page"], "code": it["code"], "name": it["name"],
                "machine": it["machine"], "kind": KIND_LABEL.get(it["kind"], it["kind"]),
                "upload_sel": it.get("upload_sel", True),
                "target": it.get("target", ""), "status": it["status"],
                "caxa_path": it.get("caxa_path", ""), "note": it.get("note", ""),
            })
        try:
            borrow_rows = []
            if getattr(self, "uploader", None) is not None:
                borrow_rows = self.uploader.borrow_actions or []
            excel_out.export_excel(rows, path, borrow_rows=borrow_rows)
            self.log("Excel 已导出：%s" % path)
        except Exception as ex:
            messagebox.showerror("导出失败", str(ex))

    # ---------- 设置 ----------
    def open_settings(self):
        dlg = tk.Toplevel(self)
        dlg.title("设置")
        dlg.transient(self)
        dlg.grab_set()
        form = ttk.Frame(dlg, padding=12)
        form.pack()
        entries = {}
        fields = []
        if self.mode != "recognize":
            fields.extend([
                ("caxa_server_ip", "CAXA服务器IP"),
                ("caxa_server_port", "端口"),
                ("caxa_user", "CAXA用户名"),
                ("caxa_password_enc", "CAXA密码（留空则不修改）"),
            ])
        if self.mode != "upload":
            fields.extend([
                ("vision_server", "本地视觉服务地址"),
                ("timeout_seconds", "补录超时秒数"),
            ])
        for i, (key, label) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            if key == "caxa_password_enc":
                var = tk.StringVar(value="")
                ent = ttk.Entry(form, textvariable=var, show="*", width=46)
            else:
                var = tk.StringVar(value=str(self.cfg.get(key, "")))
                ent = ttk.Entry(form, textvariable=var, width=46)
            ent.grid(row=i, column=1, pady=3)
            entries[key] = var
        dup_var = tk.BooleanVar(value=bool(self.cfg.get("dup_new_version")))
        if self.mode == "all":
            ttk.Checkbutton(form, text="重复图号新增版本（检出→覆盖→检入，仅未发布文档）",
                            variable=dup_var).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=2)
        borrow_var = tk.BooleanVar(value=bool(self.cfg.get("borrow_only_missing", True)))
        if self.mode == "all":
            ttk.Checkbutton(form, text="上传后按 Z03 总装图明细表借用未收录组件（只借用不新建）",
                            variable=borrow_var).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=2)
        test_var = tk.StringVar(value="")

        def test_vision():
            try:
                from pipeline import vision

                vv = vision.LocalVision(self.cfg.get("vision_server", ""),
                                        self.cfg.get("vision_script", ""))
                if vv.healthy(timeout=8):
                    test_var.set("视觉服务可连接")
                else:
                    test_var.set("连接失败：请检查地址，中央机需已启动并放行防火墙")
            except Exception as ex:
                test_var.set("连接失败：%s" % ex)

        if self.mode != "upload":
            ttk.Button(form, text="测试视觉服务", command=test_vision).grid(
                row=len(fields) + 2, column=0, sticky="w", pady=2)
            ttk.Label(form, textvariable=test_var).grid(
                row=len(fields) + 2, column=1, sticky="w", pady=2)

        def save():
            if "caxa_server_ip" in entries:
                self.cfg["caxa_server_ip"] = entries["caxa_server_ip"].get().strip()
                self.cfg["caxa_server_port"] = entries["caxa_server_port"].get().strip()
                self.cfg["caxa_user"] = entries["caxa_user"].get().strip()
                pwd = entries["caxa_password_enc"].get()
                if pwd:
                    self.cfg["caxa_password_enc"] = config._protect(pwd)
            if "vision_server" in entries:
                self.cfg["vision_server"] = entries["vision_server"].get().strip()
                try:
                    self.cfg["timeout_seconds"] = int(entries["timeout_seconds"].get())
                except Exception:
                    pass
            if self.mode == "all":
                self.cfg["dup_new_version"] = bool(dup_var.get())
                self.cfg["borrow_only_missing"] = bool(borrow_var.get())
            config.save(self.cfg)
            dlg.destroy()
            self.log("设置已保存")

        ttk.Button(form, text="保存", command=save).grid(
            row=len(fields) + 3, column=0, columnspan=2, pady=10)
