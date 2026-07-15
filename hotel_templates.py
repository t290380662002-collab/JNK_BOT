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
}

HOTELS = {
    "mingqui": {
        "name": "名匯 (Londoner Grand)",
        "file": "mingqui.xlsx",
        "form_sheet": "Londoner Grand",
        "list_sheet": "Sheet1",
        "list_header_row": 8,
        "list_start_row": 9,
        "list_cols": {"zh": "G", "en": "H", "doc": "I", "dob": "J", "room": "K"},
        "list_smoking_col": "L",  # 僅名匯清單表有吸煙欄
    },
    "venetian": {
        "name": "威尼斯 (Venetian)",
        "file": "venetian.xlsx",
        "form_sheet": "Venetian",
        "list_sheet": "工作表1",
        "list_header_row": 2,
        "list_start_row": 3,
        "list_cols": {"zh": "C", "en": "D", "doc": "E", "dob": "F", "room": "G"},
        "list_smoking_col": None,
    },
    "parisian": {
        "name": "巴黎人 (Parisian)",
        "file": "parisian.xlsx",
        "form_sheet": "Parisian",
        "list_sheet": "Sheet1",
        "list_header_row": 1,
        "list_start_row": 2,
        # 注意：巴黎人 出生日期(C) 與 證件號碼(D) 順序與其他酒店相反
        "list_cols": {"zh": "A", "en": "B", "dob": "C", "doc": "D", "room": "E"},
        "list_smoking_col": None,
    },
    "londoner": {
        "name": "倫敦人 (Londoner)",
        "file": "londoner.xlsx",
        "form_sheet": "Londoner",
        "list_sheet": "工作表1",
        "list_header_row": 2,
        "list_start_row": 3,
        "list_cols": {"zh": "B", "en": "C", "doc": "D", "dob": "E", "room": "F"},
        "list_smoking_col": None,
    },
    "yuyuan": {
        "name": "御園 (Londoner Court)",
        "file": "yuyuan.xlsx",
        "form_sheet": "Londoner Court",
        "list_sheet": "Sheet1",
        "list_header_row": 1,
        "list_start_row": 3,  # 第2列空白，資料從第3列起
        # 御園清單表無「房型」欄
        "list_cols": {"zh": "A", "en": "B", "doc": "C", "dob": "D"},
        "list_smoking_col": None,
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
