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
    是否吸煙：吸菸      禁煙
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

from timeutil import taipei_now

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
    "入住者中文", "入住者英文", "入住者名", "出生年月日", "出生日期",
    "證件號碼", "證件",
    "飯店", "酒店", "入住", "退房", "房型", "件數", "房數", "間數", "人數",
    "群組", "微信", "代理", "姓名", "名", "客人",
    "是否吸煙", "是否吸菸", "吸煙", "吸菸", "抽煙", "抽菸", "煙", "菸",
]

# 獨立行過濾：含這些字視為「指令/狀態」而非姓名（如「讀取證件完畢 回傳文字」）
IGNORE_STANDALONE = ["讀取", "回傳", "完畢", "掃描", "證件", "完"]

# 聊天記錄行（如 Telegram 匯出的「[2026/7/17 下午 04:44] 忠: 123」）直接略過，
# 不應被當成訂房資料或客人姓名。
_CHATLOG_RE = re.compile(r"^\s*\[\s*\d{4}[/-]\d{1,2}[/-]\d{1,2}")
# 指令提示關鍵字（bot 的 /command 與說明文字）
_CMD_HINT_RE = re.compile(r"(/book|/export|/list|/clear|/scan|/skip|/start|/help)")
# 值中混入的「用戶指示/聊天」文字起點（出現即截斷）
_NOISE_RE = re.compile(r"(\[|只讀取|無須讀取|普通聊天|正確格式|/book|/export|/list|/clear|/scan|/skip)")
# 聊天發言者行：冒號前不是已知關鍵字，且形如「發言者：訊息」-> 視為聊天整行略過。
# 例：「忠: 新增一些東西」「Jnk: 請傳送證件…」「客: 好的」「A: 收到」
_SPEAKER_RE = re.compile(r"^\s*[^：:]{1,12}\s*[：:]\s*\S")
# 獨立行若含這些聊天動詞/字，必非姓名，略過（避免把聊天句子當客人）
_CHAT_VERB_RE = re.compile(
    r"(新增|請|傳送|指令|提示|聊天|普通|格式|用|讓|幫|這|那|好|在|是|的|了|嗎|吧|啊|"
    r"東西|一些|什麼|什麽|今天|明天|昨天|收到|好的|好喔|謝|麻煩|麻烦)"
)


def _is_chatlog(line: str) -> bool:
    """判斷是否為聊天記錄行（以 [YYYY/M/D ...] 開頭）。"""
    return bool(_CHATLOG_RE.match(line or ""))


def _clean_kv_value(value: str) -> str:
    """截斷 KV 值中混入的聊天記錄 / 指令提示 / 用戶指示雜字，只留正確值。

    例：「泰哥服務群 只讀取正確格式 普通聊天打字 無須讀取提示 [2026/7/17...] 忠: 123」
        -> 「泰哥服務群」
    """
    if not value:
        return value
    m = _NOISE_RE.search(value)
    if m:
        value = value[:m.start()]
    return value.strip()


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
        y = taipei_now().year
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
    # 明確否定詞：否 / 無 / 不 / 沒有 -> 禁煙
    if s in ("否", "無", "无", "不", "沒有", "没有", "沒", "非"):
        return False
    if any(k in s for k in ("禁煙", "不抽", "無煙", "无烟", "不吸")):
        return False
    if any(k in s for k in ("抽煙", "吸菸", "吸煙", "吸咽", "抽", "煙", "烟", "菸")):
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
    elif label in ("件數", "房數", "間數"):
        booking["room_count"] = _parse_int(value)
    elif label == "人數":
        booking["pax"] = _parse_int(value)
    elif label == "微信":
        booking["wechat"] = value
    elif label == "群組":
        booking["group"] = value
    elif label == "代理":
        booking["agent"] = value
    elif label in ("姓名", "名", "客人"):
        for p in re.split(r"[、，,]", value):
            p = p.strip().lstrip("-").strip()
            if p and re.search(r"[\u4e00-\u9fff]", p):
                booking["guests"].append({"zh_name": p})
    elif label in ("吸煙", "吸菸", "抽煙", "抽菸", "煙", "菸", "是否吸煙", "是否吸菸"):
        booking["smoking"] = _parse_smoking(value) if value else _parse_smoking(label)
    elif label == "入住者中文":
        primary["zh_name"] = value
    elif label == "入住者英文":
        primary["en_name"] = value
    elif label == "入住者名":
        # 備註性文字（如「一間一位一本證件」），不納入任何欄位
        pass
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
        "agent": None,
        "group": None,
        "check_in": None, "check_out": None,
        "room_type": None, "room_count": None, "pax": None,
        "smoking": None, "wechat": None, "hotel": None,
        "raw": text,
    }
    primary = {}          # 入住者* 累積
    has_primary = False
    standalone = []       # 非 KV 且含中文的獨立行（可能為訂房人 / 客人）

    for ln in lines:
        # 聊天記錄行（[日期] 人名:）直接略過，不納入訂房解析
        if _is_chatlog(ln):
            continue
        m = re.match(r"^(.*?)[：:]\s*(.*)$", ln)
        if m:
            label = m.group(1).strip()
            value = _clean_kv_value(m.group(2).strip())
            # 選「最長」匹配的關鍵字：避免「入住者名」被較短的「入住」搶先誤判
            # 為入住日期（最長匹配才能讓「入住者名」>「入住」優先）。
            matched = None
            best_len = -1
            for kw in KV_LABELS:
                if kw in label and len(kw) > best_len:
                    matched = kw
                    best_len = len(kw)
            if matched:
                _apply_kv(booking, primary, matched, value)
                if matched in ("入住者中文", "入住者英文", "出生年月日", "出生日期",
                               "證件號碼", "證件"):
                    has_primary = True
                # 處理「江-泰哥-呂布 微信：泰哥服務群」同行：
                # keyword（微信/群組/代理）前的姓名視為訂房人，補回 standalone。
                if matched in ("微信", "群組", "代理") and label != matched:
                    prefix = label[:label.index(matched)].strip(" ：:").strip()
                    if prefix and re.search(r"[\u4e00-\u9fff]", prefix):
                        standalone.append(prefix)
                continue
            # 非關鍵字冒號行（如「忠: 新增一些東西」「Jnk: 請傳送證件…」）
            # 這是聊天軟體的「發言者：訊息」格式，整行略過，不當資料/姓名。
            if _SPEAKER_RE.match(ln):
                continue
        # 非 KV 獨立行：僅接受「像是姓名」的中文短行，其餘（聊天/指令）略過。
        # 判定為姓名：含中文、不含已知指令/聊天動詞/標點/英文、長度適中。
        if (re.search(r"[\u4e00-\u9fff]", ln)
                and not any(w in ln for w in IGNORE_STANDALONE)
                and not _CMD_HINT_RE.search(ln)
                and not _CHAT_VERB_RE.search(ln)
                and not re.search(r"[，。！？；、：:]", ln)
                and not re.search(r"[A-Za-z]", ln)
                and len(ln) <= 12):
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
    booking["_has_primary"] = has_primary
    return booking


def _passport_guest(parsed: dict) -> dict:
    """把掃描證件 parsed 轉成 guest dict（只含非空欄位）。
    生日歸一化為 YYYY-MM-DD（處理點號 / 中文年月日）。"""
    parsed = parsed or {}
    last = (parsed.get("last_name") or "").strip().upper()
    first = (parsed.get("first_name") or "").strip().upper()
    en = parsed.get("en_name")
    if not en and last:
        en = f"{last},{first}"
    zh = (parsed.get("zh_name") or "").strip()
    doc = (parsed.get("doc_number") or "").strip()
    dob = parsed.get("date_of_birth") or ""
    if dob:
        nd = _parse_date(dob)          # -> YYYY-MM-DD（支援 1982.01.09 / 1982年01月09日）
        if nd:
            dob = nd
    g = {}
    if zh:
        g["zh_name"] = zh
    if en:
        g["en_name"] = en
    if doc:
        g["doc_number"] = doc
    if dob:
        g["dob"] = dob
    return g


def merge_passport(booking: dict, passport_result: dict) -> dict:
    """把掃描證件結果併入文字訂房：證件欄位成為「入住者」；
    若原文字沒有 inline 入住者* 欄位，原本的獨立中文行（如「江-泰哥-呂布」）
    視為「訂房人」，不應成為入住者。"""
    pg = _passport_guest((passport_result or {}).get("parsed", {}) or {})
    has_primary = booking.get("_has_primary")
    existing = booking.get("guests") or []
    if has_primary:
        # 已有 inline 入住者* 欄位 -> 把證件欄位補進第一位客人
        g0 = existing[0] if existing else {}
        for k in ("zh_name", "en_name", "doc_number", "dob"):
            if pg.get(k) and not g0.get(k):
                g0[k] = pg[k]
        booking["guests"] = [g0] + existing[1:]
    else:
        # 無 inline 入住者 -> 證件即成為入住者；原獨立中文行轉為訂房人
        if not booking.get("booker") and existing:
            booking["booker"] = "、".join(
                g.get("zh_name", "") for g in existing if g.get("zh_name")
            )
        booking["guests"] = [pg] if pg else existing
    booking.pop("_has_primary", None)
    return booking


if __name__ == "__main__":
    sample = """入住： 7/21
退房： 7/23
飯店： 倫敦人
房型： 維多利亞房大床
件數： 1
是否吸煙：吸菸

江-泰哥-呂布
微信：泰哥服務群

入住者中文：吴俊
入住者英文：WU，JUN
出生年月日：1981.07.08
證件號碼：CJ9314108 讀取證件完畢 回傳文字"""
    import json
    print(json.dumps(parse(sample), ensure_ascii=False, indent=2))
