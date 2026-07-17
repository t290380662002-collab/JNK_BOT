# -*- coding: utf-8 -*-
"""
六間酒店訂房單模板設定（信威有限公司系統）。

每間酒店：
  key           內部代碼（callback 用，純 ASCII）
  name          顯示名稱（繁中）
  file          templates/ 下的模板檔名
  form_sheet    訂房單主表格 sheet 名（label-driven 填入，每位客人一張）
  list_sheet    客人清單資料表 sheet 名（None 表示無清單表，例：康萊德）
  list_header_row   清單標題所在列
  list_start_row    清單資料起始列
  list_cols     清單欄位 -> 欄字母映射
                （zh=中文姓名 en=英文姓名 doc=證件號碼 dob=出生日期 room=房型）
                缺欄位以 None 或不列出表示

訂房單主表格（六間一致，label-driven 已驗證）輸入格：
  姓Surname -> D16   名First Name -> K16   會員號碼 -> D17
  證件號碼 -> K17     出生日期DOB -> D18    入住C/I -> D20
  退房C/O -> D21      人數Pax -> O20        房數Rooms -> O21
"""

# 主表格標籤（label-driven）：欄位 -> 可能出現的標籤文字
# 注意 rooms 用 "No.of Rooms" 而非 "房數"，避免誤匹配 E13「房數 RM ONLY」套餐選項
FORM_LABELS = {
    "surname": ["姓Surname", "姓 Surname"],
    "firstname": ["名FirstName", "名 First Name"],
    "docnum": ["證件號碼"],
    "dob": ["出生日期DOB", "出生日期 DOB"],
    "checkin": ["入住日期", "C/I Date"],
    "checkout": ["退房日期", "C/O Date"],
    "pax": ["人數", "Pax"],
    "rooms": ["No.of Rooms", "Rooms("],
    "special_request": ["特別要求", "Special request"],
}

# 工作表1（清單表）統一為 10 欄「訂房摘要表」。
# 欄位順序固定：群組/股東/代理/入住人/人數/入住日期/抵澳時間/退房/離澳時間/晚數
# （A~J）。群組/股東/代理/抵澳時間/離澳時間 由使用者手動補；
# 入住人/人數/入住日期/退房/晚數 由 bot 自動填入。
SUMMARY_COLS = {
    "group": "A", "shareholder": "B", "agent": "C", "guest": "D",
    "pax": "E", "checkin": "F", "arrival": "G", "checkout": "H",
    "departure": "I", "nights": "J",
}

HOTELS = {
    "mingqui": {
        "name": "名匯 (Londoner Grand)",
        "file": "mingqui.xlsx",
        "form_sheet": "Londoner Grand",
        "list_sheet": "Sheet1",
        "list_header_row": 1,
        "list_start_row": 2,
        "list_cols": SUMMARY_COLS,
    },
    "venetian": {
        "name": "威尼斯 (Venetian)",
        "file": "venetian.xlsx",
        "form_sheet": "Venetian",
        "list_sheet": "工作表1",
        "list_header_row": 1,
        "list_start_row": 2,
        "list_cols": SUMMARY_COLS,
    },
    "parisian": {
        "name": "巴黎人 (Parisian)",
        "file": "parisian.xlsx",
        "form_sheet": "Parisian",
        "list_sheet": "Sheet1",
        "list_header_row": 1,
        "list_start_row": 2,
        "list_cols": SUMMARY_COLS,
    },
    "londoner": {
        "name": "倫敦人 (Londoner)",
        "file": "londoner.xlsx",
        "form_sheet": "Londoner",
        "list_sheet": "工作表1",
        "list_header_row": 1,
        "list_start_row": 2,
        "list_cols": SUMMARY_COLS,
    },
    "yuyuan": {
        "name": "御園 (Londoner Court)",
        "file": "yuyuan.xlsx",
        "form_sheet": "Londoner Court",
        "list_sheet": "Sheet1",
        "list_header_row": 1,
        "list_start_row": 2,
        "list_cols": SUMMARY_COLS,
    },
    "conrad": {
        "name": "康萊德 (Conrad)",
        "file": "conrad.xlsx",
        "form_sheet": "Conrad",
        "list_sheet": None,  # 康萊德無客人清單表，只填訂房單主表格
        "list_header_row": None,
        "list_start_row": None,
        "list_cols": None,
    },
}

# 顯示順序（inline 鍵盤用）
HOTEL_ORDER = ["mingqui", "venetian", "parisian", "londoner", "yuyuan", "conrad"]
