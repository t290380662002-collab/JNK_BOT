# -*- coding: utf-8 -*-
"""
把掃描到的客人資料填入六間酒店的訂房單模板。

對外主函式：
    fill(hotel_key, records) -> 產出檔案路徑

records 為 bot session 的記錄清單，每筆結構：
    {
      "doc_type": "護照" | "港澳通行證" | ...,
      "scan_time": "...",
      "parsed": {last_name, first_name, doc_number, date_of_birth, sex, ...}
    }

行為（依用戶確認）：
  · 有清單表的酒店：清單表填全部客人（中文姓名、房型留空手動補）
  · 每位客人各產生一張訂房單主表格（label-driven 填入）
  · 康萊德無清單表，只產生訂房單主表格
"""
import os
import re
import tempfile
from datetime import datetime

import openpyxl

import hotel_templates as HT


# ---------------------------------------------------------------------------
# 值格式化
# ---------------------------------------------------------------------------
def _en_name(parsed: dict) -> str:
    """英文姓名 -> 'LASTNAME,FIRSTNAME'（比照範例格式）。"""
    last = (parsed.get("last_name") or "").strip().upper()
    first = (parsed.get("first_name") or "").strip().upper()
    if last and first:
        return f"{last},{first}"
    return last or first


def _dob_value(parsed: dict):
    """出生日期 'YYYY-MM-DD' -> datetime（Excel 會格式化）；失敗則回原字串。"""
    s = (parsed.get("date_of_birth") or "").strip()
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return s


def _doc_value(parsed: dict) -> str:
    return (parsed.get("doc_number") or "").strip().upper()


# ---------------------------------------------------------------------------
# label-driven：找標籤右側輸入格
# ---------------------------------------------------------------------------
def _norm(s) -> str:
    return str(s).replace(" ", "").replace("\u3000", "").replace("\n", "")


def _merged_range_of(ws, row, col):
    for mr in ws.merged_cells.ranges:
        if mr.min_row <= row <= mr.max_row and mr.min_col <= col <= mr.max_col:
            return mr
    return None


def _find_label(ws, subs):
    for row in ws.iter_rows():
        for c in row:
            if c.value and isinstance(c.value, str):
                v = _norm(c.value)
                for sub in subs:
                    if _norm(sub) in v:
                        return c
    return None


def _input_cell(ws, label_cell):
    """回傳標籤右側輸入格（若在合併範圍則取左上角）。"""
    mr = _merged_range_of(ws, label_cell.row, label_cell.column)
    if mr:
        trow, tcol = mr.min_row, mr.max_col + 1
    else:
        trow, tcol = label_cell.row, label_cell.column + 1
    tmr = _merged_range_of(ws, trow, tcol)
    if tmr:
        trow, tcol = tmr.min_row, tmr.min_col
    return ws.cell(row=trow, column=tcol)


def _fill_form_sheet(ws, parsed: dict):
    """label-driven 填一張訂房單主表格。"""
    values = {
        "surname": (parsed.get("last_name") or "").strip().upper(),
        "firstname": (parsed.get("first_name") or "").strip().upper(),
        "docnum": _doc_value(parsed),
        "dob": _dob_value(parsed),
    }
    for field, subs in HT.FORM_LABELS.items():
        lc = _find_label(ws, subs)
        if lc is None:
            continue
        cell = _input_cell(ws, lc)
        cell.value = values.get(field, "")


def _safe_sheet_title(base: str, idx: int, surname: str) -> str:
    """產生合法且不重複的 sheet 名（<=31 字、去除非法字元）。"""
    surname = re.sub(r"[\[\]\:\*\?\/\\]", "", surname or "")[:12]
    title = f"{base}-{idx}"
    if surname:
        title = f"{title}-{surname}"
    return title[:31]


# ---------------------------------------------------------------------------
# 清單表填入
# ---------------------------------------------------------------------------
def _col_to_idx(letter: str) -> int:
    return openpyxl.utils.column_index_from_string(letter)


def _clear_list_region(ws, cfg: dict, rows: int = 60):
    """清掉清單表原有的範例資料（這些「空白檔」其實內含範例列）。
    清除範圍涵蓋所有映射欄位並外擴 3 欄（涵蓋吸煙等附加欄），
    自資料起始列往下 rows 列。"""
    cols = cfg["list_cols"]
    idxs = [_col_to_idx(v) for v in cols.values()]
    cmin, cmax = min(idxs), max(idxs) + 3
    start = cfg["list_start_row"]
    for r in range(start, start + rows):
        for c in range(cmin, cmax + 1):
            cell = ws.cell(row=r, column=c)
            # 跳過合併儲存格的非左上角（唯讀）
            mr = _merged_range_of(ws, r, c)
            if mr and (r, c) != (mr.min_row, mr.min_col):
                continue
            cell.value = None


def _fill_list_sheet(ws, cfg: dict, records: list):
    _clear_list_region(ws, cfg)
    start = cfg["list_start_row"]
    cols = cfg["list_cols"]
    for i, rec in enumerate(records):
        parsed = rec.get("parsed", {})
        row = start + i
        if "en" in cols:
            ws.cell(row=row, column=_col_to_idx(cols["en"]), value=_en_name(parsed))
        if "doc" in cols:
            ws.cell(row=row, column=_col_to_idx(cols["doc"]), value=_doc_value(parsed))
        if "dob" in cols:
            ws.cell(row=row, column=_col_to_idx(cols["dob"]), value=_dob_value(parsed))
        # zh（中文姓名）與 room（房型）依用戶要求留空，手動補


# ---------------------------------------------------------------------------
# 主函式
# ---------------------------------------------------------------------------
def fill(hotel_key: str, records: list) -> str:
    if hotel_key not in HT.HOTELS:
        raise ValueError(f"未知酒店代碼：{hotel_key}")
    cfg = HT.HOTELS[hotel_key]

    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(base_dir, "templates", cfg["file"])
    wb = openpyxl.load_workbook(template_path)

    # 1) 清單表（若有）填全部客人
    if cfg.get("list_sheet") and cfg["list_sheet"] in wb.sheetnames:
        _fill_list_sheet(wb[cfg["list_sheet"]], cfg, records)

    # 2) 每位客人各一張訂房單主表格
    form_name = cfg["form_sheet"]
    src_form = wb[form_name]
    if records:
        # 第一位客人填在原始主表格
        _fill_form_sheet(src_form, records[0].get("parsed", {}))
        src_form.title = _safe_sheet_title(
            form_name, 1, records[0].get("parsed", {}).get("last_name", "")
        )
        # 其餘客人複製主表格再填
        for idx, rec in enumerate(records[1:], start=2):
            new_ws = wb.copy_worksheet(src_form)
            # 複製來的 sheet 需清掉第一位客人的值再填當前客人
            _clear_form_values(new_ws)
            _fill_form_sheet(new_ws, rec.get("parsed", {}))
            new_ws.title = _safe_sheet_title(
                form_name, idx, rec.get("parsed", {}).get("last_name", "")
            )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{hotel_key}_{ts}.xlsx"
    out_path = os.path.join(tempfile.gettempdir(), out_name)
    wb.save(out_path)
    return out_path


def _clear_form_values(ws):
    """清掉複製表中已填的 surname/firstname/docnum/dob 值。"""
    for subs in HT.FORM_LABELS.values():
        lc = _find_label(ws, subs)
        if lc is not None:
            _input_cell(ws, lc).value = None


if __name__ == "__main__":
    # 簡易自測：假資料填六酒店
    sample_records = [
        {
            "doc_type": "護照",
            "scan_time": "2026-07-16 06:00:00",
            "parsed": {
                "last_name": "STEPHENS", "first_name": "JOHN",
                "doc_number": "GBN123456", "date_of_birth": "1985-01-01",
                "sex": "M", "nationality": "英國",
            },
        },
        {
            "doc_type": "護照",
            "scan_time": "2026-07-16 06:00:00",
            "parsed": {
                "last_name": "CHAN", "first_name": "TAI MAN",
                "doc_number": "K12345678", "date_of_birth": "1990-06-15",
                "sex": "M", "nationality": "香港",
            },
        },
    ]
    for hk in HT.HOTEL_ORDER:
        p = fill(hk, sample_records)
        print(hk, "->", p, "sheets:", openpyxl.load_workbook(p).sheetnames)
