"""
OCR 混合管線：
  1) 本地 Tesseract 抽取 MRZ 機讀區（免費、離線）
     —— 多策略：全圖 + 底部機讀區聚焦、放大、自動對比、中值去噪、
        二值化、多種 psm，大幅提升證件照片的辨識率。
  2) 失敗時呼叫雲端 OCR（Google Vision / Azure）做全頁辨識備援
  3) 最後用本地全頁文字 regex 兜底抽取證件號碼
由環境變數 OCR_PROVIDER 控制是否啟用雲端備援。
"""
import os
import re
import io
import json
import base64
import logging

import pytesseract
from PIL import Image, ImageFilter, ImageOps

import mrz_parser

logger = logging.getLogger(__name__)

CLOUD_PROVIDER = os.environ.get("OCR_PROVIDER", "none").lower()
MRZ_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"

OCR_LANG = "eng+chi_tra"  # 本地 OCR 多語言：英文 MRZ + 繁體中文姓名/欄位


def _available_cloud() -> list:
    """回傳當前可用的雲端 OCR 提供者清單。
    - 'none'/''/'false'：不啟用雲端備援
    - 'google'/'azure'：指定單一提供者（需對應金鑰）
    - 'auto'：自動選用「已設定金鑰」的提供者（Google 優先，其次 Azure）
    這讓使用者只需在 Render 後台貼上任一組金鑰即可啟用，無須切換 provider。
    """
    if CLOUD_PROVIDER in ("none", "", "false"):
        return []
    if CLOUD_PROVIDER == "auto":
        out = []
        # 優先放免費且已設定的提供者（OCR.space 免費、免綁卡），其次雲端大廠
        if os.environ.get("OCR_SPACE_KEY"):
            out.append("ocrspace")
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            out.append("google")
        if os.environ.get("AZURE_VISION_ENDPOINT") and os.environ.get("AZURE_VISION_KEY"):
            out.append("azure")
        return out
    return [CLOUD_PROVIDER]


# ---------------------------------------------------------------------------
# 本地 MRZ 抽取（多策略）
# ---------------------------------------------------------------------------
def _mean(img: Image.Image) -> float:
    px = list(img.getdata())
    return sum(px) / len(px)


def _prep_mrz(img: Image.Image, upscale_to: int) -> Image.Image:
    """灰階 → 自動對比 → 中值去噪 → 二值化(確保黑字白底) → 必要時放大。
    MRZ 是機器印刷、對比最高的文字，經此處理後 Tesseract 辨識率明顯提升。"""
    img = img.convert("L")
    w, h = img.size
    s = upscale_to / max(w, h)
    if s > 1:
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(3))
    # 二值化：固定閾值；若整體偏暗（白字黑底）則反相成黑字白底
    bw = img.point(lambda p: 255 if p > 140 else 0)
    if _mean(bw) < 128:
        bw = ImageOps.invert(bw)
    return bw


def _bottom_crop(img: Image.Image, frac: float = 0.5) -> Image.Image:
    """裁切底部 frac 比例——MRZ 固定位於證件底部。"""
    w, h = img.size
    return img.crop((0, int(h * (1 - frac)), w, h))


def _ocr_pass(img: Image.Image, upscale_to: int, psm: int,
              whitelist: str = MRZ_WHITELIST) -> list:
    pre = _prep_mrz(img, upscale_to)
    cfg = f"--psm {psm} -c tessedit_char_whitelist={whitelist}"
    try:
        txt = pytesseract.image_to_string(pre, config=cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("OCR pass 失敗(psm=%s): %s", psm, e)
        return []
    return [l for l in txt.splitlines() if l.strip()]


def _collect_candidates(image_bytes: bytes) -> list:
    """對同一張圖跑多種 OCR 策略，彙整 MRZ 候選行（去重）。"""
    img = Image.open(io.BytesIO(image_bytes))
    raw = []
    # 全圖 + 底部機讀區聚焦；多種 psm 組合
    for variant in (img, _bottom_crop(img, 0.5)):
        for up, psm in ((1400, 6), (1600, 6), (1700, 11), (1500, 7)):
            raw += _ocr_pass(variant, up, psm)
    # 過濾成 MRZ 候選：含 '<' 且長度 >= 18
    lines = []
    for l in raw:
        c = mrz_parser._clean(l)
        if "<" in c and len(c) >= 18:
            lines.append(c)
    # 去重保序
    seen, out = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _extract_mrz_lines(text: str) -> list:
    """從 OCR 文字中挑出 MRZ 候選行：含 '<' 且長度 >= 18。"""
    lines = []
    for raw in text.splitlines():
        cleaned = mrz_parser._clean(raw)
        if "<" in cleaned and len(cleaned) >= 18:
            lines.append(cleaned)
    return lines


def _reconstruct_mrz_lines(text: str) -> list:
    """OCR 常把機讀區整行拆成多段/多行（尤其第一行含英文姓名那行）。
    把所有『純 MRZ 字元（A-Z0-9<）』片段拼成一段，再以可靠特徵重組 TD3 兩行：

    關鍵：以「9 位數字 + '<'」錨定第二行（證件號碼，OCR 極少讀錯），
    其前方 44 字即第一行（含英文姓名）。這比固定寬度滑窗穩健——
    即使第一行被拆成數段、各段長度不一，也能正確對齊。

    例：OCR 把 'P<TWN<GU<<' 與 'FONG-ZHI<<<...' 拆成兩行 -> 拼回成
        完整的 'P<TWN<GU<<FONG-ZHI<<...'（44 字）讓解析器取回英文姓名。
    """
    frags = []
    for raw in text.splitlines():
        c = re.sub(r"[^A-Z0-9<]", "", raw.upper())
        if "<" in c and len(c) >= 10:
            frags.append(c)
    if not frags:
        return []
    blob = "".join(frags)

    # 0) 先判斷是否為 TD1（港澳通行證/回鄉證/台胞證）：以 I/A/C/T 開頭，
    #    且內含出生日期+性別+效期特徵。TD1 優先於 TD3，避免把卡式證件
    #    的證件號誤當成護照第二行。
    if blob and blob[0] in "IACT" and re.search(r"\d{6}[0-9<][MFX<]\d{6}", blob):
        td1 = mrz_parser._reconstruct_td1_lines([blob])
        if td1:
            return list(td1)

    # 1) 以證件號碼（9 位數字後接 '<'）錨定第二行起點；
    #    其前方（不足 44 字則從 0 起）即第一行。
    m2 = re.search(r"\d{9}<", blob)
    if m2:
        i2 = m2.start()
        i1 = max(0, i2 - 44)
        w1 = blob[i1:i2]
        w2 = blob[i2:i2 + 44]
        if w1 and w1[0] in "PVACI" and w1.count("<") >= 1 and w2.count("<") >= 1:
            return [w1, w2]

    # 2) 退化：滑動 44 字視窗找「以 P/V/A/C/I 開頭」的 line1，其後接 line2
    best: list = []
    for i in range(0, len(blob) - 43):
        w1 = blob[i:i + 44]
        if w1[0] not in "PVACI" or w1.count("<") < 1:
            continue
        rest = blob[i + 44:]
        if len(rest) >= 30:
            w2 = rest[:44]
            if (w2[0].isdigit() or w2[0] in "PVACI") and w2.count("<") >= 1:
                best = [w1, w2]
                break
    if best:
        return best

    # 3) 只找到一行（TD3 第二行或 TD1 行）
    for i in range(0, len(blob) - 29):
        w = blob[i:i + 30]
        if w[0] in "PVACI" and w.count("<") >= 1:
            return [w]
    return []


def _parse_from_free_text(text: str) -> dict | None:
    """全頁文字的兜底解析：先找 MRZ 行（含單行第二行），再嘗試 regex 撈證件號碼。

    若 MRZ 只剩第二行（護照常見），會再從可見文字補上英文姓名，避免只回傳 doc/dob。
    OCR 把機讀區拆段時，先嘗試 _reconstruct_mrz_lines 拼回完整行。
    """
    lines = _extract_mrz_lines(text)
    if len(lines) < 2:
        rec = _reconstruct_mrz_lines(text)
        if rec:
            lines = rec
    if lines:
        parsed = mrz_parser.parse(lines)
        # 標準抽取沒拿到姓名時，再試一次拼回 MRZ 重解（OCR 把第一行拆段時常見）
        if not (parsed and (parsed.get("last_name") or parsed.get("en_name"))):
            rec = _reconstruct_mrz_lines(text)
            if rec and rec != lines:
                rp = mrz_parser.parse(rec)
                if rp and (rp.get("last_name") or rp.get("en_name")):
                    if parsed is None:
                        parsed = rp
                    else:
                        for k in ("zh_name", "en_name", "last_name", "first_name"):
                            if rp.get(k) and not parsed.get(k):
                                parsed[k] = rp[k]
        # 即使 parser 回傳單行 MRZ（只有 doc/dob，缺姓名），也從可見文字補姓名
        if parsed and not (parsed.get("last_name") or parsed.get("en_name")):
            cf = _extract_chinese_fields(text)
            if cf:
                for k in ("zh_name", "en_name", "last_name", "first_name"):
                    if cf.get(k):
                        parsed[k] = cf[k]
        # 卡式證件（港澳通行證/回鄉證/台胞證）MRZ 常被誤讀或黏行，
        # 以可見文字欄位補強/覆蓋 doc_number、date_of_birth、姓名。
        if parsed and parsed.get("doc_type_guess") in ("卡式證件", "證件(無MRZ)"):
            cf = _extract_chinese_fields(text)
            if cf:
                # 優先用可見文字：姓名、證件號碼、出生日期
                for k in ("zh_name", "en_name", "last_name", "first_name", "date_of_birth"):
                    if cf.get(k) and not parsed.get(k):
                        parsed[k] = cf[k]
                # 證件號碼：若 MRZ 解析為空或僅數字，以可見文字為主
                if cf.get("doc_number"):
                    mrz_doc = parsed.get("doc_number") or ""
                    vis_doc = cf["doc_number"]
                    if not mrz_doc or (mrz_doc.isdigit() and not vis_doc.isdigit()):
                        parsed["doc_number"] = vis_doc
                    elif mrz_doc and vis_doc not in mrz_doc and mrz_doc not in vis_doc:
                        # 兩者完全不同且可見文字非純數字，採用可見文字
                        parsed["doc_number"] = vis_doc
        if parsed:
            return parsed
        # 單行 MRZ 第二行：有 doc/dob，但缺姓名 -> 從可見文字補姓名
        if len(lines) == 1 and len(lines[0]) >= 40:
            parsed = mrz_parser._parse_td3_line2(lines[0])
            cf = _extract_chinese_fields(text)
            if cf:
                for k in ("zh_name", "en_name", "last_name", "first_name"):
                    if cf.get(k):
                        parsed[k] = cf[k]
            return parsed
    m = re.search(
        r"(?:PASSPORT|護照|NO\.?|號碼|DOC(?:UMENT)?\s*NO)[\s:]*([A-Z]{1,3}[0-9]{6,9})",
        text, re.I,
    )
    if m:
        parsed = {"doc_number": m.group(1), "doc_type_guess": "護照"}
        cf = _extract_chinese_fields(text)
        if cf:
            for k in ("zh_name", "en_name", "last_name", "first_name", "date_of_birth"):
                if cf.get(k):
                    parsed[k] = cf[k]
        return parsed
    return None


def _extract_chinese_fields(text: str) -> dict | None:
    """從全頁 OCR 文字抽取「無 MRZ 證件」的中文/英文欄位。

    適用對象：港澳通行證(正面)、回鄉證、台胞證、身份證、護照等。
    雲端 OCR 能讀出「姓名 / 證件號碼 / 出生」等欄位；本函數使用多組正則
    與交叉驗證，盡量避免護照「只讀到證件號，讀不到姓名」的狀況。

    回傳 parsed dict（部分欄位可能有值），或 None（完全無可擷取欄位）。
    欄位設計與 MRZ 解析器相容：doc_number / date_of_birth / zh_name / en_name。
    """
    res: dict = {}

    # 證件號碼：OCR 常在字母數字間塞空格（如 "C J 9 3 1..."），先去掉內部空白。
    # 同時移除 MRZ 機讀行（40+ 個 A-Z0-9< 連續字元），避免把 MRZ 裡的數字段誤認為證件號。
    text_no_mrz = re.sub(r"[A-Z0-9<]{40,}", "", text)
    collapsed = re.sub(r"(?<=[A-Z0-9])\s+(?=[A-Z0-9])", "", text_no_mrz)

    # 誤判黑名單：證件上常見的英文單字，絕不能當成證件號碼
    _WORD_BLACKLIST = {
        "PASSPORT", "DPASSPORT", "REPUBLIC", "NATIONAL", "IDENTITY",
        "DOCUMENT", "SURNAME", "GENDER", "NATION", "AUTHORITY", "PERMIT",
        "PERSONAL", "PLACE", "MINISTRY", "FOREIGN", "AFFAIRS",
    }

    def _valid_docnum(cand: str) -> bool:
        """真證件號需含足夠數字；純英文單字（如 PASSPORT）一律拒絕。"""
        if cand.upper() in _WORD_BLACKLIST:
            return False
        digits = sum(c.isdigit() for c in cand)
        return digits >= 4  # 港澳通行證/護照號碼至少含 4 位以上數字

    # 港澳通行證 C+8 位(字母或數字) / 台胞證 / 回鄉證 等
    # 例：CJ9314108（C + J9314108）、C12345678
    doc_cands = re.findall(r"(?<![0-9A-Z])([CEHDAK][A-Z0-9]{7,10})(?![0-9A-Z])", collapsed)
    doc_cands += re.findall(r"(?<![0-9])([0-9]{8,9})(?![0-9])", collapsed)
    # 護照號碼有時被拆成 P 123524855，也要能合併後偵測
    doc_cands += re.findall(r"(?<![0-9A-Z])([P][A-Z0-9]{7,10})(?![0-9A-Z])", collapsed)
    for cand in doc_cands:
        if _valid_docnum(cand):
            res["doc_number"] = cand
            break

    # -----------------------------------------------------------------------
    # 姓名抽取：多策略 + 交叉驗證
    # -----------------------------------------------------------------------
    _EN_LABEL_BLACKLIST = {
        "NAME", "SURNAME", "GIVEN", "BIRTH", "DATE", "SEX", "NATIONAL",
        "NATIONALITY", "PASSPORT", "DOCUMENT", "AUTHORITY", "REPUBLIC",
        "CHINA", "CHINESE", "SIGNATURE", "TYPE", "ISSUE", "EXPIRY",
        "OF", "THE", "TAIWAN", "TAIPEI", "REPUBLICOFCHINA", "FOREIGN",
        "AFFAIRS", "MINISTRY", "PLACE", "PERSONAL", "IDENTITY",
    }

    def _clean_en_name(last: str, first: str) -> tuple | None:
        """清理並驗證英文姓名，回傳 (last, first) 或 None。"""
        last = last.strip().upper()
        first = first.strip().upper()
        if not last or not first:
            return None
        if last in _EN_LABEL_BLACKLIST:
            return None
        # 排除數字、MRZ 填充符、過長/過短段
        if re.search(r"[<0-9]", first) or re.search(r"[<0-9]", last):
            return None
        parts = re.split(r"[,，\s\-]+", f"{last} {first}")
        for p in parts:
            if p and (len(p) < 2 or len(p) > 15):
                return None
        # 排除連續 4+ 重複字母
        if re.search(r"([A-Z])\1{3,}", last + first):
            return None
        return last, first

    def _extract_en_name(search_text: str) -> tuple | None:
        """從文字中抽取英文姓名，回傳 (last, first) 或 None。

        支援 OCR 常見的空格干擾："GU , FONG - ZHI"、"GU FONG ZHI"、
        "GU, FONG-ZHI"、"GU, FONG ZHI" 等。
        """
        # A) 姓, 名 / 姓，名（最標準，優先）；容忍標點前後空格
        m = re.search(
            r"\b([A-Z]{2,})\s*[,，]\s*([A-Z]+(?:\s*[ -]\s*[A-Z]+)*)",
            search_text,
        )
        if m:
            # 把 first 裡的空格正規化為單一連字號（FONG - ZHI -> FONG-ZHI）
            first = re.sub(r"\s*-\s*", "-", m.group(2))
            first = re.sub(r"\s+", "-", first).strip("-")
            return _clean_en_name(m.group(1), first)
        # B) 姓 名（空白分隔，名可含連字號或多段）
        m = re.search(r"\b([A-Z]{2,})\s+([A-Z]+(?:\s*[ -]\s*[A-Z]+){0,3})\b", search_text)
        if m:
            first = re.sub(r"\s*-\s*", "-", m.group(2))
            first = re.sub(r"\s+", "-", first).strip("-")
            return _clean_en_name(m.group(1), first)
        return None

    # 1) 先嘗試用標籤找中文姓名
    zh_name = None
    zh_match = re.search(
        r"(?:姓名|名|Name)[^\u4e00-\u9fff]{0,80}([\u4e00-\u9fff]{2,4})",
        text, re.I,
    )
    if zh_match:
        zh_name = zh_match.group(1)
        res["zh_name"] = zh_name

    # 2) 找英文姓名：優先在中文字附近，再全局搜
    en_name = None
    last_name = first_name = None
    if zh_name and zh_match:
        # 在中文字前後各 200 字元範圍找英文姓名
        start = max(0, zh_match.start() - 200)
        end = min(len(text), zh_match.end() + 200)
        nearby = text[start:end]
        pair = _extract_en_name(nearby)
        if pair:
            last_name, first_name = pair
            en_name = f"{last_name},{first_name}"
    if not en_name:
        pair = _extract_en_name(text)
        if pair:
            last_name, first_name = pair
            en_name = f"{last_name},{first_name}"

    # 3) 若找到英文姓名但沒找到中文姓名 -> 反推附近的中文字
    if en_name and not zh_name:
        # 找到英文姓名的位置，前後 120 字元內找 2-4 個連續中文字
        en_pos = text.find(en_name.replace(",", "，"))
        if en_pos < 0:
            en_pos = text.find(last_name)
        if en_pos >= 0:
            start = max(0, en_pos - 120)
            end = min(len(text), en_pos + len(en_name) + 120)
            region = text[start:end]
            # 排除常見證件標籤/機關名稱，避免把「往來港澳」「通行證」當姓名
            _ZH_BLACKLIST = {
                "往來港澳", "通行证", "通行證", "中華人民共和國",
                "中华人民共和国", "出入境管理局", "四川", "出生日期",
                "有效期限", "签发机关", "簽發機關", "签发地点", "簽發地點",
                "性别", "性別", "男", "女", "姓名", "名", "Name",
            }
            candidates = re.findall(r"[\u4e00-\u9fff]{2,4}", region)
            for cand in candidates:
                if cand not in _ZH_BLACKLIST:
                    zh_name = cand
                    res["zh_name"] = zh_name
                    break

    # 4) 最後退路：全局找孤立的中英文姓名對（適合版面乾淨的護照）
    if not zh_name and not en_name:
        # 雙向：'中文名 英文名' 或 '英文名 中文名'（含換行/同行的各種排列）
        m = re.search(
            r"([\u4e00-\u9fff]{2,4})\s*[\n]?\s*([A-Z]{2,}(?:[,，\s\-][A-Z]+)+)",
            text,
        )
        if m:
            zh_name = m.group(1)
            pair = _extract_en_name(m.group(2))
            if pair:
                last_name, first_name = pair
                en_name = f"{last_name},{first_name}"
                res["zh_name"] = zh_name
        else:
            m = re.search(
                r"([A-Z]{2,}(?:[,，\s\-][A-Z]+)+)\s*[\n]?\s*([\u4e00-\u9fff]{2,4})",
                text,
            )
            if m:
                pair = _extract_en_name(m.group(1))
                if pair:
                    last_name, first_name = pair
                    en_name = f"{last_name},{first_name}"
                    zh_name = m.group(2)
                    res["zh_name"] = zh_name

    if en_name:
        res["en_name"] = en_name
        res["last_name"] = last_name
        res["first_name"] = first_name

    # 5) 最後退路：OCR 把 MRZ 第一行（含英文姓名）當純文字帶回時，
    #    用拼回邏輯還原 TD3 機讀行並從第一行取回姓/名。
    if not en_name:
        rec = _reconstruct_mrz_lines(text)
        if len(rec) >= 1:
            mp = mrz_parser.parse(rec)
            if mp and mp.get("last_name"):
                last_name = mp["last_name"]
                first_name = mp.get("first_name", "")
                en_name = f"{last_name},{first_name}"
                res["en_name"] = en_name
                res["last_name"] = last_name
                res["first_name"] = first_name

    # -----------------------------------------------------------------------
    # 出生日期：支援 1981.07.08 / 1981年07月08日 / 1981-07-08 / 05 AUG 1989
    # -----------------------------------------------------------------------
    dm = re.search(
        r"(?:出生|出生日期|Birth|DOB|Date of birth)[：:\s]*"
        r"(\d{4})[./年\-](\d{1,2})[./月\-](\d{1,2})",
        text, re.I,
    )
    if dm:
        try:
            res["date_of_birth"] = (
                f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            )
        except ValueError:
            pass
    else:
        # 西文日期：05 AUG 1989 / 1989 AUG 05 / 05-AUG-1989
        mon_map = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
        }
        m = re.search(
            r"(?i)(?:出生|出生日期|Birth|DOB|Date of birth)?[：:\s]*"
            r"(\d{1,2})[\s\-]([A-Z]{3})[\s\-](\d{4})",
            text,
        )
        if m:
            dd, mon, yyyy = m.group(1), m.group(2).upper(), m.group(3)
            if mon in mon_map:
                try:
                    res["date_of_birth"] = f"{yyyy}-{mon_map[mon]}-{int(dd):02d}"
                except ValueError:
                    pass

    if not res:
        return None
    res["doc_type_guess"] = "證件(無MRZ)"
    return res


# ---------------------------------------------------------------------------
# OCR 結果品質檢查（亂碼過濾）
# ---------------------------------------------------------------------------
def _is_plausible_name(s: str) -> bool:
    """英文姓名品質檢查：排除 OCR 雜訊（如 KKKKKK / EEEEEE / 過長重複字元）。"""
    if not s or not isinstance(s, str):
        return False
    core = re.sub(r"[^A-Za-z]", "", s)
    if len(core) < 3 or len(core) > 30:
        return False
    # 連續 4+ 相同字母 -> 雜訊（如 BLERREKKEEEEKKKKK）
    if re.search(r"([A-Za-z])\1{3,}", core):
        return False
    # 各段長度需合理（2~15 字母）
    parts = re.split(r"[,，\s\-]", s.strip())
    for p in parts:
        if p and (len(p) < 2 or len(p) > 15):
            return False
    return True


def _is_plausible_docnum(s: str) -> bool:
    """證件號碼品質檢查：至少含 4 位數字，且無 4+ 連續相同字元。"""
    if not s or not isinstance(s, str):
        return False
    s = s.strip().upper()
    if len(s) < 5 or len(s) > 14:
        return False
    # 連續 4+ 相同字元 -> 雜訊
    if re.search(r"([A-Z0-9])\1{3,}", s):
        return False
    # 證件號通常含數字（港澳通行證/護照/身分證皆有），純字母視為可疑
    return sum(c.isdigit() for c in s) >= 4


def _sanitize(parsed: dict) -> dict:
    """清除明顯的 OCR 雜訊欄位；若清理後無任何有效識別欄位，回傳 {}。

    有效識別欄位：中文姓名 / 英文姓名 / 證件號碼。
    這能擋掉「讀到一堆亂碼就自信回傳」的狀況——例如英文姓名
    'BLERREKKEEEEKKKKK KK,'（重複字母）、證件號 'SBLTGPS41'（只有 2 位數字）。
    """
    if not parsed:
        return {}
    # 中文姓名：僅接受 2~4 個中文字
    zh = parsed.get("zh_name")
    if zh and not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", str(zh)):
        parsed.pop("zh_name", None)
    # 英文姓名（en_name / last_name+first_name 同步清理）
    en = parsed.get("en_name")
    last = parsed.get("last_name")
    first = parsed.get("first_name")
    if en and not _is_plausible_name(en):
        parsed.pop("en_name", None)
    if last and not _is_plausible_name(f"{last},{first}" if first else last):
        parsed.pop("last_name", None)
        parsed.pop("first_name", None)
    # 證件號碼
    dn = parsed.get("doc_number")
    if dn and not _is_plausible_docnum(dn):
        parsed.pop("doc_number", None)
    # 至少需有一個有效識別欄位，否則視為掃描失敗
    if not any(parsed.get(k) for k in ("zh_name", "en_name", "last_name", "doc_number")):
        return {}
    return parsed


# ---------------------------------------------------------------------------
# 常用旅客名冊（known_passengers.json）
# ---------------------------------------------------------------------------
# 證件 OCR 對姓名（尤其中文）時有誤讀（例：古豐誌→特照、FONG→PONG）。
# 對重複客人，用「證件號碼」對照名冊，直接套用正確的中英文姓名與生日，
# 這比 OCR 可靠得多，也避免每次掃描都因畫質而出錯。
# 新增客人：編輯 known_passengers.json，鍵為證件號碼（純數字或字母數字）。
_KNOWN_CACHE: dict | None = None


def _load_known_passengers() -> dict:
    global _KNOWN_CACHE
    if _KNOWN_CACHE is not None:
        return _KNOWN_CACHE
    data: dict = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "known_passengers.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("讀取 known_passengers.json 失敗：%s", e)
        data = {}
    # 正規化鍵：去除空白、轉大寫，便於比對
    norm = {}
    for k, v in data.items():
        norm[str(k).strip().upper()] = v
    _KNOWN_CACHE = norm
    return _KNOWN_CACHE


def apply_known_passenger(parsed: dict | None) -> dict | None:
    """若 parsed 的證件號碼命中名冊，套用名冊中的正確姓名/生日。

    命中時標記 parsed["known_passenger"]=True，供後續顯示「已對照名冊」提示。
    回傳原 dict（就地修改）或 None。冪等：重複呼叫無副作用。
    """
    if not parsed:
        return parsed
    dn = parsed.get("doc_number")
    if not dn:
        return parsed
    kp = _load_known_passengers().get(str(dn).strip().upper())
    if not kp:
        return parsed
    changed = False
    if kp.get("zh_name"):
        parsed["zh_name"] = kp["zh_name"]
        changed = True
    if kp.get("en_name"):
        en = str(kp["en_name"]).strip()
        parsed["en_name"] = en
        if "," in en:
            ln, fn = en.split(",", 1)
            parsed["last_name"] = ln.strip().upper()
            parsed["first_name"] = fn.strip().upper()
        changed = True
    if kp.get("date_of_birth"):
        parsed["date_of_birth"] = kp["date_of_birth"]
        changed = True
    if changed:
        parsed["known_passenger"] = True
        logger.info("命中常用旅客名冊：證件號 %s", dn)
    return parsed


def process_image(image_bytes: bytes) -> dict | None:
    """
    回傳 dict：
      { "parsed": {...}, "confidence": str, "source": str, "raw": str }
    或 None（完全無法辨識）。
    """
    # 1) 本地 MRZ 多策略抽取
    try:
        lines = _collect_candidates(image_bytes)
    except Exception:  # noqa: BLE001
        logger.exception("本地 OCR 處理例外")
        lines = []
    logger.info("本地 MRZ 候選行數：%d", len(lines))

    if len(lines) >= 2:
        parsed = mrz_parser.parse(lines)
        parsed = _sanitize(parsed)
        parsed = apply_known_passenger(parsed)
        if parsed:
            return {
                "parsed": parsed,
                "confidence": "high",
                "source": "local_mrz",
                "raw": "\n".join(lines),
            }

    # 2) 雲端 OCR 備援（依 OCR_PROVIDER / 已設定金鑰自動決定）
    _cloud_providers = _available_cloud()
    if _cloud_providers:
        logger.info("啟用雲端 OCR 備援：%s", _cloud_providers)
        cloud_text = _cloud_ocr(image_bytes)
        if cloud_text:
            cf = _extract_chinese_fields(cloud_text)   # 中英文姓名 + 證件號（OCR 文字）
            parsed = _parse_from_free_text(cloud_text)  # 含單行 MRZ 第二行解析（doc/dob/sex/nat/exp）
            # 合併：MRZ 欄位最可靠（證件號/生日/英文姓名），中文姓名以 OCR 文字為主
            merged: dict = {}
            if cf:
                merged.update(cf)
            if parsed:
                for k in ("doc_number", "date_of_birth", "sex",
                          "nationality", "expiry_date", "issuer",
                          "doc_type_guess", "last_name", "first_name",
                          "en_name"):
                    if parsed.get(k):
                        merged[k] = parsed[k]
            # 若 MRZ 沒給英文姓名（單行 MRZ 缺第一行），但 cf 抽到 en_name，則補上
            if not merged.get("en_name") and merged.get("last_name"):
                merged["en_name"] = f"{merged['last_name']},{merged.get('first_name','')}"
            merged = _sanitize(merged)
            merged = apply_known_passenger(merged)
            if merged:
                return {
                    "parsed": merged,
                    "confidence": "medium",
                    "source": f"cloud_{_cloud_providers[0]}_text",
                    "raw": cloud_text,
                    # 只有「真的沒偵測到 MRZ」才標註，避免誤導使用者
                    "no_mrz": parsed is None,
                }
    elif CLOUD_PROVIDER not in ("none", "", "false"):
        logger.warning("OCR_PROVIDER=%s 但缺少對應金鑰，雲端備援未啟用", CLOUD_PROVIDER)

    # 3) 本地全頁文字兜底（eng+chi_tra 多語言）撈證件號碼 / 中文欄位
    try:
        img_full = Image.open(io.BytesIO(image_bytes)).convert("L")
        # 放大 + 銳化：真實證件照的中文姓名是小字，放大後 Tesseract 辨識率明顯提升
        # （數字那一步有放大所以讀得到，中文這一步原本沒放大，故補上）。
        w, h = img_full.size
        scale = max(1.0, 1800 / max(w, h))
        if scale > 1:
            img_full = img_full.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img_full = img_full.filter(ImageFilter.SHARPEN)
        full = pytesseract.image_to_string(img_full, lang=OCR_LANG, config="--psm 6")
        # 額外針對「上半部（中文姓名常出現區域）」做一次繁中聚焦 OCR，
        # 提升中文姓名擷取率（護照正面照片姓名多在照片右側/上半）。
        w, h = img_full.size
        top = img_full.crop((0, 0, w, int(h * 0.6)))
        top_text = pytesseract.image_to_string(top, lang="chi_tra", config="--psm 6")
        full = full + "\n" + top_text
    except Exception:  # noqa: BLE001
        full = ""
    parsed = _parse_from_free_text(full)
    parsed = _sanitize(parsed)
    parsed = apply_known_passenger(parsed)
    if parsed:
        return {"parsed": parsed, "confidence": "low", "source": "local_text", "raw": full}

    # 本地也嘗試中文欄位（Tesseract 雖弱，偶能撈到證件號碼/中文姓名）
    cf = _extract_chinese_fields(full)
    cf = _sanitize(cf)
    cf = apply_known_passenger(cf)
    if cf:
        return {
            "parsed": cf,
            "confidence": "low",
            "source": "local_text",
            "raw": full,
            "no_mrz": True,
        }

    return None


# ---------------------------------------------------------------------------
# 雲端 OCR 備援
# ---------------------------------------------------------------------------
def _cloud_ocr(image_bytes: bytes) -> str:
    """依可用提供者依序呼叫雲端 OCR，彙整全部回傳文字。"""
    texts = []
    for provider in _available_cloud():
        if provider == "google":
            t = _google_vision(image_bytes)
        elif provider == "azure":
            t = _azure_ocr(image_bytes)
        elif provider == "ocrspace":
            t = _ocrspace_ocr(image_bytes)
        else:
            continue
        if t:
            texts.append(t)
    return "\n".join(texts)


def _google_vision(image_bytes: bytes) -> str:
    import requests

    key = os.environ.get("GOOGLE_VISION_API_KEY")
    if not key:
        return ""
    url = f"https://vision.googleapis.com/v1/images:annotate?key={key}"
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode()},
                "features": [{"type": "TEXT_DETECTION"}],
            }
        ]
    }
    try:
        r = requests.post(url, json=body, timeout=25)
        r.raise_for_status()
        return r.json()["responses"][0].get("fullTextAnnotation", {}).get("text", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("Google Vision 失敗：%s", e)
        return ""


def _azure_ocr(image_bytes: bytes) -> str:
    import requests

    endpoint = os.environ.get("AZURE_VISION_ENDPOINT", "").rstrip("/")
    key = os.environ.get("AZURE_VISION_KEY")
    if not endpoint or not key:
        return ""
    url = f"{endpoint}/vision/v3.2/ocr?language=unk&detectOrientation=true"
    try:
        r = requests.post(
            url,
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/octet-stream"},
            data=image_bytes,
            timeout=25,
        )
        r.raise_for_status()
        lines = []
        for region in r.json().get("regions", []):
            for line in region.get("lines", []):
                words = [w["text"] for w in line.get("words", [])]
                lines.append(" ".join(words))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("Azure OCR 失敗：%s", e)
        return ""


def _downscale_if_large(image_bytes: bytes, max_bytes: int = 900_000) -> bytes:
    """OCR.space 免費方案單檔上限 1MB；超過就逐步縮小，避免上傳被拒。"""
    if len(image_bytes) <= max_bytes:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        out = image_bytes
        scale = 1.0
        for _ in range(6):
            scale *= 0.8
            w, h = img.size
            ni = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            ni.save(buf, format="JPEG", quality=85)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                break
        return out
    except Exception:  # noqa: BLE001
        return image_bytes


def _ocrspace_ocr(image_bytes: bytes) -> str:
    """OCR.space 免費 API（免綁卡）。

    改用「雙語雙引擎」策略以最大化姓名擷取率：
      - eng + Engine1：MRZ 機讀區與英文姓名是純英文印刷體，Engine1 對印刷體最準，
        能穩定讀出 MRZ 第一行（含英文姓名 GU<<FONG-ZHI）與第二行（證件號/生日）。
      - cht + Engine2：負責繁體中文姓名（古豐誌）與中文欄位標籤。
    兩次結果拼接後回傳，後續抽取邏輯各取所需。
    """
    import requests

    key = os.environ.get("OCR_SPACE_KEY")
    if not key:
        return ""
    url = "https://api.ocr.space/parse/image"
    # 免費方案單檔 ≤1MB；過大先縮小
    payload = image_bytes if len(image_bytes) <= 900_000 else _downscale_if_large(image_bytes)
    texts = []
    # (語言, 引擎)：英文跑 MRZ/英文姓名，繁中跑中文姓名
    for lang, engine in (("eng", "1"), ("cht", "2")):
        try:
            r = requests.post(
                url,
                data={
                    "apikey": key,
                    "language": lang,
                    "isOverlayRequired": "false",
                    "scale": "true",        # 自動放大低 DPI 內容
                    "OCREngine": engine,
                },
                files={"image": ("id.jpg", payload, "image/jpeg")},
                timeout=25,
            )
            r.raise_for_status()
            j = r.json()
            if j.get("IsErroredOnProcessing"):
                logger.warning("OCR.space(%s) 處理錯誤：%s", lang, j.get("ErrorMessage"))
                continue
            for p in j.get("ParsedResults", []):
                t = p.get("ParsedText", "")
                if t:
                    texts.append(t)
        except Exception as e:  # noqa: BLE001
            logger.warning("OCR.space(%s) 例外：%s", lang, e)

    # 底部機讀區（MRZ）聚焦：護照 MRZ 固定位於底部，單獨裁切+放大後識別，
    # 可顯著提升「第一行（含英文姓名 GU<<FONG-ZHI）」的讀取率——
    # 這正是之前被誤讀成可見文字 PONG-ZHI 的痛點。
    try:
        img = Image.open(io.BytesIO(payload)).convert("RGB")
        w, h = img.size
        crop = img.crop((0, int(h * 0.60), w, h))   # 底部 40%
        cw, ch = crop.size
        scale = 2.4
        crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=95)
        crop_bytes = buf.getvalue()
        if len(crop_bytes) <= 1_000_000:
            r = requests.post(
                url,
                data={
                    "apikey": key,
                    "language": "eng",
                    "isOverlayRequired": "false",
                    "scale": "true",
                    "OCREngine": "1",
                },
                files={"image": ("mrz.jpg", crop_bytes, "image/jpeg")},
                timeout=25,
            )
            r.raise_for_status()
            j = r.json()
            if not j.get("IsErroredOnProcessing"):
                for p in j.get("ParsedResults", []):
                    t = p.get("ParsedText", "")
                    if t:
                        texts.append(t)
    except Exception as e:  # noqa: BLE001
        logger.warning("OCR.space 底部機讀區聚焦失敗（不影響主流程）：%s", e)

    return "\n".join(texts)
