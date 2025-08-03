import logging
import os
from telegram import Bot, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(BOT_TOKEN)

async def summarize_text(text: str) -> str:
    return f"Сводка:\n{text[:500]}..."

def extract_text_from_docx(file_path: str) -> str:
    from docx import Document
    doc = Document(file_path)
    return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())

def extract_text_from_srt(file_path: str) -> str:
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    return '\n'.join(l.strip() for l in lines if not l.strip().isdigit() and '-->' not in l)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        return

    file = update.message.document
    if not (file.file_name.endswith('.docx') or file.file_name.endswith('.srt')):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    file_path = f"/tmp/{file.file_name}"
    file_obj = await file.get_file()
    await file_obj.download_to_drive(file_path)

    try:
        if file.file_name.endswith('.docx'):
            text = extract_text_from_docx(file_path)
        else:
            text = extract_text_from_srt(file_path)

        summary = await summarize_text(text)
        summary = '\n'.join([f"> {line}" for line in summary.splitlines()])
        await update.message.reply_text(summary)
    finally:
        os.remove(file_path)

def main():
    application = Application(bot=bot)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.run_polling()

if __name__ == '__main__':
    main()
