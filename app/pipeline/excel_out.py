import os

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


def export_excel(rows: list, out_path: str, borrow_rows: list = None):
    headers = [
        "来源PDF", "页", "图号", "名称", "成品代码(整机代码)", "类型", "上传选择", "上传目标", "状态", "CAXA路径", "备注",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "上传结果"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
    for r in rows:
        ws.append([
            r.get("source", ""), r.get("page", ""), r.get("code", ""), r.get("name", ""),
            r.get("machine", ""), r.get("kind", ""),
            "是" if r.get("upload_sel", True) else "否",
            r.get("target", ""), r.get("status", ""),
            r.get("caxa_path", ""), r.get("note", ""),
        ])
    if borrow_rows:
        ws2 = wb.create_sheet("明细表借用")
        ws2.append(["整机代码", "组件图号", "状态", "备注"])
        for c in ws2[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9E1F2")
        for r in borrow_rows:
            ws2.append([r.get("machine", ""), r.get("code", ""),
                        r.get("status", ""), r.get("note", "")])
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return out_path
