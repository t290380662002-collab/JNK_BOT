# -*- coding: utf-8 -*-
"""
解析文字訂房訊息 -> booking dict（供 template_filler.fill_manual 使用）。

支援格式（繁/簡體關鍵字，大小寫不拘）：
    入住：7/21          入住日期 2026-07-21
    退房：7/23
    飯店：倫敦人        酒店：威尼斯
    房型：維多利亞房大床
    件數：1            房數：2
    人數：3
    是否吸煙：抽煙      禁煙
    姓名：江-泰哥-呂布
    微信：泰哥服務群

客人姓名：非 key-value 且含中文字的獨立行，或多個以 、/， 分隔。
年份預設為今年。
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

# 用來辨識 key-value 行的標籤關鍵字
KV_LABELS = ["飯店", "酒店", "入住", "退房", "房型", "件數", "房數", "人數",
             "微信", "姓名", "名", "客人", "吸煙", "抽煙", "是否吸煙", "煙"]


def _resolve_hotel(name):
    name = (name or "").strip().lower()
    if not name:
        return None
    for alias, key in HOTEL_ALIASES.items():
        if alias in name:
            return key
    return None


def _parse_date(s):
    """'7/21' / '2026-07-21' / '7月21日' -> 'YYYY-MM-DD'；失敗回 None。"""
    if not s:
        return None
    s = s.strip()
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})[月/-](\d{1,2})[日]?", s)
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


def _add_guests(booking, value):
    for p in re.split(r"[、，,]", value):
        p = p.strip().lstrip("-").strip()
        if p and re.search(r"[\u4e00-\u9fff]", p):
            booking["guests"].append(p)


def _apply_kv(booking, label, value):
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
        _add_guests(booking, value)
    elif label in ("吸煙", "抽煙", "煙"):
        booking["smoking"] = _parse_smoking(value) if value else _parse_smoking(label)


def parse(text: str) -> dict:
    """解析文字訂房 -> booking dict。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    booking = {
        "guests": [],
        "en_names": [],
        "check_in": None,
        "check_out": None,
        "room_type": None,
        "room_count": None,
        "pax": None,
        "smoking": None,
        "wechat": None,
        "hotel": None,
        "raw": text,
    }
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
                _apply_kv(booking, matched, value)
                continue
        # 非 key-value：含中文的獨立行視為客人姓名
        if re.search(r"[\u4e00-\u9fff]", ln):
            _add_guests(booking, ln)

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
微信：泰哥服務群"""
    import json
    print(json.dumps(parse(sample), ensure_ascii=False, indent=2))
