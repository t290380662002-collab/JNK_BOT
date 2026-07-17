# -*- coding: utf-8 -*-
"""
把客人資料填入六間酒店的訂房單模板。

對外主函式：
    fill(hotel_key, records)         # records: 掃描記錄清單（photo OCR）
    fill_manual(hotel_key, booking)  # booking: 文字訂房 dict（/book 指令）

record（掃描）結構：
    {"doc_type":..., "parsed": {last_name, first_name, doc_number, date_of_birth, ...}}
booking（文字訂房）結構：
    {
      "guests": ["中文姓名", ...],
      "en_names": ["ENG,NAME", ...] (optional, 與 guests 對齊),
      "check_in": "2026-07-21", "check_out": "2026-07-23",
      "room_count": 1, "pax": 1,
      "smoking": True / False / None,
      "doc_numbers": [...], "dobs": [...] (optional, 與 guests 對齊),
    }

行為（依用戶確認）：
  · 有清單表的酒店：清單表填全部客人（掃描時中文姓名留空手動補；文字訂房則填入中文姓名）
  · 每位客人各產生一張訂房單主表格（label-driven 填入）
  · 房型：依用戶指示「無須填入」，均留空手動補
  · 吸菸：True/False 會填入訂房單主表格「特別要求 Special request」欄（吸菸/禁煙）
"""
import os
import re
import tempfile
from datetime import datetime, date

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


def _date_value(s):
    """'YYYY-MM-DD' / 'MM-DD' -> datetime（Excel 會格式化）；失敗回原字串。"""
    if not s or not str(s).strip():
        return ""
    s = str(s).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == "%m-%d":
                d = d.replace(year=datetime.now().year)
            return d
        except ValueError:
            continue
    return s


def _doc_value(parsed: dict) -> str:
    return (parsed.get("doc_number") or "").strip().upper()


def _split_name(zh: str):
    """中文姓名拆 姓/名：有 '-' 取第一段為姓，否則取首字為姓。"""
    zh = (zh or "").strip()
    if not zh:
        return "", ""
    if "-" in zh:
        parts = [p for p in zh.split("-") if p]
        if parts:
            return parts[0], "-".join(parts[1:])
    return zh[0], zh[1:]


def _split_en_name(en: str):
    """英文姓名拆 姓/名：'WU，JUN' -> ('WU','JUN')；'STEPHENS,JOHN A' -> ('STEPHENS','JOHN A')。"""
    en = (en or "").replace("，", ",").replace("、", ",").strip()
    parts = [p.strip() for p in re.split(r"[,/\s]+", en) if p.strip()]
    if not parts:
        return "", ""
    return parts[0], (" ".join(parts[1:]) if len(parts) > 1 else "")


# ---------------------------------------------------------------------------
# 標準化：record / booking -> 內部 booking dict
# ---------------------------------------------------------------------------
def _normalize_record(rec: dict) -> dict:
    b = {k: "" for k in ("surname", "firstname", "docnum", "dob",
                          "checkin", "checkout", "pax", "rooms",
                          "zh", "en", "room", "smoking")}
    parsed = rec.get("parsed")
    if parsed:
        b["surname"] = (parsed.get("last_name") or "").strip().upper()
        b["firstname"] = (parsed.get("first_name") or "").strip().upper()
        b["docnum"] = _doc_value(parsed)
        b["dob"] = _date_value(parsed.get("date_of_birth"))
        b["en"] = _en_name(parsed)
        zh = (parsed.get("zh_name") or "").strip()
        en = (parsed.get("en_name") or "").strip()
        if zh:
            b["zh"] = zh
        if en:
            b["en"] = en.upper()
            s, f = _split_en_name(en)
            b["surname"], b["firstname"] = s.upper(), f.upper()
        elif zh:
            # 無英文時用中文拆 姓/名
            s, f = _split_name(zh)
            b["surname"], b["firstname"] = s, f
        # 掃描無中文姓名/房型 -> 留空手動補
    manual = rec.get("manual") or rec.get("booking")
    if manual:
        en = (manual.get("en_name") or "").strip()
        zh = (manual.get("zh_name") or "").strip()
        if en:
            b["en"] = en.upper()
            s, f = _split_en_name(en)
            b["surname"], b["firstname"] = s.upper(), f.upper()
        if zh:
            b["zh"] = zh
            if not en:                      # 無英文時才用中文拆 姓/名
                s, f = _split_name(zh)
                b["surname"], b["firstname"] = s, f
        if manual.get("doc_number"):
            b["docnum"] = str(manual["doc_number"]).strip().upper()
        if manual.get("dob"):
            b["dob"] = _date_value(manual["dob"])
        if manual.get("check_in"):
            b["checkin"] = _date_value(manual["check_in"])
        if manual.get("check_out"):
            b["checkout"] = _date_value(manual["check_out"])
        if manual.get("room_count"):
            b["rooms"] = manual["room_count"]
        if manual.get("pax"):
            b["pax"] = manual["pax"]
        if manual.get("smoking") is not None:
            b["smoking"] = manual["smoking"]
        # 房型依用戶指示不填 -> room 留空
    return b


def _bookings_from_manual(booking: dict) -> list:
    """文字訂房 dict -> 標準化 booking 清單（每位客人一筆，共用日期/房數/吸煙）。

    booking["guests"] 為 dict 清單，每筆含 zh_name/en_name/doc_number/dob（部分可空）。
    """
    guests = booking.get("guests") or []
    out = []
    for g in guests:
        if isinstance(g, str):
            g = {"zh_name": g}
        m = {
            "zh_name": g.get("zh_name"),
            "en_name": g.get("en_name"),
            "doc_number": g.get("doc_number"),
            "dob": g.get("dob"),
            "check_in": booking.get("check_in"),
            "check_out": booking.get("check_out"),
            "room_count": booking.get("room_count"),
            "pax": booking.get("pax"),
            "smoking": booking.get("smoking"),
        }
        out.append(_normalize_record({"manual": m}))
    return out


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


def _find_date_label(ws):
    """專找獨立的 'Date:' 標籤（避開 C/I Date、C/O Date）。"""
    for row in ws.iter_rows():
        for c in row:
            if c.value and isinstance(c.value, str):
                v = c.value.strip()
                if v.startswith("Date:") and not re.search(r"C\s*[/_]\s*[IO]|入住|退房", v, re.I):
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


_DOB_FMT = "yyyy/mm/dd"      # 出生日期固定顯示（補零，不隨區域變動）
_CHECK_FMT = "yyyy/mm/dd"    # 入住/退房日期：用戶確認為 2026/07/29 格式


def _fill_form_sheet(ws, b: dict):
    """label-driven 填一張訂房單主表格（只填有值的欄，避免清掉模板）。"""
    # 吸菸 -> 特別要求欄；True=吸菸，False=禁煙，None=不填
    special = ""
    if b.get("smoking") is True:
        special = "吸菸"
    elif b.get("smoking") is False:
        special = "禁煙"
    values = {
        "surname": b.get("surname", ""),
        "firstname": b.get("firstname", ""),
        "docnum": b.get("docnum", ""),
        "dob": b.get("dob", ""),
        "checkin": b.get("checkin", ""),
        "checkout": b.get("checkout", ""),
        "pax": b.get("pax", ""),
        "rooms": b.get("rooms", ""),
        "special_request": special,
        "date": datetime.now().date(),
    }
    for field, subs in HT.FORM_LABELS.items():
        if field == "date":
            # Date: 欄位需避免誤匹配 C/I Date / C/O Date，改由專用搜尋
            lc = _find_date_label(ws)
        else:
            lc = _find_label(ws, subs)
        if lc is None:
            continue
        v = values.get(field, "")
        if v == "" or v is None:
            continue
        cell = _input_cell(ws, lc)
        cell.value = v
        if isinstance(v, (datetime, date)):
            cell.number_format = _CHECK_FMT if field in ("checkin", "checkout") else _DOB_FMT


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
            mr = _merged_range_of(ws, r, c)
            if mr and (r, c) != (mr.min_row, mr.min_col):
                continue
            cell.value = None


def _nights(ci, co):
    """晚數 = 退房 - 入住（天）。兩者皆為日期才計算。"""
    if isinstance(ci, datetime) and isinstance(co, datetime):
        d = (co - ci).days
        return d if d >= 0 else None
    return None


def _fill_list_sheet(ws, cfg: dict, bookings: list):
    """填工作表1（10 欄訂房摘要表）：每位客人一列。

    自動填：入住人 / 人數 / 入住日期 / 退房 / 晚數。
    留空白手動補：群組 / 股東 / 代理 / 抵澳時間 / 離澳時間。
    """
    _clear_list_region(ws, cfg)
    start = cfg["list_start_row"]
    cols = cfg["list_cols"]
    for i, b in enumerate(bookings):
        row = start + i
        # 入住人：中文姓名優先，否則英文
        guest = b.get("zh") or b.get("en") or ""
        if guest:
            ws.cell(row=row, column=_col_to_idx(cols["guest"]), value=guest)
        # 人數（整筆訂房共用）
        pax = b.get("pax")
        if pax not in ("", None):
            ws.cell(row=row, column=_col_to_idx(cols["pax"]), value=pax)
        # 入住日期
        ci = b.get("checkin")
        if ci not in ("", None):
            c = ws.cell(row=row, column=_col_to_idx(cols["checkin"]), value=ci)
            if isinstance(ci, datetime):
                c.number_format = _CHECK_FMT
        # 退房
        co = b.get("checkout")
        if co not in ("", None):
            c = ws.cell(row=row, column=_col_to_idx(cols["checkout"]), value=co)
            if isinstance(co, datetime):
                c.number_format = _CHECK_FMT
        # 晚數
        n = _nights(ci, co)
        if n is not None:
            ws.cell(row=row, column=_col_to_idx(cols["nights"]), value=n)
        # 群組/股東/代理/抵澳時間/離澳時間 -> 留空白手動補（使用者填）


# ---------------------------------------------------------------------------
# 主函式
# ---------------------------------------------------------------------------
def _render(cfg: dict, bookings: list) -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(base_dir, "templates", cfg["file"])
    wb = openpyxl.load_workbook(template_path)

    # 1) 清單表（若有）填全部客人
    if cfg.get("list_sheet") and cfg["list_sheet"] in wb.sheetnames:
        _fill_list_sheet(wb[cfg["list_sheet"]], cfg, bookings)

    # 2) 每位客人各一張訂房單主表格
    form_name = cfg["form_sheet"]
    src_form = wb[form_name]
    if bookings:
        _fill_form_sheet(src_form, bookings[0])
        src_form.title = _safe_sheet_title(form_name, 1, bookings[0].get("surname", ""))
        for idx, b in enumerate(bookings[1:], start=2):
            new_ws = wb.copy_worksheet(src_form)
            _clear_form_values(new_ws)
            _fill_form_sheet(new_ws, b)
            new_ws.title = _safe_sheet_title(form_name, idx, b.get("surname", ""))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"{cfg.get('key', 'booking')}_{ts}.xlsx"
    out_path = os.path.join(tempfile.gettempdir(), out_name)
    wb.save(out_path)
    return out_path


def fill(hotel_key: str, records: list) -> str:
    """掃描記錄 -> Excel。"""
    if hotel_key not in HT.HOTELS:
        raise ValueError(f"未知酒店代碼：{hotel_key}")
    cfg = dict(HT.HOTELS[hotel_key])
    cfg["key"] = hotel_key
    bookings = [_normalize_record(r) for r in records]
    return _render(cfg, bookings)


def fill_manual(hotel_key: str, booking: dict) -> str:
    """文字訂房 -> Excel。"""
    if hotel_key not in HT.HOTELS:
        raise ValueError(f"未知酒店代碼：{hotel_key}")
    cfg = dict(HT.HOTELS[hotel_key])
    cfg["key"] = hotel_key
    bookings = _bookings_from_manual(booking)
    if not bookings:
        raise ValueError("訂房無客人資料")
    return _render(cfg, bookings)


def _clear_form_values(ws):
    """清掉複製表中已填的所有主表格欄位值。"""
    for subs in HT.FORM_LABELS.values():
        lc = _find_label(ws, subs)
        if lc is not None:
            _input_cell(ws, lc).value = None


if __name__ == "__main__":
    # 簡易自測：掃描假資料填六酒店
    sample_records = [
        {
            "doc_type": "護照",
            "parsed": {
                "last_name": "STEPHENS", "first_name": "JOHN",
                "doc_number": "GBN123456", "date_of_birth": "1985-01-01",
            },
        },
        {
            "doc_type": "護照",
            "parsed": {
                "last_name": "CHAN", "first_name": "TAI MAN",
                "doc_number": "K12345678", "date_of_birth": "1990-06-15",
            },
        },
    ]
    for hk in HT.HOTEL_ORDER:
        p = fill(hk, sample_records)
        print(hk, "->", p, "sheets:", openpyxl.load_workbook(p).sheetnames)

    # 文字訂房自測
    booking = {
        "guests": ["江-泰哥-呂布"],
        "check_in": "2026-07-21", "check_out": "2026-07-23",
        "room_count": 1, "pax": 1, "smoking": True,
    }
    p = fill_manual("londoner", booking)
    wb = openpyxl.load_workbook(p)
    ws = wb["Londoner-1-江"]
    print("文字訂房 倫敦人：", "D16=", ws["D16"].value, "K16=", ws["K16"].value,
          "D20=", ws["D20"].value, "D21=", ws["D21"].value,
          "O20=", ws["O20"].value, "O21=", ws["O21"].value)
