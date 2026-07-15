"""
OCR 混合管線：
  1) 本地 Tesseract 抽取 MRZ 機讀區（免費、離線）
  2) 失敗時呼叫雲端 OCR（Google Vision / Azure）做全頁辨識備援
  3) 最後用自由文字 regex 兜底抽取欄位
由環境變數 OCR_PROVIDER 控制是否啟用雲端備援。
"""
import os
import re
import io
import base64
import logging

import pytesseract
from PIL import Image

import mrz_parser

logger = logging.getLogger(__name__)

CLOUD_PROVIDER = os.environ.get("OCR_PROVIDER", "none").lower()
TESSERACT_CONFIG = "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"


def _preprocess(img: Image.Image) -> Image.Image:
    img = img.convert("L")  # 灰階
    w, h = img.size
    if max(w, h) < 1100:  # 太小就放大，提升 OCR 準確度
        scale = 1100 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)))
    return img


def _extract_mrz_lines(text: str):
    """從 OCR 文字中挑出 MRZ 候選行：含 '<' 且長度 >= 25。"""
    lines = []
    for raw in text.splitlines():
        cleaned = mrz_parser._clean(raw)
        if "<" in cleaned and len(cleaned) >= 25:
            lines.append(cleaned)
    return lines


def _parse_from_free_text(text: str):
    """雲端 OCR 全頁文字的兜底解析：先找 MRZ 行，再嘗試 regex。"""
    lines = _extract_mrz_lines(text)
    if len(lines) >= 2:
        parsed = mrz_parser.parse(lines)
        if parsed:
            return parsed
    # 找不到標準 MRZ，嘗試從自由文字撈證件號碼
    m = re.search(r"(?:PASSPORT|護照|NO\.?|號碼)[\s:]*([A-Z]{1,3}[0-9]{6,9})", text, re.I)
    if m:
        return {"doc_number": m.group(1), "doc_type_guess": "護照"}
    return None


def process_image(image_bytes: bytes) -> dict:
    """
    回傳 dict：
      { "parsed": {...}, "confidence": str, "source": str, "raw": str }
    或 None（完全無法辨識）。
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = _preprocess(img)

    text = pytesseract.image_to_string(img, config=TESSERACT_CONFIG)
    mrz_lines = _extract_mrz_lines(text)
    logger.info("本地 MRZ 候選行數：%d", len(mrz_lines))

    if len(mrz_lines) >= 2:
        parsed = mrz_parser.parse(mrz_lines)
        if parsed:
            return {
                "parsed": parsed,
                "confidence": "high",
                "source": "local_mrz",
                "raw": text,
            }

    # 本地 MRZ 失敗 -> 雲端 OCR 備援
    if CLOUD_PROVIDER in ("google", "azure"):
        cloud_text = _cloud_ocr(image_bytes)
        if cloud_text:
            parsed = _parse_from_free_text(cloud_text)
            if parsed:
                return {
                    "parsed": parsed,
                    "confidence": "medium",
                    "source": f"cloud_{CLOUD_PROVIDER}",
                    "raw": cloud_text,
                }

    # 最後兜底：用本地文字直接試一次自由文字解析
    parsed = _parse_from_free_text(text)
    if parsed:
        return {"parsed": parsed, "confidence": "low", "source": "local_text", "raw": text}

    return None


# ---------------------------------------------------------------------------
# 雲端 OCR 備援
# ---------------------------------------------------------------------------
def _cloud_ocr(image_bytes: bytes) -> str:
    if CLOUD_PROVIDER == "google":
        return _google_vision(image_bytes)
    if CLOUD_PROVIDER == "azure":
        return _azure_ocr(image_bytes)
    return ""


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
