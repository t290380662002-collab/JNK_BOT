# 證件掃描 Telegram Bot（docscan-bot）

傳送「護照 / 港澳通行證 / 回鄉證 / 台胞證」照片，Bot 自動辨識機讀區（MRZ），
標記證件類型後，一鍵產生 Excel 發回 Telegram。

- **部署**：Render（Docker）
- **辨識**：本地 Tesseract 讀 MRZ 為主，可選 Google Vision / Azure 雲端 OCR 備援
- **輸出**：直接產生 `.xlsx` 發送，不落資料庫

---

## 目錄結構

```
docscan-bot/
├── bot.py            # Telegram Bot 主程式（webhook / polling 雙模式）
├── ocr.py            # OCR 混合管線（Tesseract + 雲端備援）
├── mrz_parser.py     # 純 Python MRZ 解析（TD3 護照 / TD1 卡式）
├── excel_writer.py   # openpyxl 產生 Excel
├── test_mrz.py       # MRZ 解析單元測試
├── test_ocr_pipeline.py # OCR→Excel 端到端測試（mock）
├── requirements.txt
├── Dockerfile
├── render.yaml       # Render 部署設定
└── .env.example
```

---

## 〇、推上 GitHub（連接 Render 前必做）

本專案已是獨立 Git 倉庫（`.env` 已被 `.gitignore` / `.dockerignore` 排除，不會外洩）。

```bash
# 在 docscan-bot/ 目錄下執行（已 git init 並完成首次 commit）
git remote add origin git@github.com:<你的帳號>/docscan-bot.git
git branch -M main
git push -u origin main
```

> 若用 HTTPS：把上面 `git@github.com:...` 改成
> `https://github.com/<你的帳號>/docscan-bot.git`。
> 推送前請確認 `git status` 沒有 `.env`（`git ls-files | grep .env` 應無輸出）。

推完後，到 Render 建 Web Service 選 **Connect a repository** → 選這個 repo 即可。

---

## 一、取得 Telegram Bot Token

1. 在 Telegram 搜尋 `@BotFather`，發送 `/newbot`
2. 按提示取名稱與 username，取得 `TELEGRAM_BOT_TOKEN`（格式 `123456789:xxxx`）

---

## 二、本地測試

### 方式 A：Docker（與 Render 環境一致，推薦）
```bash
cp .env.example .env        # 填入 TELEGRAM_BOT_TOKEN
docker build -t docscan-bot .
docker run --env-file .env -p 10000:10000 docscan-bot
```
Bot 會以 polling 模式運行（`.env` 不留 `WEBHOOK_URL`）。

### 方式 B：本機 venv（需自行安裝 Tesseract）
```bash
pip install -r requirements.txt
# Windows 另裝 Tesseract：https://github.com/UB-Mannheim/tesseract/wiki
python test_mrz.py            # 驗證 MRZ 解析
python test_ocr_pipeline.py   # 驗證 OCR→Excel 管線
```

---

## 三、部署到 Render

1. 在 Render 新建 **Web Service**，連接本專案 Git 倉庫
2. 執行方式選 **Docker**（已內含 `Dockerfile`，會自動裝好 Tesseract）
3. 在 Environment 設定以下變數：
   - `TELEGRAM_BOT_TOKEN`：BotFather 取得的 token
   - `WEBHOOK_URL`：Render 提供的網址，例如 `https://docscan-bot.onrender.com`
   - `OCR_PROVIDER`：`none`（預設，免費）／`google`／`azure`
4. 部署完成後，Bot 會以 webhook 模式收訊

> Render 免費方案閒置會休眠，首次接收訊息可能延遲數秒；可用 UptimeRobot 每 5–10 分鐘 ping 一次 `WEBHOOK_URL` 保持喚醒。

---

## 四、使用方式

| 指令 / 操作 | 說明 |
|---|---|
| 傳送照片 | 辨識護照 / 通行證，回傳解析結果 |
| 選擇證件類型（Inline 按鈕） | 標記為 護照 / 港澳通行證 / 回鄉證 / 台胞證 |
| `/export` | 產生並發送 Excel（含目前所有暫存筆數） |
| `/list` | 查看已掃描筆數與摘要 |
| `/clear` | 清空暫存 |

Excel 欄位：證件類型、英文姓、英文名、證件號碼、國籍/地區、出生日期、性別、有效期限、發證代碼、掃描時間。

---

## 五、雲端 OCR 備援（選用）

本地 MRZ 為主，若照片模糊導致 MRZ 讀不到，可啟用雲端 OCR 做全頁辨識：

- `OCR_PROVIDER=google` + `GOOGLE_VISION_API_KEY`
- `OCR_PROVIDER=azure` + `AZURE_VISION_ENDPOINT` + `AZURE_VISION_KEY`

---

## 六、支援證件與限制

| 證件 | 格式 | 備註 |
|---|---|---|
| 護照 | TD3（兩行 44 字） | 各國通用，準確度最高 |
| 港澳通行證 | TD1 卡式 | 需使用者選類型 |
| 回鄉證 | TD1 卡式 | 需使用者選類型 |
| 台胞證 | TD1 卡式 | 需使用者選類型 |

**限制**
- 內地身份證（無 MRZ）未支援，需改 OCR 全頁，準確度較低，暫未實作。
- 卡式證件因各家 MRZ 校驗位演算法不同，解析以「欄位抽取」為主，不強制校驗位驗證。
- 辨識品質取決於拍照：光線充足、證件攤平、底部機讀碼清晰。
- 護照含個人資料，請注意隱私與資料保存合規。
