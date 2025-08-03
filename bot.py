import logging
import os
import sys
import asyncio
from threading import Thread
from flask import Flask, jsonify
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Добавим отладку версии Python
print(f"Running with Python version: {sys.version}")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app для health check
app = Flask(__name__)

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "telegram-bot"}), 200

@app.route('/')
def index():
    return jsonify({"message": "Telegram Summary Bot is running"}), 200

# Получение токена с проверкой
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set!")
    sys.exit(1)

async def summarize_text(text: str) -> str:
    """Простая функция суммаризации текста"""
    if not text.strip():
        return "Текст пустой или не найден."
    
    # Простая суммаризация - берем первые 500 символов
    summary = text[:500]
    if len(text) > 500:
        summary += "..."
    
    return f"📄 Сводка документа:\n\n{summary}"

def extract_text_from_docx(file_path: str) -> str:
    """Извлечение текста из DOCX файла"""
    try:
        from docx import Document
        doc = Document(file_path)
        text_parts = []
        
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text_parts.append(paragraph.text.strip())
        
        return '\n'.join(text_parts)
    except Exception as e:
        logger.error(f"Error extracting text from DOCX: {e}")
        return f"Ошибка при извлечении текста из DOCX файла: {str(e)}"

def extract_text_from_srt(file_path: str) -> str:
    """Извлечение текста из SRT файла"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        text_lines = []
        for line in lines:
            line = line.strip()
            # Пропускаем номера субтитров и временные метки
            if line and not line.isdigit() and '-->' not in line:
                text_lines.append(line)
        
        return '\n'.join(text_lines)
    except Exception as e:
        logger.error(f"Error extracting text from SRT: {e}")
        return f"Ошибка при извлечении текста из SRT файла: {str(e)}"

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов"""
    try:
        if not update.message or not update.message.document:
            return
        
        file = update.message.document
        file_name = file.file_name or "unknown"
        
        # Проверяем поддерживаемые форматы
        if not (file_name.endswith('.docx') or file_name.endswith('.srt')):
            await update.message.reply_text(
                "❌ Поддерживаются только файлы формата .docx и .srt"
            )
            return
        
        # Показываем, что бот печатает
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, 
            action=ChatAction.TYPING
        )
        
        # Создаем временный файл
        file_path = f"/tmp/{file_name}"
        
        try:
            # Скачиваем файл
            file_obj = await file.get_file()
            await file_obj.download_to_drive(file_path)
            
            # Извлекаем текст в зависимости от типа файла
            if file_name.endswith('.docx'):
                text = extract_text_from_docx(file_path)
            else:  # .srt
                text = extract_text_from_srt(file_path)
            
            if not text or not text.strip():
                await update.message.reply_text("❌ Не удалось извлечь текст из файла")
                return
            
            # Создаем сводку
            summary = await summarize_text(text)
            
            # Отправляем результат
            await update.message.reply_text(summary)
            
        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
            await update.message.reply_text(
                f"❌ Ошибка при обработке файла: {str(e)}"
            )
        
        finally:
            # Удаляем временный файл
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error removing temp file: {e}")
    
    except Exception as e:
        logger.error(f"Unexpected error in handle_document: {e}")
        try:
            await update.message.reply_text("❌ Произошла непредвиденная ошибка")
        except:
            pass

def run_bot():
    """Запуск Telegram бота в отдельном потоке"""
    try:
        logger.info("Starting Telegram bot...")
        
        # Создаем приложение
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # Добавляем обработчик документов
        application.add_handler(
            MessageHandler(filters.Document.ALL, handle_document)
        )
        
        logger.info("Bot handlers registered successfully")
        
        # Запускаем бота
        logger.info("Starting polling...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

def main():
    """Главная функция"""
    # Запускаем бота в отдельном потоке
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask сервер для health checks
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == '__main__':
    main()
