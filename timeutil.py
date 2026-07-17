# -*- coding: utf-8 -*-
"""統一使用「台灣時間（UTC+8）」產生所有對外時間戳記。

Render 部署環境的系統時區為 UTC，若直接用 datetime.now() 會比台灣慢 8 小時，
導致檔名時間戳、掃描時間、表單登記日期都錯誤。本模組集中提供 taipei_now()，
所有需要「現在時間」的地方都改呼叫它，確保與台灣本地一致。
"""
from datetime import datetime, timezone, timedelta

# 台灣時區 = UTC+8（不依賴 tzdata 資料庫，直接以固定偏移實作，部署環境最穩）
TAIWAN_TZ = timezone(timedelta(hours=8))


def taipei_now() -> datetime:
    """回傳目前的台灣時間（UTC+8，aware datetime）。

    回傳值為 aware datetime，可直接呼叫 .year / .date() / .strftime() 等。
    """
    return datetime.now(TAIWAN_TZ)
