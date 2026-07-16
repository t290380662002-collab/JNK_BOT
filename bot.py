"""
Telegram 證件掃描 Bot（部署於 Render，Docker 運行）
流程：傳照片 -> 本地 MRZ 辨識 -> Inline 選證件類型 -> 暫存
      -> /export 產生 Excel 發回 -> /list 查看 -> /clear 清空
"""
import asyncio
import io
import logging
import os
import re
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


def _norm_dob(s) -> str:
    """把任意日期字串統一為 YYYY/MM/DD（與 Excel 輸出一致）。
    支援 1982.01.09 / 1982-01-09 / 1982/01/09 / 1982年01月09日 / 1982.1.9 等。"""
    if not isinstance(s, str):
        return s
    m = re.match(
        r"^\s*(\d{4})\s*[./年\-]\s*(\d{1,2})\s*[./月\-]\s*(\d{1,2})\s*日?\s*$",
        s,
    )
    if m:
        return f"{int(m.group(1)):04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    return s


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
        v = p.get(k)
        if v:
            if k in ("date_of_birth", "expiry_date"):
                v = _norm_dob(v)
            lines.append(f"• {label}：{v}")
    if p.get("zh_name"):
        lines.append(f"• 中文姓名：{p['zh_name']}")
    if r.get("no_mrz"):
        lines.append("• 備註：未偵測到 MRZ，欄位由照片文字擷取，請務必核對")
    return "\n".join(lines)


def format_passport_text(r: dict) -> str:
    """把掃描結果轉成「讀取證件完畢 回傳文字」格式，可直接貼進 /book 訊息。"""
    p = r.get("parsed", {})
    zh = p.get("zh_name") or "（未提供）"
    last = (p.get("last_name") or "").strip().upper()
    first = (p.get("first_name") or "").strip().upper()
    if last and first:
        en = f"{last}，{first}"
    elif p.get("en_name"):
        # 退路：en_name 為 "CHUNG,MING-HUNG" 形式，轉成「姓，名」顯示
        en = p["en_name"].replace(",", "，")
    else:
        en = last or "（未提供）"
    dob = p.get("date_of_birth") or "（未提供）"
    # 統一出生日期顯示為 YYYY/MM/DD（與 Excel 輸出一致），
    # 任何來源（MRZ 破折號 / OCR 可見文字的點號 / 中文年月日）都歸一化。
    dob = _norm_dob(dob)
    doc = p.get("doc_number") or "（未提供）"
    return (
        "讀取證件完畢，回傳文字：\n"
        f"入住者中文：{zh}\n"
        f"入住者英文：{en}\n"
        f"出生年月日：{dob}\n"
        f"證件號碼：{doc}"
    )


def _fmt_ci(d):
    """check_in/out（YYYY-MM-DD）-> 顯示字串：今年顯 M/D，跨年顯 YYYY/M/D。"""
    if not d:
        return "?"
    try:
        y, m, day = str(d).split("-")
        if int(y) == datetime.now().year:
            return f"{int(m)}/{int(day)}"
        return f"{y}/{int(m)}/{int(day)}"
    except Exception:
        return str(d)


def format_combined(booking: dict) -> str:
    """產出「訂房 + 證件」合併文字（對應使用者描述的『最後產生』區塊）。"""
    hotel_key = booking.get("hotel")
    hotel_name = (HT.HOTELS.get(hotel_key, {}).get("name", "?").split(" ")[0]
                  if hotel_key else "?")
    lines = [
        f"入住：{_fmt_ci(booking.get('check_in'))}",
        f"退房：{_fmt_ci(booking.get('check_out'))}",
        f"飯店：{hotel_name}",
    ]
    if booking.get("room_type"):
        lines.append(f"房型：{booking['room_type']}")
    lines.append(f"件數：{booking.get('room_count') or 1}")
    smk = booking.get("smoking")
    if smk is True:
        lines.append("是否吸煙：抽煙")
    elif smk is False:
        lines.append("是否吸煙：禁煙")

    g = (booking.get("guests") or [{}])[0]
    lines.append("")
    lines.append(f"入住者中文：{g.get('zh_name') or '（未提供）'}")
    lines.append(f"入住者英文：{g.get('en_name') or '（未提供）'}")
    lines.append(f"出生年月日：{_norm_dob(g.get('dob') or '') or '（未提供）'}")
    lines.append(f"證件號碼：{g.get('doc_number') or '（未提供）'}")

    booker = booking.get("booker")
    if booker:
        line = booker
        if booking.get("wechat"):
            line += f" 微信：{booking['wechat']}"
        lines.append("")
        lines.append(line)
    return "\n".join(lines)


async def _prompt_passport(target, context):
    """提示使用者傳證件照；target 可為 Message 或 CallbackQuery。"""
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏭️ 跳過證件，直接產單", callback_data="skip_passport")]]
    )
    text = (
        "📋 訂房資料已收到。請傳送客人的「證件照片」完成掃描，"
        "我會自動併入這筆訂房；\n若暫無證件，請按「跳過」直接產生訂房單。"
    )
    msg = target.message if hasattr(target, "message") else target
    await msg.reply_text(text, reply_markup=kb)


async def _produce_combined(chat_id, context, update, booking, note=""):
    """產出合併文字 + 填好的訂房單 Excel。"""
    hotel_key = booking.get("hotel")
    if not hotel_key:
        await context.bot.send_message(chat_id, "⚠️ 缺少飯店資訊，無法產生訂房單。")
        return
    text = format_combined(booking)
    if note:
        text += "\n\n" + note
    await context.bot.send_message(chat_id, text)
    # Excel：確保至少一位客人，避免 fill_manual 報「無客人資料」
    if not booking.get("guests"):
        booking["guests"] = [{}]
    try:
        path = await asyncio.to_thread(template_filler.fill_manual, hotel_key, booking)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zh = HT.HOTELS[hotel_key]["name"].split(" ")[0]
        fname = f"{zh}_整合_{ts}.xlsx"
    except Exception as e:  # noqa: BLE001
        logger.exception("整合訂房單產生失敗")
        await context.bot.send_message(chat_id, f"❌ Excel 產生失敗：{e}")
        return
    with open(path, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=f, filename=fname)
    try:
        os.remove(path)
    except OSError:
        pass


def _prepare_skip(booking: dict) -> dict:
    """跳過證件：無 inline 入住者* 時，獨立中文行視為訂房人，入住者留空。"""
    if not booking.get("_has_primary"):
        if not booking.get("booker") and booking.get("guests"):
            booking["booker"] = "、".join(
                g.get("zh_name", "") for g in booking["guests"] if g.get("zh_name")
            )
        booking["guests"] = [{}]
    booking.pop("_has_primary", None)
    return booking


async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/skip：跳過證件掃描，直接以現有訂房資料產單。"""
    chat_id = update.message.chat_id
    sess = get_session(chat_id)
    booking = sess.get("pending_booking")
    if not booking or not sess.get("awaiting_passport"):
        await update.message.reply_text(
            "目前沒有等待證件的訂房。先用 /book 貼上訂房資料。"
        )
        return
    _prepare_skip(booking)
    await _produce_combined(
        chat_id, context, update, booking,
        "（已跳過證件掃描，入住者欄位留空，請手動補）",
    )
    sess.pop("awaiting_passport", None)
    sess.pop("pending_booking", None)


async def skip_passport_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """inline 按鈕「跳過證件」的處理。"""
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    sess = get_session(chat_id)
    booking = sess.get("pending_booking")
    if not booking or not sess.get("awaiting_passport"):
        await q.edit_message_text("目前沒有等待證件的訂房。請先用 /book 貼上訂房資料。")
        return
    _prepare_skip(booking)
    await q.edit_message_text("⏳ 正在產生訂房單（已跳過證件）…")
    await _produce_combined(
        chat_id, context, update, booking,
        "（已跳過證件掃描，入住者欄位留空，請手動補）",
    )
    sess.pop("awaiting_passport", None)
    sess.pop("pending_booking", None)


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
    sess = get_session(chat_id)
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
        # 診斷：雲端 OCR 是否已啟用（有無可用提供者）
        cloud_on = bool(ocr._available_cloud())
        hint = (
            "💡 小提示：\n"
            "• 護照/卡式證件的機讀碼（MRZ）通常在「背面」，請拍背面那兩行字；\n"
            "• 港澳通行證/回鄉證正面（中文面）沒有 MRZ，需靠雲端 OCR 讀取中文欄位——"
            + ("目前已啟用 ✅\n" if cloud_on else "目前尚未啟用雲端 OCR ❌（請先啟用 Google Vision API）\n")
            + "• 若仍困難，最穩的方式是用 /book 直接輸入資料。"
        )
        if sess.get("awaiting_passport"):
            await update.message.reply_text(
                "⚠️ 這張證件沒讀到可用欄位，請重拍；或輸入 /skip 直接產單。\n" + hint
            )
            return
        await update.message.reply_text(
            "⚠️ 無法從這張照片辨識出可用欄位（MRZ 或中文欄位皆未讀到）。\n"
            "請重拍：光線充足、證件攤平、字跡清晰對焦；\n"
            f"{hint}"
        )
        return

    # ===== 整合流程：/book 後等待證件照 =====
    if sess.get("awaiting_passport") and sess.get("pending_booking"):
        booking = sess["pending_booking"]
        text_booking.merge_passport(booking, result)
        note = ""
        if result.get("no_mrz"):
            note = "⚠️ 這張照片沒標準 MRZ，欄位由照片文字擷取，請核對證件號碼/姓名。"
        await _produce_combined(chat_id, context, update, booking, note)
        sess.pop("awaiting_passport", None)
        sess.pop("pending_booking", None)
        return

    # ===== 原本的掃描流程（無訂房上下文，進暫存取 /export）=====
    key = uuid.uuid4().hex
    sess["pending"][key] = result

    note = ""
    if result.get("no_mrz"):
        note = (
            "\n⚠️ 這張照片沒有標準機讀碼（MRZ），我已從照片文字擷取部分欄位。"
            "請務必核對「證件號碼 / 姓名」是否正確，有誤請改以 /book 輸入。"
        )

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"type|{key}|{t}") for t in DOC_TYPES]]
    )
    await update.message.reply_text(
        fmt_result(result)
        + "\n\n"
        + format_passport_text(result)
        + note
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
        lines.append("🚬 抽煙：已填入特別要求欄")
    elif smk is False:
        lines.append("🚭 禁煙：已填入特別要求欄")
    else:
        lines.append("ℹ️ 吸煙：未指定（如需標註請在文字輸入「抽煙」或「禁煙」）")

    if booking.get("booker"):
        lines.append(f"📇 訂房人：{booking['booker']}（本表無訂房人欄，僅作記錄）")
    return "\n".join(lines)


async def book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 文字下訂（整合證件掃描）\n\n"
        "1) 先貼訂房資料，格式例如：\n"
        "入住：7/21\n退房：7/23\n飯店：倫敦人\n"
        "房型：維多利亞房大床\n件數：1\n是否吸煙：抽煙\n"
        "江-泰哥-呂布\n微信：泰哥服務群\n\n"
        "2) 我會請你傳「證件照片」，掃完自動併入這筆訂房，"
        "產出含訂房+證件的最終結果與 Excel。\n"
        "（暫無證件可輸入 /skip 直接產單；房型仍留空手動補）\n\n"
        "英文姓名 / 證件號碼 / 出生日期 由證件掃描自動填入；"
        "吸煙與否會填入「特別要求 Special request」欄。"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.message.chat_id
    sess = get_session(chat_id)
    # 整合流程中：正在等證件照，忽略其他文字（避免重複觸發）
    if sess.get("awaiting_passport"):
        await update.message.reply_text(
            "📸 請傳送客人的「證件照片」完成掃描，或輸入 /skip 跳過證件直接產單。"
        )
        return
    if not _looks_like_booking(text):
        await update.message.reply_text(
            "請傳送證件『照片』，或用 /book 文字下訂、/export、/list、/clear 指令。"
        )
        return
    booking = text_booking.parse(text)
    if not booking.get("hotel"):
        # 飯店不明 -> 先選飯店，選完再進入「待掃證件」狀態
        sess["pending_booking"] = booking
        await update.message.reply_text(
            "未偵測到飯店，請選擇要填入的酒店訂房單：",
            reply_markup=_hotel_keyboard("bookhotel"),
        )
        return
    # 有飯店 -> 進入「待掃證件」狀態，提示傳證件照
    sess["pending_booking"] = booking
    sess["awaiting_passport"] = True
    await _prompt_passport(update.message, context)


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
    booking["hotel"] = key
    sess["pending_booking"] = booking
    sess["awaiting_passport"] = True
    await q.edit_message_text(
        f"已選擇「{label.split(' ')[0]}」。請傳送客人的「證件照片」，"
        "或輸入 /skip 直接產生訂房單。"
    )
    await _prompt_passport(q, context)


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
        # 診斷：回報 OCR 雲端提供者狀態，便於遠端確認 key 是否被容器載入
        try:
            import ocr as _ocr
            _cloud = _ocr._available_cloud()
        except Exception:  # noqa: BLE001
            _cloud = ["<import_failed>"]
        return web.json_response({
            "status": "ok",
            "ready": diag["ready"],
            "webhook_url": diag["webhook_url"],
            "error": str(diag["error"]) if diag["error"] else None,
            "ocr_space_key_loaded": bool(os.environ.get("OCR_SPACE_KEY")),
            "cloud_providers": _cloud,
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
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CallbackQueryHandler(type_callback, pattern=r"^type\|"))
    app.add_handler(CallbackQueryHandler(hotel_callback, pattern=r"^hotel\|"))
    app.add_handler(CallbackQueryHandler(bookhotel_callback, pattern=r"^bookhotel\|"))
    app.add_handler(CallbackQueryHandler(skip_passport_callback, pattern=r"^skip_passport$"))
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
