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
import text_booking

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


def format_passport_text(r: dict) -> str:
    """把掃描結果轉成「讀取證件完畢 回傳文字」格式，可直接貼進 /book 訊息。"""
    p = r.get("parsed", {})
    zh = p.get("zh_name") or "（未提供）"
    last = (p.get("last_name") or "").strip().upper()
    first = (p.get("first_name") or "").strip().upper()
    if last and first:
        en = f"{last}，{first}"
    else:
        en = last or "（未提供）"
    dob = p.get("date_of_birth") or "（未提供）"
    doc = p.get("doc_number") or "（未提供）"
    return (
        "讀取證件完畢，回傳文字：\n"
        f"入住者中文：{zh}\n"
        f"入住者英文：{en}\n"
        f"出生年月日：{dob}\n"
        f"證件號碼：{doc}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📸 證件掃描 Bot\n\n"
        "直接傳送「護照 / 港澳通行證 / 回鄉證 / 台胞證」照片，"
        "我會自動辨識機讀區並填入訂房單。\n\n"
        "支援酒店訂房單：\n"
        "  名匯 / 威尼斯 / 巴黎人 / 倫敦人 / 御園 / 康萊德\n"
        "（自動填英文姓名、證件號碼、出生日期；中文姓名與房型請手動補）\n\n"
        "指令：\n"
        "  /book    ─ 以文字下訂（貼上入住/退房/飯店/姓名等）\n"
        "  /export  ─ 選酒店並產生訂房單 Excel（掃描證件後）\n"
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
            "⚠️ 無法從這張照片辨識出機讀碼（MRZ）。\n"
            "請重拍：光線充足、證件攤平、底部機讀碼清晰對焦；\n"
            "或改用 /book 直接輸入資料。"
        )
        return

    sess = get_session(chat_id)
    key = uuid.uuid4().hex
    sess["pending"][key] = result

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"type|{key}|{t}") for t in DOC_TYPES]]
    )
    await update.message.reply_text(
        fmt_result(result)
        + "\n\n"
        + format_passport_text(result)
        + "\n\n請選擇證件類型：",
        reply_markup=kb,
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


def _hotel_keyboard(prefix: str = "hotel") -> InlineKeyboardMarkup:
    """六酒店（+ 通用格式）的 inline 鍵盤。prefix 區分掃描匯出/文字下訂。"""
    rows = []
    row = []
    for i, key in enumerate(HT.HOTEL_ORDER, 1):
        row.append(InlineKeyboardButton(HT.HOTELS[key]["name"], callback_data=f"{prefix}|{key}"))
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # 通用格式僅掃描匯出適用（文字下訂無 parsed 資料）
    if prefix == "hotel":
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


# ---------------------------------------------------------------------------
# 文字下訂（/book）：解析文字訂房 -> 填表 -> 發 Excel
# ---------------------------------------------------------------------------
BOOKING_KEYWORDS = ["入住", "退房", "飯店", "酒店", "訂房", "房型",
                    "件數", "房數", "人數", "姓名", "微信", "吸煙", "抽煙"]


def _looks_like_booking(text: str) -> bool:
    return any(k in text for k in BOOKING_KEYWORDS)


def _manual_summary(booking: dict, hotel_key: str) -> str:
    cfg = HT.HOTELS[hotel_key]
    name = cfg["name"].split(" ")[0]
    guests = booking.get("guests") or []
    g0 = guests[0] if guests else {}

    def _gname(g):
        return g.get("zh_name") or g.get("en_name") or "（未提供姓名）"

    names = "、".join(_gname(g) for g in guests) if guests else "（未提供姓名）"
    ci = booking.get("check_in") or "?"
    co = booking.get("check_out") or "?"
    rc = booking.get("room_count") or 1
    pax = booking.get("pax") or len(guests) or 1

    lines = [
        f"✅ 已產生「{name}」訂房單（文字下訂）",
        f"客人：{names}（{len(guests) or '?'} 位）",
        f"入住 {ci} / 退房 {co}｜房數 {rc}｜人數 {pax}",
    ]

    # 依實際有無護照資料，動態列出已填 / 未填
    filled = ["中文姓名", "入住", "退房", "房數", "人數"]
    unfilled = []
    if g0.get("en_name"):
        filled.append("英文姓名")
    else:
        unfilled.append("英文姓名")
    if g0.get("doc_number"):
        filled.append("證件號碼")
    else:
        unfilled.append("證件號碼")
    if g0.get("dob"):
        filled.append("出生日期")
    else:
        unfilled.append("出生日期")
    unfilled.append("房型（依指示不填）")

    lines.append("已填：" + "、".join(filled))
    lines.append("未填（手動補）：" + "、".join(unfilled))

    smk = booking.get("smoking")
    if smk is True:
        lines.append("🚬 抽煙：已填入清單表吸煙欄" if cfg.get("list_smoking_col")
                     else "⚠️ 抽煙：本模板無吸煙欄，已記錄但無法填入，請手寫標註")
    elif smk is False:
        lines.append("🚭 禁煙：已填入清單表吸煙欄" if cfg.get("list_smoking_col")
                     else "ℹ️ 禁煙：本模板無吸煙欄")

    if booking.get("booker"):
        lines.append(f"📇 訂房人：{booking['booker']}（本表無訂房人欄，僅作記錄）")
    return "\n".join(lines)


async def book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 文字下訂\n\n"
        "直接把訂房資料貼給我，格式例如：\n"
        "入住：7/21\n退房：7/23\n飯店：倫敦人\n"
        "房型：維多利亞房大床\n件數：1\n是否吸煙：抽煙\n"
        "江-泰哥-呂布\n微信：泰哥服務群\n\n"
        "我會自動解析並產出該酒店訂房單。\n"
        "英文姓名 / 證件號碼 / 出生日期（無護照時）與房型 留空手動補；"
        "抽煙僅名匯模板有欄位會自動填，其餘酒店會提醒手寫。"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not _looks_like_booking(text):
        await update.message.reply_text(
            "請傳送證件『照片』，或用 /book 文字下訂、/export、/list、/clear 指令。"
        )
        return
    booking = text_booking.parse(text)
    await process_manual_booking(update.message.chat_id, context, booking, update)


async def process_manual_booking(chat_id, context, booking, update):
    hotel_key = booking.get("hotel")
    if not hotel_key:
        sess = get_session(chat_id)
        sess["pending_booking"] = booking
        await context.bot.send_message(
            chat_id,
            "未偵測到飯店，請選擇要填入的酒店訂房單：",
            reply_markup=_hotel_keyboard("bookhotel"),
        )
        return
    await _do_fill_manual(chat_id, context, hotel_key, booking, update)


async def _do_fill_manual(chat_id, context, hotel_key, booking, update):
    cfg = HT.HOTELS.get(hotel_key, {})
    label = cfg.get("name", hotel_key)
    msg = update.effective_message if update else None
    if msg:
        await msg.reply_text(f"⏳ 正在產生「{label.split(' ')[0]}」訂房單…")
    try:
        path = await asyncio.to_thread(template_filler.fill_manual, hotel_key, booking)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zh = label.split(" ")[0]
        fname = f"{zh}_文字_{ts}.xlsx"
    except Exception as e:  # noqa: BLE001
        logger.exception("文字訂房產生失敗")
        if msg:
            await msg.reply_text(f"❌ 產生失敗：{e}")
        return
    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename=fname)
    try:
        os.remove(path)
    except OSError:
        pass
    await context.bot.send_message(chat_id, _manual_summary(booking, hotel_key))


async def bookhotel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, key = q.data.split("|", 1)
    chat_id = q.message.chat_id
    sess = get_session(chat_id)
    booking = sess.pop("pending_booking", None)
    if not booking:
        await q.edit_message_text("找不到待處理的訂房資料，請重新傳送。")
        return
    cfg = HT.HOTELS.get(key, {})
    label = cfg.get("name", key)
    await q.edit_message_text(f"⏳ 正在產生「{label.split(' ')[0]}」訂房單…")
    try:
        path = await asyncio.to_thread(template_filler.fill_manual, key, booking)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zh = label.split(" ")[0]
        fname = f"{zh}_文字_{ts}.xlsx"
    except Exception as e:  # noqa: BLE001
        logger.exception("文字訂房產生失敗")
        await q.edit_message_text(f"❌ 產生失敗：{e}")
        return
    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename=fname)
    try:
        os.remove(path)
    except OSError:
        pass
    await context.bot.send_message(chat_id, _manual_summary(booking, key))


async def _run_webhook_server(app: Application, base: str):
    """自建 aiohttp 伺服器：同時處理 Telegram webhook 與健康檢查。
    自癒 + 可診斷設計：
      · HTTP 伺服器先啟動（GET / 即回 200，Render 不判部署失敗）。
      · PTB 初始化/啟動搬進背景 ensure_ready 任務，不阻塞 HTTP。
      · ensure_ready 重試 app.initialize()/app.start()（40s 超時防卡死）
        與 set_webhook，並每 30 秒 getWebhookInfo 自檢：webhook 網址不符就補設——
        徹底免疫「舊容器關閉刪 webhook」「冷啟動 set_webhook 偶敗」「start 卡死」。
      · GET / 與 /health 回傳就緒狀態與最後錯誤，便於遠端診斷。
      · 絕不在 finally 刪除 webhook。"""
    port = int(os.environ.get("PORT", "10000"))
    url = base.rstrip("/") + WEBHOOK_PATH

    # 跨閉包共享的就緒狀態（dict 讓 handler 與背景任務讀寫同一物件）
    diag = {"ready": False, "error": None, "webhook_url": None}

    async def handle_webhook(request: web.Request):
        if WEBHOOK_SECRET and \
           request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        if not diag["ready"]:
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
        return web.json_response({
            "status": "ok",
            "ready": diag["ready"],
            "webhook_url": diag["webhook_url"],
            "error": str(diag["error"]) if diag["error"] else None,
        })

    aio_app = web.Application()
    aio_app.router.add_post(WEBHOOK_PATH, handle_webhook)
    aio_app.router.add_get("/", handle_health)
    aio_app.router.add_get("/health", handle_health)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP 服務啟動於 0.0.0.0:%s (health=/, webhook=%s)", port, WEBHOOK_PATH)

    async def ensure_ready():
        """背景自癒任務：確保 PTB 啟動 + webhook 設定好；失敗重試；定時自檢補設。"""
        initialized = False
        started = False
        while True:
            try:
                if not initialized:
                    await app.initialize()
                    initialized = True
                    logger.info("PTB initialize 完成")
                if not started:
                    try:
                        await asyncio.wait_for(app.start(), timeout=40)
                    except asyncio.TimeoutError:
                        logger.warning("app.start() 超時（40s），將重試")
                        raise
                    started = True
                    diag["ready"] = True
                    diag["error"] = None
                    logger.info("PTB start 完成，handlers 已就緒")
                # webhook 自檢 + 補設
                try:
                    info = await app.bot.get_webhook_info()
                    diag["webhook_url"] = info.url or None
                    if info.url != url:
                        logger.warning("webhook 未註冊或網址不符（%s），重新設定", info.url)
                        await app.bot.set_webhook(
                            url=url, secret_token=WEBHOOK_SECRET,
                            drop_pending_updates=True, max_connections=50,
                        )
                        diag["webhook_url"] = url
                        logger.info("webhook 已設定：%s", url)
                except Exception as e:  # noqa: BLE001
                    logger.warning("webhook 自檢/設定失敗：%s", e)
            except Exception as e:  # noqa: BLE001
                diag["error"] = e
                diag["ready"] = False
                logger.exception("PTB 啟動失敗，將於 30 秒後重試：%s", e)
            await asyncio.sleep(30)

    # 背景啟動自癒任務（不阻塞 HTTP 伺服器；start() 暫時失敗時 GET / 仍可看到狀態）
    _ready_task = asyncio.create_task(ensure_ready())

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        # 注意：刻意不呼叫 delete_webhook()，讓 webhook 在容器重啟間持久存在
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
    app.add_handler(CommandHandler("book", book_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(type_callback, pattern=r"^type\|"))
    app.add_handler(CallbackQueryHandler(hotel_callback, pattern=r"^hotel\|"))
    app.add_handler(CallbackQueryHandler(bookhotel_callback, pattern=r"^bookhotel\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
