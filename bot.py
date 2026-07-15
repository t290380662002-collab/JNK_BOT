"""
Telegram 證件掃描 Bot（部署於 Render，Docker 運行）
流程：傳照片 -> 本地 MRZ 辨識 -> Inline 選證件類型 -> 暫存
      -> /export 產生 Excel 發回 -> /list 查看 -> /clear 清空
"""
import asyncio
import io
import logging
import os
import uuid
from datetime import datetime

from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Webhook 模式設定（Render Web Service 用）
#   · 自建 aiohttp 伺服器，同時處理 POST /webhook（收 Telegram 推播）
#     與 GET /（健康檢查，啟動即回 200，避免 Render 判部署失敗）
#   · 健康檢查端點最先可用，webhook 設定失敗也不會 crash 整個服務
#   · 同一隻 Bot 只會有一組 webhook，從根本杜絕 polling 多實例 409
# ---------------------------------------------------------------------------
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "docscan-2026-secret")


def _webhook_base_url() -> str | None:
    """回傳 webhook 基底網址（不含路徑）；無則回 None（退回 polling）。"""
    return os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or None

import ocr
import excel_writer
import hotel_templates as HT
import template_filler

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DOC_TYPES = ["護照", "港澳通行證", "回鄉證", "台胞證"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# chat_id -> {"records": [...], "pending": {key: result}}
sessions: dict = {}


def get_session(chat_id: int) -> dict:
    return sessions.setdefault(chat_id, {"records": [], "pending": {}})


def fmt_result(r: dict) -> str:
    p = r.get("parsed", {})
    lines = [f"✅ 辨識完成（信心：{r.get('confidence')}｜來源：{r.get('source')}）"]
    label_map = {
        "last_name": "英文姓",
        "first_name": "英文名",
        "doc_number": "證件號碼",
        "nationality": "國籍/地區",
        "date_of_birth": "出生日期",
        "sex": "性別",
        "expiry_date": "有效期限",
        "issuer": "發證代碼",
    }
    for k, label in label_map.items():
        if p.get(k):
            lines.append(f"• {label}：{p[k]}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📸 證件掃描 Bot\n\n"
        "直接傳送「護照 / 港澳通行證 / 回鄉證 / 台胞證」照片，"
        "我會自動辨識機讀區並填入訂房單。\n\n"
        "支援酒店訂房單：\n"
        "  名匯 / 威尼斯 / 巴黎人 / 倫敦人 / 御園 / 康萊德\n"
        "（自動填英文姓名、證件號碼、出生日期；中文姓名與房型請手動補）\n\n"
        "指令：\n"
        "  /export  ─ 選酒店並產生訂房單 Excel\n"
        "  /list    ─ 查看已掃描筆數與摘要\n"
        "  /clear   ─ 清空目前暫存\n"
        "  /help    ─ 說明"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text("🔍 辨識中…")

    photo = update.message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await tg_file.download_to_memory(bio)
        data = bio.getvalue()
        result = ocr.process_image(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("OCR 處理失敗")
        await update.message.reply_text(f"❌ 辨識失敗：{e}")
        return

    if not result or not result.get("parsed"):
        await update.message.reply_text(
            "⚠️ 未偵測到 MRZ 機讀區，雲端 OCR 也無法解析。\n"
            "請重拍（光線充足、證件攤平、底部機讀碼清晰）或改用手動輸入。"
        )
        return

    sess = get_session(chat_id)
    key = uuid.uuid4().hex
    sess["pending"][key] = result

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"type|{key}|{t}") for t in DOC_TYPES]]
    )
    await update.message.reply_text(
        fmt_result(result) + "\n\n請選擇證件類型：", reply_markup=kb
    )


async def type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, key, dtype = q.data.split("|")
    chat_id = q.message.chat_id
    sess = get_session(chat_id)
    rec = sess["pending"].pop(key, None)
    if not rec:
        await q.edit_message_text(q.message.text + "\n（此筆已處理）")
        return

    rec["doc_type"] = dtype
    rec["scan_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sess["records"].append(rec)
    await q.edit_message_text(
        q.message.text.split("\n\n請選擇")[0]
        + f"\n\n➡️ 已標記為：{dtype}（目前共 {len(sess['records'])} 筆）"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.message.chat_id)
    recs = sess["records"]
    if not recs:
        await update.message.reply_text("尚無資料，先傳照片吧。")
        return
    lines = [f"📋 目前共 {len(recs)} 筆："]
    for i, r in enumerate(recs, 1):
        p = r["parsed"]
        name = f"{p.get('last_name','')} {p.get('first_name','')}".strip()
        lines.append(f"{i}. [{r['doc_type']}] {name} | {p.get('doc_number','')}")
    await update.message.reply_text("\n".join(lines))


def _hotel_keyboard() -> InlineKeyboardMarkup:
    """六酒店 + 通用格式 的 inline 鍵盤。"""
    rows = []
    row = []
    for i, key in enumerate(HT.HOTEL_ORDER, 1):
        row.append(InlineKeyboardButton(HT.HOTELS[key]["name"], callback_data=f"hotel|{key}"))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📄 通用格式 (Generic)", callback_data="hotel|generic")])
    return InlineKeyboardMarkup(rows)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.message.chat_id)
    if not sess["records"]:
        await update.message.reply_text("尚無資料，先傳照片吧。")
        return
    await update.message.reply_text(
        f"目前共 {len(sess['records'])} 筆。請選擇要匯出的酒店訂房單格式：",
        reply_markup=_hotel_keyboard(),
    )


async def hotel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, key = q.data.split("|", 1)
    chat_id = q.message.chat_id
    sess = get_session(chat_id)
    if not sess["records"]:
        await q.edit_message_text("尚無資料，先傳照片吧。")
        return

    label = "通用格式" if key == "generic" else HT.HOTELS.get(key, {}).get("name", key)
    await q.edit_message_text(f"⏳ 正在產生「{label}」訂房單…")

    try:
        if key == "generic":
            path = await asyncio.to_thread(excel_writer.build, sess["records"])
            fname = os.path.basename(path)
        else:
            path = await asyncio.to_thread(template_filler.fill, key, sess["records"])
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            zh = HT.HOTELS[key]["name"].split(" ")[0]
            fname = f"{zh}_{ts}.xlsx"
    except Exception as e:  # noqa: BLE001
        logger.exception("匯出失敗")
        await context.bot.send_message(chat_id, f"❌ 產生失敗：{e}")
        return

    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename=fname)
    try:
        os.remove(path)
    except OSError:
        pass

    n = len(sess["records"])
    note = "（中文姓名、房型請自行手動補上）" if key != "generic" else ""
    await context.bot.send_message(
        chat_id,
        f"✅ 已匯出「{label}」，共 {n} 位客人{note}",
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.message.chat_id)
    sess["records"].clear()
    sess["pending"].clear()
    await update.message.reply_text("🧹 已清空暫存。")


async def ignore_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("請傳送證件『照片』，或用 /export、/list、/clear 指令。")


async def _run_webhook_server(app: Application, base: str):
    """自建 aiohttp 伺服器：同時處理 Telegram webhook 與健康檢查。
    啟動順序：HTTP 伺服器 -> PTB 初始化 -> webhook 設定。
    確保健康檢查端點最先可用，避免 Render 判部署失敗。
    webhook 設定失敗時只記 log、不 raise，服務持續存活。"""
    port = int(os.environ.get("PORT", "10000"))
    url = base.rstrip("/") + WEBHOOK_PATH
    _ptb_ready = False  # 標記 PTB 是否已初始化完成

    async def handle_webhook(request: web.Request):
        if WEBHOOK_SECRET and \
           request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        if not _ptb_ready:
            return web.Response(status=503, text="not ready")
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        update = Update.de_json(data, app.bot)
        logger.info("handle_webhook: update_id=%s", update.update_id)
        try:
            await app.process_update(update)
        except Exception:
            logger.exception("process_update 失敗")
        return web.Response(text="ok")

    async def handle_health(request: web.Request):
        return web.Response(text="ok")

    aio_app = web.Application()
    aio_app.router.add_post(WEBHOOK_PATH, handle_webhook)
    aio_app.router.add_get("/", handle_health)
    aio_app.router.add_get("/health", handle_health)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP 服務啟動於 0.0.0.0:%s (health=/, webhook=%s)", port, WEBHOOK_PATH)

    # --- HTTP 伺服器已啟動，現在初始化 PTB 並設定 webhook ---
    try:
        await app.initialize()
        await app.start()
        await app.bot.set_webhook(
            url=url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True
        )
        _ptb_ready = True
        logger.info("PTB 初始化完成，webhook 已設定：%s", url)
    except Exception:
        logger.exception("PTB 初始化或 webhook 設定失敗（服務仍持續存活）")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await app.bot.delete_webhook()
        except Exception:
            pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        await runner.cleanup()


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(type_callback, pattern=r"^type\|"))
    app.add_handler(CallbackQueryHandler(hotel_callback, pattern=r"^hotel\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ignore_text))

    base = _webhook_base_url()
    logger.info(
        "啟動診斷: RENDER_EXTERNAL_URL=%s WEBHOOK_URL=%s PORT=%s",
        os.environ.get("RENDER_EXTERNAL_URL"),
        os.environ.get("WEBHOOK_URL"),
        os.environ.get("PORT"),
    )

    if base:
        # ---- Webhook 模式（Render Web Service）----
        logger.info(
            "Bot 啟動中 (webhook) -> %s%s | PORT=%s",
            base.rstrip("/"), WEBHOOK_PATH, os.environ.get("PORT"),
        )
        try:
            asyncio.run(_run_webhook_server(app, base))
        except Exception as e:
            logger.exception("webhook 服務啟動失敗：%s", e)
            raise
    else:
        # ---- Polling 模式（本機開發備援）----
        logger.info("Bot 啟動中 (polling)...")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
