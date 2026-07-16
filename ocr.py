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
import base64
import logging

import pytesseract
from PIL import Image, ImageFilter, ImageOps

import mrz_parser

logger = logging.getLogger(__name__)

CLOUD_PROVIDER = os.environ.get("OCR_PROVIDER", "none").lower()
MRZ_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"


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


def _parse_from_free_text(text: str) -> dict | None:
    """全頁文字的兜底解析：先找 MRZ 行，再嘗試 regex 撈證件號碼。"""
    lines = _extract_mrz_lines(text)
    if len(lines) >= 2:
        parsed = mrz_parser.parse(lines)
        if parsed:
            return parsed
    m = re.search(
        r"(?:PASSPORT|護照|NO\.?|號碼|DOC(?:UMENT)?\s*NO)[\s:]*([A-Z]{1,3}[0-9]{6,9})",
        text, re.I,
    )
    if m:
        return {"doc_number": m.group(1), "doc_type_guess": "護照"}
    return None


def _extract_chinese_fields(text: str) -> dict | None:
    """從全頁 OCR 文字抽取「無 MRZ 證件」的中文/英文欄位。

    適用對象：港澳通行證(正面)、回鄉證、台胞證、身份證等沒有標準機讀區的證件。
    這些證件照片即使拍正面，雲端 OCR 也能讀出「姓名 / 證件號碼 / 出生」等欄位。

    回傳 parsed dict（部分欄位可能有值），或 None（完全無可擷取欄位）。
    欄位設計與 MRZ 解析器相容：doc_number / date_of_birth / zh_name / en_name。
    """
    res: dict = {}

    # 證件號碼：OCR 常在字母數字間塞空格（如 "C J 9 3 1..."），先去掉內部空白
    collapsed = re.sub(r"(?<=[A-Z0-9])\s+(?=[A-Z0-9])", "", text)

    # 誤判黑名單：證件上常見的英文單字，絕不能當成證件號碼
    _WORD_BLACKLIST = {
        "PASSPORT", "DPASSPORT", "REPUBLIC", "NATIONAL", "IDENTITY",
        "DOCUMENT", "SURNAME", "GENDER", "NATION", "AUTHORITY", "PERMIT",
    }

    def _valid_docnum(cand: str) -> bool:
        """真證件號需含足夠數字；純英文單字（如 PASSPORT）一律拒絕。"""
        if cand.upper() in _WORD_BLACKLIST:
            return False
        digits = sum(c.isdigit() for c in cand)
        return digits >= 4  # 港澳通行證/護照號碼至少含 4 位以上數字

    # 港澳通行證 C+8 位(字母或數字) / 台胞證 / 回鄉證 等
    # 例：CJ9314108（C + J9314108）、C12345678
    doc_cands = re.findall(r"(?<![0-9A-Z])([CEHDAK][A-Z0-9]{8})(?![0-9A-Z])", collapsed)
    doc_cands += re.findall(r"(?<![0-9])([0-9]{8,9})(?![0-9])", collapsed)
    for cand in doc_cands:
        if _valid_docnum(cand):
            res["doc_number"] = cand
            break

    # 中文姓名：找「姓名/名」標籤後的 2~4 個中文字
    nm = re.search(r"(?:姓名|名|Name)[：:\s]*([\u4e00-\u9fff]{2,4})", text, re.I)
    if nm:
        res["zh_name"] = nm.group(1)

    # 英文姓名：WU,JUN / WU，JUN 形態（須有逗號，降低誤判）
    en = re.search(r"\b([A-Z]{2,})[,，]([A-Z]{1,}(?:\s[A-Z]+)?)", text)
    if en:
        res["en_name"] = f"{en.group(1)},{en.group(2).strip()}"

    # 出生日期：出生/出生日期/Birth + 1981.07.08 / 1981年07月08日 / 1981-07-08
    dm = re.search(
        r"(?:出生|出生日期|Birth|DOB)[：:\s]*"
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

    if not res:
        return None
    res["doc_type_guess"] = "證件(無MRZ)"
    return res


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
            parsed = _parse_from_free_text(cloud_text)
            if parsed:
                return {
                    "parsed": parsed,
                    "confidence": "medium",
                    "source": f"cloud_{_cloud_providers[0]}",
                    "raw": cloud_text,
                }
            # 無 MRZ：嘗試中文/非 MRZ 證件欄位（港澳通行證正面等）
            cf = _extract_chinese_fields(cloud_text)
            if cf:
                return {
                    "parsed": cf,
                    "confidence": "medium",
                    "source": f"cloud_{_cloud_providers[0]}_text",
                    "raw": cloud_text,
                    "no_mrz": True,
                }
    elif CLOUD_PROVIDER not in ("none", "", "false"):
        logger.warning("OCR_PROVIDER=%s 但缺少對應金鑰，雲端備援未啟用", CLOUD_PROVIDER)

    # 3) 本地全頁文字兜底（無白名單）撈證件號碼
    try:
        full = pytesseract.image_to_string(
            Image.open(io.BytesIO(image_bytes)).convert("L"), config="--psm 6"
        )
    except Exception:  # noqa: BLE001
        full = ""
    parsed = _parse_from_free_text(full)
    if parsed:
        return {"parsed": parsed, "confidence": "low", "source": "local_text", "raw": full}

    # 本地也嘗試中文欄位（Tesseract 雖弱，偶能撈到證件號碼/中文姓名）
    cf = _extract_chinese_fields(full)
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
