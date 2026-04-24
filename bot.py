#!/usr/bin/env python3
"""
Sidor Avoda Maor — Telegram Bot
Receives work schedule screenshots, parses shifts for מאור פורמן,
and creates Google Calendar events automatically.
Runs for 25 minutes then exits (scheduled every 30 min via GitHub Actions).
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from vision_parser import parse_schedule_image, get_last_debug
from calendar_client import create_all_shifts

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
RUN_DURATION_SECONDS = 25 * 60  # 25 minutes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "שלום! אני הבוט של מאור לסידור עבודה 💼\n\n"
        "שלח לי את הסידור עבודה כ-📎 קובץ (לא תמונה!) לאיכות מיטבית.\n"
        "לחץ על 📎 → קובץ → בחר את התמונה."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("מעבד את הסידור... ⏳")

    try:
        if update.message.document:
            file = await context.bot.get_file(update.message.document.file_id)
        else:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        shifts = parse_schedule_image(bytes(image_bytes))

        if not shifts:
            debug_info = get_last_debug()
            await update.message.reply_text(
                f"לא מצאתי משמרות עבור מאור פורמן.\n\n"
                f"🔍 מה ה-AI ראה בתמונה:\n{debug_info}\n\n"
                f"נסה לשלוח תמונה ברורה יותר או חתוך אותה לאזור הרלוונטי."
            )
            return

        links = create_all_shifts(shifts)

        lines = ["✅ עודכן ביומן!\n"]
        for shift in shifts:
            end_note = " (+1 יום)" if shift["shift_type"] in ("לילה", "כפולה לילה") else ""
            lines.append(
                f"📅 תאריך: {shift['date']}\n"
                f"📍 מיקום: {shift['location']}\n"
                f"🪖 עמדה: {shift['role']}\n"
                f"⏰ משמרת: {shift['shift_type']} ({shift['start_time']}–{shift['end_time']}{end_note})\n"
            )

        await update.message.reply_text("\n".join(lines))
        logger.info(f"Created {len(shifts)} events for chat {chat_id}")

    except Exception as e:
        logger.error(f"Error processing photo: {e}", exc_info=True)
        await update.message.reply_text(f"שגיאה בעיבוד התמונה: {e}\nנסה שנית.")


async def shutdown_after(app: Application, seconds: int):
    await asyncio.sleep(seconds)
    logger.info("Run duration reached, shutting down.")
    await app.stop()
    await app.shutdown()


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

    async def post_init(application: Application):
        asyncio.create_task(shutdown_after(application, RUN_DURATION_SECONDS))

    app.post_init = post_init

    logger.info("Bot started, will run for 25 minutes.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
