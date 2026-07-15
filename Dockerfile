FROM python:3.11-slim

WORKDIR /app

# 安裝 Tesseract OCR 引擎（本地 MRZ 辨識用）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render 會注入 PORT 環境變數；webhook 模式由 WEBHOOK_URL 控制
CMD ["python", "bot.py"]
