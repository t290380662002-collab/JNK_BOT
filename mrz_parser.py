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

    # TD3（容錯）：OCR 偶爾漏掉機讀區尾部的填充 '<'，使兩行都 < 40 字。
    # 護照 TD3 第一行必定以 'P' 開頭（TD1 卡式證件以 I/A/C 開頭，須排除，
    # 否則會被誤判成 TD3 第一行而解析錯亂）。只要存在這樣的候選行，
    # 且另有一條候選行（第二行），即可還原英文姓名。
    name_lines = [l for l in candidates if l and l[0] == "P" and "<<" in l]
    if name_lines and len(candidates) >= 2:
        line1 = name_lines[0]
        line2 = next((l for l in candidates if l != line1), line1)
        return _parse_td3(line1, line2)

    # 單行：需判斷是 TD3 第一行（含英文姓名）還是第二行（證件號/生日）。
    # 第一行以 P/V/A/C/I 開頭，姓名在位置 5 之後；第二行以數字或國碼開頭。
    # 注意：若單行其實是 TD1 三行黏成（長度 >= 60、含生日+性別+效期），
    #       應優先走 TD1 拆分，避免誤判為護照單行 MRZ。
    if longs:
        l0 = longs[0]
        if len(l0) >= 60 and l0[0] in "IACT" and re.search(r"\d{6}[0-9<][MFX<]\d{6}", l0):
            td1 = _reconstruct_td1_lines([l0])
            if td1:
                return _parse_td1(*td1)
        if l0[0] in "PVACI":
            last, first = _split_names(l0[5:])
            if last:
                return {
                    "doc_type_guess": "護照(單行MRZ)",
                    "last_name": last,
                    "first_name": first,
                }
        if "<" in l0:
            return _parse_td3_line2(l0)

    # TD1：含 '<' 且長度 >= 18 的候選行取最長三條
    mrz_like = [l for l in candidates if "<" in l and len(l) >= 18]
    if len(mrz_like) >= 3:
        line1, line2, name = _select_td1(mrz_like)
        return _parse_td1(line1, line2, name)

    # TD1 容錯：OCR 常把三行機讀碼（港澳通行證/回鄉證/台胞證）黏成一整行，
    # 或只讀到 line1+line2。此處嘗試把最長候選行拆回標準 30/30/30 三段。
    if mrz_like:
        td1 = _reconstruct_td1_lines(mrz_like)
        if td1:
            return _parse_td1(*td1)

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
    # 證件行：通常以 I/A/C 開頭後接 '<'（T 常為 I< 誤讀）
    line1 = next((l for l in others if l[:1] in "IAC" and l[1:2] == "<"), None)
    if line1 is None:
        line1 = next((l for l in others if l[:1] in "T" and l[1:2] in "<SCG"), None)
    if line1 is None:
        others_sorted = sorted(others, key=len, reverse=True)
        line1 = others_sorted[0]
        name = others_sorted[-1]
    else:
        name = [l for l in others if l != line1][0]
    return line1, line2, name


def _reconstruct_td1_lines(mrz_like):
    """把 OCR 黏成一整行的 TD1 機讀碼拆回 (line1, line2, name_line)。

    港澳通行證/回鄉證/台胞證的 TD1 標準為三行各 30 字。實務上 OCR 容易
    把底部三行讀成一行或兩行，此處先取最長的候選行，再依 TD1 佈局拆分。
    """
    # 取最長且含 '<' 的候選行（最可能是完整機讀區）
    blob = max(mrz_like, key=len)
    blob = _clean(blob)

    # 容錯：若開頭把 'I<' 誤讀成 'T'，嘗試修正
    if blob.startswith("T") and len(blob) >= 20:
        # 嘗試把 'TS' 視為 'I<CHN' 的誤讀：T->I, S-><, C->C, G->H, ?
        # 最實用做法是：如果後面出現 CHN/SCG，直接重設為 I<CHN 開頭
        if re.match(r"T[SCG][CG][NH]", blob):
            blob = "I<CHN" + blob[5:]

    # 若總長 >= 60，視為 line1+line2+name_line 黏在一起；
    # 優先按 30/30/30 切，再嘗試 30/30/... 。
    if len(blob) >= 60:
        # 用生日+性別+效期特徵定位 line2 起點，使拆分更穩健
        m = re.search(r"(\d{6}[0-9<][MFX<]\d{6})", blob)
        if m:
            i2 = m.start()
            # line2 在 TD1 中應位於 30-59；容錯取前後一段
            i2 = max(30, min(i2, len(blob) - 30))
            line2 = blob[i2:i2 + 30]
            line1 = blob[max(0, i2 - 30):i2]
            name = blob[i2 + 30:]
            # 若 line1 不足 30，由 blob 前端補
            if len(line1) < 30:
                line1 = blob[:30].ljust(30, "<")
            return line1, line2, name
        # 退化：直接 30/30/30 切
        return blob[:30], blob[30:60], blob[60:]

    # 若只有 line1+line2（30~59 字），日期行在後半段
    if 30 <= len(blob) < 60:
        m = re.search(r"(\d{6}[0-9<][MFX<]\d{6})", blob)
        if m:
            i2 = m.start()
            line2 = blob[i2:i2 + 30]
            line1 = blob[:i2]
            # line1 應為 30 字；若不足則左對齊並補 '<'
            if len(line1) < 30:
                line1 = line1.ljust(30, "<")
            name = blob[i2 + 30:]
            return line1, line2, name
        # 退化：前半為 line1，後半為 line2
        return blob[:30].ljust(30, "<"), blob[30:].ljust(30, "<"), ""

    return None


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


def _parse_td3_line2(l2: str) -> dict:
    """TD3 只剩第二行（OCR 把兩行打散時）的解析。

    TD3 第二行佈局（44 字）：
      doc(9) chk(1) iss(3) dob(6) chk(1) sex(1) exp(6) chk(1) ...
    可可靠取得 證件號/發證地/生日/性別/效期；
    姓名位於第一行，此處無法取得，留待 OCR 文字補。
    """
    res = {}
    res["doc_type_guess"] = "護照(單行MRZ)"
    res["doc_number"] = l2[0:9].rstrip("<")
    res["issuer"] = l2[2:5]
    res["nationality"] = _region(l2[10:13])
    res["date_of_birth"] = _parse_date(l2[13:19])
    res["sex"] = l2[20:21]
    res["expiry_date"] = _parse_date(l2[21:27])
    return res


def _parse_td1(l1: str, l2: str, l3: str) -> dict:
    res = {"doc_type_guess": "卡式證件"}
    l1 = (l1 or "").ljust(30, "<")
    l2 = (l2 or "").ljust(30, "<")
    l3 = (l3 or "").ljust(30, "<")
    # line1 容錯：I<CHN 被誤讀成 T 開頭時，強制轉回 I<CHN
    if l1[0] == "T" and l1[1:3] in ("SC", "CG", "CH", "HN"):
        l1 = "I<CHN" + l1[5:]
    res["issuer"] = l1[2:5] if l1[2:5] != "<<<" else "CHN"
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
