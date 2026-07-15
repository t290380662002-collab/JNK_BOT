FROM python:3.11-slim

WORKDIR /app

# 安裝 Tesseract OCR 引擎（本地 MRZ 辨識用）
# 注意：slim 映像 + --no-install-recommends 不會自動帶 eng 語言包，
# 必須顯式安裝 tesseract-ocr-eng，否則 pytesseract 會報「Error opening data file」。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render 會注入 PORT 環境變數；webhook 模式由 WEBHOOK_URL 控制
CMD ["python", "bot.py"]
