# -*- coding: utf-8 -*-
"""
解析文字訂房訊息 -> booking dict（供 template_filler.fill_manual 使用）。

支援格式（繁/簡體關鍵字，大小寫不拘）：
    入住：7/21
    退房：7/23
    飯店：倫敦人        酒店：威尼斯
    房型：維多利亞房大床
    件數：1            房數：2
    人數：3
    是否吸煙：抽煙      禁煙
    姓名：江-泰哥-呂布
    微信：泰哥服務群
    # 讀取證件完畢回傳的文字（護照欄位）——
    入住者中文：吴俊
    入住者英文：WU，JUN
    出生年月日：1981.07.08
    證件號碼：CJ9314108

解析結果：
    booking["guests"]  : 入住者清單（每位一 dict：zh_name/en_name/doc_number/dob）
    booking["booker"]  : 訂房人（非入住者、獨立姓名行，如「江-泰哥-呂布」）
    booking["check_in/out"], room_count, pax, smoking, wechat, hotel, room_type

年份預設今年；客人獨立行可多個以 、/， 分隔。
"""
import re
from datetime import datetime

HOTEL_ALIASES = {
    "倫敦人": "londoner", "伦敦人": "londoner", "londoner": "londoner",
    "威尼斯": "venetian", "威尼斯人": "venetian", "venetian": "venetian",
    "巴黎人": "parisian", "巴黎": "parisian", "parisian": "parisian",
    "名匯": "mingqui", "名汇": "mingqui", "mingqui": "mingqui",
    "御園": "yuyuan", "御园": "yuyuan", "yuyuan": "yuyuan",
    "康萊德": "conrad", "康莱德": "conrad", "conrad": "conrad",
}

# key-value 標籤（順序：越具體越前，避免被短關鍵字搶先匹配，
# 例如 "入住" 是 "入住者中文" 的前綴，必須讓具體標籤排前面）
KV_LABELS = [
    "入住者中文", "入住者英文", "出生年月日", "出生日期",
    "證件號碼", "證件",
    "飯店", "酒店", "入住", "退房", "房型", "件數", "房數", "人數",
    "微信", "姓名", "名", "客人",
    "是否吸煙", "吸煙", "抽煙", "煙",
]

# 獨立行過濾：含這些字視為「指令/狀態」而非姓名（如「讀取證件完畢 回傳文字」）
IGNORE_STANDALONE = ["讀取", "回傳", "完畢", "掃描", "證件", "完"]


def _resolve_hotel(name):
    name = (name or "").strip().lower()
    if not name:
        return None
    for alias, key in HOTEL_ALIASES.items():
        if alias in name:
            return key
    return None


def _parse_date(s):
    """'7/21' / '2026-07-21' / '1981.07.08' / '7月21日' -> 'YYYY-MM-DD'；失敗回 None。"""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})[月/\-.](\d{1,2})", s)
    if m:
        y = datetime.now().year
        return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _parse_int(s):
    if not s:
        return None
    m = re.search(r"\d+", str(s))
    return int(m.group(0)) if m else None


def _parse_smoking(s):
    """回傳 True / False / None。"""
    if not s:
        return None
    s = s.strip()
    if any(k in s for k in ("禁煙", "不抽", "無煙", "无烟", "不吸")):
        return False
    if any(k in s for k in ("抽煙", "吸煙", "吸咽", "抽", "煙", "烟")):
        return True
    return None


def _first_token(s):
    """取字串首個連續英數字片段（如證件號碼：'CJ9314108 讀取...' -> 'CJ9314108'）。"""
    m = re.search(r"[A-Za-z0-9]+", str(s))
    return m.group(0) if m else ""


def _apply_kv(booking, primary, label, value):
    if label in ("飯店", "酒店"):
        booking["hotel"] = _resolve_hotel(value)
    elif label == "入住":
        booking["check_in"] = _parse_date(value)
    elif label == "退房":
        booking["check_out"] = _parse_date(value)
    elif label == "房型":
        booking["room_type"] = value
    elif label in ("件數", "房數"):
        booking["room_count"] = _parse_int(value)
    elif label == "人數":
        booking["pax"] = _parse_int(value)
    elif label == "微信":
        booking["wechat"] = value
    elif label in ("姓名", "名", "客人"):
        for p in re.split(r"[、，,]", value):
            p = p.strip().lstrip("-").strip()
            if p and re.search(r"[\u4e00-\u9fff]", p):
                booking["guests"].append({"zh_name": p})
    elif label in ("吸煙", "抽煙", "煙", "是否吸煙"):
        booking["smoking"] = _parse_smoking(value) if value else _parse_smoking(label)
    elif label == "入住者中文":
        primary["zh_name"] = value
    elif label == "入住者英文":
        primary["en_name"] = value
    elif label in ("出生年月日", "出生日期"):
        primary["dob"] = _parse_date(value)
    elif label in ("證件號碼", "證件"):
        primary["doc_number"] = _first_token(value)


def parse(text: str) -> dict:
    """解析文字訂房 -> booking dict。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    booking = {
        "guests": [],
        "booker": None,
        "check_in": None, "check_out": None,
        "room_type": None, "room_count": None, "pax": None,
        "smoking": None, "wechat": None, "hotel": None,
        "raw": text,
    }
    primary = {}          # 入住者* 累積
    has_primary = False
    standalone = []       # 非 KV 且含中文的獨立行（可能為訂房人 / 客人）

    for ln in lines:
        m = re.match(r"^(.*?)[：:]\s*(.*)$", ln)
        if m:
            label = m.group(1).strip()
            value = m.group(2).strip()
            matched = None
            for kw in KV_LABELS:
                if kw in label:
                    matched = kw
                    break
            if matched:
                _apply_kv(booking, primary, matched, value)
                if matched in ("入住者中文", "入住者英文", "出生年月日", "出生日期",
                               "證件號碼", "證件"):
                    has_primary = True
                continue
        # 非 KV：含中文的獨立行
        if re.search(r"[\u4e00-\u9fff]", ln) and not any(w in ln for w in IGNORE_STANDALONE):
            standalone.append(ln)

    # 入住者優先成為客人；其餘獨立行視為訂房人（如「江-泰哥-呂布」）
    if primary:
        booking["guests"].append(primary)
    elif standalone:
        for s in standalone:
            booking["guests"].append({"zh_name": s})
    if standalone and primary:
        booking["booker"] = "、".join(standalone)

    if booking["pax"] is None and booking["guests"]:
        booking["pax"] = len(booking["guests"])
    if booking["room_count"] is None:
        booking["room_count"] = 1
    return booking


if __name__ == "__main__":
    sample = """入住： 7/21
退房： 7/23
飯店： 倫敦人
房型： 維多利亞房大床
件數： 1
是否吸煙：抽煙

江-泰哥-呂布
微信：泰哥服務群

入住者中文：吴俊
入住者英文：WU，JUN
出生年月日：1981.07.08
證件號碼：CJ9314108 讀取證件完畢 回傳文字"""
    import json
    print(json.dumps(parse(sample), ensure_ascii=False, indent=2))
