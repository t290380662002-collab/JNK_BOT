FROM python:3.11-slim

WORKDIR /app

# 安裝 Tesseract OCR 引擎（本地 MRZ 辨識用）
# 注意：slim 映像 + --no-install-recommends 不會自動帶 eng 語言包，
# 必須顯式安裝 tesseract-ocr-eng，否則 pytesseract 會報「Error opening data file」。
# 加裝 chi_tra（繁體中文）語言包，讓本地 OCR 也能讀中文姓名/欄位（不依賴雲端）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render 會注入 PORT 環境變數；webhook 模式由 WEBHOOK_URL 控制
CMD ["python", "bot.py"]
