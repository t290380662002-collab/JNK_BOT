"""
Telegram 證件掃描 Bot（部署於 Render，Docker 運行）
流程：傳照片 -> 本地 MRZ 辨識 -> Inline 選證件類型 -> 暫存
      -> /export 產生 Excel 發回 -> /list 查看 -> /clear 清空
"""
import io
import logging
import os
import uuid
from datetime import datetime

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

import ocr
import excel_writer

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
        "我會自動辨識機讀區並填入 Excel。\n\n"
        "指令：\n"
        "  /export  ─ 產生並發送 Excel\n"
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


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.message.chat_id)
    if not sess["records"]:
        await update.message.reply_text("尚無資料，先傳照片吧。")
        return
    path = excel_writer.build(sess["records"])
    with open(path, "rb") as f:
        await update.message.reply_document(
            document=f, filename=os.path.basename(path)
        )
    os.remove(path)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(update.message.chat_id)
    sess["records"].clear()
    sess["pending"].clear()
    await update.message.reply_text("🧹 已清空暫存。")


async def ignore_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("請傳送證件『照片』，或用 /export、/list、/clear 指令。")


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(type_callback, pattern=r"^type\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ignore_text))

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        port = int(os.environ.get("PORT", "10000"))
        webhook_path = f"/{TOKEN}"
        logger.info("以 webhook 模式啟動：%s%s", webhook_url, webhook_path)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url.rstrip("/"),
            webhook_path=webhook_path,
        )
    else:
        logger.info("以 polling 模式啟動（本地測試）")
        app.run_polling()


if __name__ == "__main__":
    main()
