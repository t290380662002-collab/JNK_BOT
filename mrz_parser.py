"""
純 Python 的 MRZ（機讀區）解析器。
支援兩種格式：
  - TD3（兩行 44 字，護照）
  - TD1（三行 30 字，卡式證件：港澳通行證 / 回鄉證 / 台胞證）
不依賴任何外部 OCR 或校驗庫，便於在 Render 上穩定運行。
"""
import re
from datetime import datetime

# 常見發證地 / 國籍代碼對照（用於欄位輔助顯示，非必要）
KNOWN_CODES = {
    "CHN": "中國", "HKG": "香港", "MAC": "澳門", "TWN": "台灣",
    "GBR": "英國", "USA": "美國", "JPN": "日本", "KOR": "韓國",
    "FRA": "法國", "DEU": "德國", "CAN": "加拿大", "AUS": "澳洲",
    "SGP": "新加坡", "MYS": "馬來西亞", "THA": "泰國", "PHL": "菲律賓",
    "VNM": "越南", "IND": "印度", "BRA": "巴西", "RUS": "俄羅斯",
}


def _clean(line: str) -> str:
    """只保留 MRZ 合法字元：A-Z 0-9 與 <"""
    return re.sub(r"[^A-Z0-9<]", "", line.upper())


def _parse_date(s: str) -> str:
    """YYMMDD -> YYYY-MM-DD。無法解析則回傳空字串。"""
    if not s or len(s) != 6 or not s.isdigit():
        return ""
    yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
    year = 2000 + yy if yy <= 30 else 1900 + yy
    try:
        return datetime(year, mm, dd).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _split_names(name_field: str):
    """'SURNAME<<GIVEN' 或 'SURNAME<GIVEN' -> (姓, 名)"""
    name_field = name_field.replace("<", " ").strip()
    parts = [p.strip() for p in name_field.split("  ") if p.strip()]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if parts:
        return (parts[0], "")
    return ("", "")


def _region(code: str) -> str:
    return KNOWN_CODES.get(code, code)


def parse(mrz_lines):
    """
    輸入：從 OCR 文字中取出的候選行（list[str]）。
    輸出：dict（含 last_name/first_name/doc_number/nationality/
                  date_of_birth/sex/expiry_date/issuer/doc_type_guess）
          或 None（無法辨識）。
    """
    cleaned = [_clean(l) for l in mrz_lines]
    cleaned = [l for l in cleaned if l]
    if not cleaned:
        return None

    # MRZ 特徵：含 '<' 即高度可能是機讀區；另補上無 '<' 但很長的行
    candidates = [l for l in cleaned if "<" in l]
    candidates += [l for l in cleaned if "<" not in l and len(l) >= 40]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return None
    candidates.sort(key=len, reverse=True)

    # TD3：兩行都 >= 40 字（護照）
    longs = [l for l in candidates if len(l) >= 40]
    if len(longs) >= 2:
        line1 = next((l for l in longs if l[0] in "PVAC"), longs[0])
        line2 = next((l for l in longs if l != line1), longs[1])
        return _parse_td3(line1, line2)

    # TD1：含 '<' 且長度 >= 18 的候選行取最長三條
    mrz_like = [l for l in candidates if "<" in l and len(l) >= 18]
    if len(mrz_like) >= 3:
        line1, line2, name = _select_td1(mrz_like)
        return _parse_td1(line1, line2, name)

    return None


def _select_td1(lines):
    """從候選行中挑出 TD1 的三行：證件行 / 日期行 / 姓名行。"""
    lines = list(dict.fromkeys(lines))  # 去重保序
    while len(lines) < 3:
        lines.append("")

    # 日期行：生日(6)+校驗+性別(M/F/X)+效期(6) 的強特徵
    date_lines = [l for l in lines if re.search(r"\d{6}[0-9<][MFX<]\d{6}", l)]
    line2 = date_lines[0] if date_lines else lines[1]

    others = [l for l in lines if l != line2]
    # 證件行：通常以 I/A/C 開頭後接 '<'
    line1 = next((l for l in others if l[:1] in "IAC" and l[1:2] == "<"), None)
    if line1 is None:
        others_sorted = sorted(others, key=len, reverse=True)
        line1 = others_sorted[0]
        name = others_sorted[-1]
    else:
        name = [l for l in others if l != line1][0]
    return line1, line2, name


def _parse_td3(l1: str, l2: str) -> dict:
    res = {}
    doc_type = l1[0]
    res["doc_type_guess"] = "護照" if doc_type.startswith("P") else "其他"
    res["issuer"] = l1[2:5]
    last, first = _split_names(l1[5:])
    res["last_name"] = last
    res["first_name"] = first
    res["doc_number"] = l2[0:9].rstrip("<")
    res["nationality"] = _region(l2[10:13])
    res["date_of_birth"] = _parse_date(l2[13:19])
    res["sex"] = l2[20:21]
    res["expiry_date"] = _parse_date(l2[21:27])
    return res


def _parse_td1(l1: str, l2: str, l3: str) -> dict:
    res = {}
    res["doc_type_guess"] = "卡式證件"
    res["issuer"] = l1[2:5]
    res["doc_number"] = l1[5:14].rstrip("<")
    res["date_of_birth"] = _parse_date(l2[0:6])
    res["sex"] = l2[7:8]
    res["expiry_date"] = _parse_date(l2[8:14])
    res["nationality"] = _region(l2[15:18])
    last, first = _split_names(l3)
    res["last_name"] = last
    res["first_name"] = first
    return res


# ---------------------------------------------------------------------------
# 當模組直接執行時做簡易自測
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    samples = {
        "護照(TD3)": [
            "P<GBRSTEPHENS<<JOHN<<MR<MICHAEL" + "<" * 12,
            "GBN123456<GBR8501018M2501018" + "<" * 15 + "0",
        ],
        "卡式(TD1)": [
            "I<CHN123456789<" + "<" * 15,
            "8501014M2501018CHN" + "<" * 11 + "0",
            "SURNAME<<GIVENNAME",
        ],
    }
    for label, lines in samples.items():
        print(f"\n=== {label} ===")
        print(parse(lines))
