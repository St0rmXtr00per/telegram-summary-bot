import logging
import os
import sys
import asyncio
import aiohttp
from threading import Thread
from flask import Flask, jsonify
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

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

# Получение токенов и настроек
BOT_TOKEN = os.getenv("BOT_TOKEN")
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")  # Добавьте это в Render
HUGGINGFACE_MODEL = os.getenv("HUGGINGFACE_MODEL", "facebook/bart-large-cnn")  # Модель по умолчанию

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set!")
    sys.exit(1)

if not HUGGINGFACE_API_KEY:
    logger.error("HUGGINGFACE_API_KEY environment variable is not set!")
    sys.exit(1)

async def summarize_with_huggingface(text: str) -> str:
    """Суммаризация текста через Hugging Face API"""
    try:
        # URL API Hugging Face
        api_url = f"https://api-inference.huggingface.co/models/{HUGGINGFACE_MODEL}"
        
        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Ограничиваем длину текста (модели имеют лимиты)
        max_length = 1024  # Для BART
        if len(text) > max_length:
            # Берем начало и конец текста
            half = max_length // 2
            text = text[:half] + "..." + text[-half:]
        
        payload = {
            "inputs": text,
            "parameters": {
                "max_length": 150,
                "min_length": 30,
                "do_sample": False
            }
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if isinstance(result, list) and len(result) > 0:
                        summary = result[0].get('summary_text', 'Не удалось получить резюме')
                        return f"📄 **Краткое содержание:**\n\n{summary}"
                    else:
                        return "❌ Неожиданный формат ответа от API"
                elif response.status == 503:
                    return "⏳ Модель загружается, попробуйте через минуту"
                else:
                    error_text = await response.text()
                    logger.error(f"Hugging Face API error: {response.status} - {error_text}")
                    return f"❌ Ошибка API: {response.status}"
    
    except Exception as e:
        logger.error(f"Error calling Hugging Face API: {e}")
        return f"❌ Ошибка при обращении к API: {str(e)}"

def extract_text_from_docx(file_path: str) -> str:
    """Извлечение текста из DOCX файла"""
    try:
        from docx import Document
        doc = Document(file_path)
        text_parts = []
        
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                text_parts.append(paragraph.text.strip())
        
        full_text = '\n'.join(text_parts)
        logger.info(f"Extracted {len(full_text)} characters from DOCX")
        return full_text
        
    except Exception as e:
        logger.error(f"Error extracting text from DOCX: {e}")
        raise e

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
        
        full_text = ' '.join(text_lines)  # Соединяем пробелами для лучшего чтения
        logger.info(f"Extracted {len(full_text)} characters from SRT")
        return full_text
        
    except Exception as e:
        logger.error(f"Error extracting text from SRT: {e}")
        raise e

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_message = """
🤖 **Бот для суммаризации документов**

Отправьте мне файл в формате:
📄 `.docx` - документ Word
🎬 `.srt` - файл субтитров

Я проанализирую содержимое и создам краткое резюме с помощью ИИ.

Просто отправьте файл и ждите результат!
    """
    await update.message.reply_text(welcome_message)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик документов"""
    try:
        logger.info("Received document message")
        
        if not update.message or not update.message.document:
            logger.warning("No document in message")
            return
        
        file = update.message.document
        file_name = file.file_name or "unknown"
        file_size = file.file_size or 0
        
        logger.info(f"Processing file: {file_name}, size: {file_size} bytes")
        
        # Проверяем размер файла (лимит 20MB для Telegram API)
        if file_size > 20 * 1024 * 1024:
            await update.message.reply_text("❌ Файл слишком большой (максимум 20MB)")
            return
        
        # Проверяем поддерживаемые форматы
        if not (file_name.lower().endswith('.docx') or file_name.lower().endswith('.srt')):
            await update.message.reply_text(
                "❌ Поддерживаются только файлы формата .docx и .srt\n"
                "Отправьте документ Word или файл субтитров."
            )
            return
        
        # Уведомляем пользователя о начале обработки
        await update.message.reply_text("🔄 Обрабатываю файл...")
        
        # Показываем, что бот печатает
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, 
            action=ChatAction.TYPING
        )
        
        # Создаем временный файл
        file_path = f"/tmp/{file_name}"
        
        try:
            # Скачиваем файл
            logger.info("Downloading file...")
            file_obj = await file.get_file()
            await file_obj.download_to_drive(file_path)
            
            # Извлекаем текст в зависимости от типа файла
            logger.info("Extracting text...")
            if file_name.lower().endswith('.docx'):
                text = extract_text_from_docx(file_path)
            else:  # .srt
                text = extract_text_from_srt(file_path)
            
            if not text or not text.strip():
                await update.message.reply_text("❌ Файл пустой или не содержит текста")
                return
            
            # Показываем прогресс
            await update.message.reply_text("🤖 Создаю резюме с помощью ИИ...")
            
            # Создаем сводку через Hugging Face
            logger.info("Calling Hugging Face API...")
            summary = await summarize_with_huggingface(text)
            
            # Отправляем результат
            await update.message.reply_text(summary, parse_mode='Markdown')
            logger.info("Successfully processed document")
            
        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
            await update.message.reply_text(
                f"❌ Ошибка при обработке файла: {str(e)}\n"
                "Попробуйте еще раз или отправьте другой файл."
            )
        
        finally:
            # Удаляем временный файл
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info("Temporary file removed")
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
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        
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
    logger.info("Starting application...")
    
    # Запускаем бота в отдельном потоке
    bot_thread = Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Запускаем Flask сервер для health checks
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == '__main__':
    main()
