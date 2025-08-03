import logging
import os
import sys
import asyncio
import aiohttp
from flask import Flask, jsonify, request
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from docx import Document

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app для health check и вебхука
app = Flask(__name__)

# Получение токенов и настроек
BOT_TOKEN = os.getenv("BOT_TOKEN")
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HUGGINGFACE_MODEL = os.getenv("HUGGINGFACE_MODEL", "IlyaGusev/mbart_ru_sum_gazeta")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set!")
    sys.exit(1)

if not HUGGINGFACE_API_KEY:
    logger.error("HUGGINGFACE_API_KEY environment variable is not set!")
    sys.exit(1)

if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL environment variable is not set!")
    sys.exit(1)

# Функции для обработки текста и API (без изменений)
def prepare_episode_text(text: str) -> str:
    """Подготавливает текст серии для анализа"""
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        line = line.strip()
        if line and '[' in line and ']' in line:
            if '-->' not in line and not line.isdigit():
                cleaned_lines.append(line)
    
    episode_text = ' '.join(cleaned_lines)
    
    max_chars = 3000
    if len(episode_text) > max_chars:
        quarter = max_chars // 4
        episode_text = episode_text[:quarter*3] + "..." + episode_text[-quarter:]
    
    return episode_text

async def summarize_episode_with_huggingface(episode_text: str, file_name: str) -> str:
    """Создание краткого пересказа серии через Hugging Face API"""
    try:
        prompt = f"""Создай краткий пересказ этой серии на основе диалогов персонажей.
        
ВАЖНЫЕ ТРЕБОВАНИЯ:
- Используй ТОЛЬКО информацию из предоставленного текста
- НЕ добавляй ничего от себя
- Сосредоточься на главных событиях серии
- Упоминай имена персонажей из квадратных скобок
- Пересказ должен читаться за 2 минуты
- Не описывай мелкие детали

Текст серии:
{episode_text}

Краткий пересказ основных событий серии:"""

        api_url = f"https://api-inference.huggingface.co/models/{HUGGINGFACE_MODEL}"
        
        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_length": 300,
                "min_length": 100,
                "do_sample": False,
                "temperature": 0.3,
                "repetition_penalty": 1.1
            }
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(api_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if isinstance(result, list) and len(result) > 0:
                        summary = result[0].get('summary_text', '')
                        if not summary:
                            summary = result[0].get('generated_text', '')
                            if prompt in summary:
                                summary = summary.replace(prompt, '').strip()
                        
                        if summary:
                            return format_episode_summary(summary, file_name)
                        else:
                            return "❌ Модель не смогла создать пересказ"
                    else:
                        return "❌ Неожиданный формат ответа от API"
                elif response.status == 503:
                    return "⏳ Модель загружается, попробуйте через 1-2 минуты"
                elif response.status == 429:
                    return "⏳ Превышены лимиты API, попробуйте позже"
                else:
                    error_text = await response.text()
                    logger.error(f"Hugging Face API error: {response.status} - {error_text}")
                    return f"❌ Ошибка API: {response.status}"
    
    except asyncio.TimeoutError:
        logger.error("Timeout calling Hugging Face API")
        return "⏳ Таймаут API, попробуйте еще раз"
    except Exception as e:
        logger.error(f"Error calling Hugging Face API: {e}")
        return f"❌ Ошибка при обращении к API: {str(e)}"

def format_episode_summary(summary: str, file_name: str) -> str:
    """Форматирует пересказ для отправки в Telegram со сворачиваемым блоком"""
    episode_name = file_name.replace('.srt', '').replace('.docx', '')
    formatted_summary = f"""📺 **Краткий пересказ: {episode_name}**

||{summary.strip()}||

_Пересказ создан на основе диалогов персонажей_"""
    
    return formatted_summary

def extract_text_from_docx(file_path: str) -> str:
    """Извлечение текста из DOCX файла"""
    try:
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
    """Извлечение текста из SRT файла с сохранением структуры диалогов"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        blocks = content.strip().split('\n\n')
        dialogue_lines = []
        
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 3:
                text_lines = lines[2:]
                for text_line in text_lines:
                    text_line = text_line.strip()
                    if text_line and '[' in text_line and ']' in text_line:
                        dialogue_lines.append(text_line)
        
        full_text = '\n'.join(dialogue_lines)
        logger.info(f"Extracted {len(dialogue_lines)} dialogue lines from SRT")
        return full_text
    except Exception as e:
        logger.error(f"Error extracting text from SRT: {e}")
        raise e

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_message = """🎬 **Бот для пересказа серий**

Отправьте мне файл субтитров или документ:
🎬 `.srt` - файл субтитров с диалогами персонажей
📄 `.docx` - документ с диалогами в формате [Имя]: текст

Я создам краткий пересказ основных событий серии на основе диалогов персонажей. Пересказ будет скрыт под спойлер, чтобы не портить впечатление другим.

**Важно:** Я использую только информацию из вашего файла и не добавляю ничего от себя.

Просто отправьте файл и ждите пересказ!"""
    
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

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
        
        if file_size > 20 * 1024 * 1024:
            await update.message.reply_text("❌ Файл слишком большой (максимум 20MB)")
            return
        
        if not (file_name.lower().endswith('.docx') or file_name.lower().endswith('.srt')):
            await update.message.reply_text(
                "❌ Поддерживаются только файлы .srt (субтитры) и .docx (документы)\n"
                "Отправьте файл субтитров с диалогами персонажей."
            )
            return
        
        status_message = await update.message.reply_text("🔄 Анализирую диалоги персонажей...")
        
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, 
            action=ChatAction.TYPING
        )
        
        file_path = f"/tmp/{file_name}"
        
        try:
            logger.info("Downloading file...")
            file_obj = await file.get_file()
            await file_obj.download_to_drive(file_path)
            
            logger.info("Extracting dialogue text...")
            if file_name.lower().endswith('.docx'):
                raw_text = extract_text_from_docx(file_path)
            else:
                raw_text = extract_text_from_srt(file_path)
            
            if not raw_text or not raw_text.strip():
                await status_message.edit_text("❌ Файл не содержит диалогов персонажей")
                return
            
            episode_text = prepare_episode_text(raw_text)
            
            if not episode_text or len(episode_text) < 100:
                await status_message.edit_text("❌ Недостаточно диалогов для создания пересказа")
                return
            
            await status_message.edit_text("🤖 Создаю пересказ серии...")
            
            logger.info("Creating episode summary...")
            summary = await summarize_episode_with_huggingface(episode_text, file_name)
            
            await status_message.delete()
            
            await update.message.reply_text(
                summary, 
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id
            )
            
            logger.info("Successfully created episode summary")
        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
            await status_message.edit_text(
                f"❌ Ошибка при обработке файла: {str(e)}\n"
                "Убедитесь, что файл содержит диалоги в формате [Персонаж]: текст"
            )
        finally:
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

# Инициализация бота
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Теперь мы добавляем обработчики, когда все функции уже определены
application.add_handler(CommandHandler("start", start_command))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "telegram-bot"}), 200

@app.route('/')
def index():
    return jsonify({"message": "Telegram Summary Bot is running"}), 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
async def webhook_handler():
    try:
        update_json = await request.get_json(force=True)
        update = Update.de_json(update_json, application.bot)
        await application.process_update(update)
        return "ok"
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}")
        return "error", 500

async def setup_webhook():
    """Настройка вебхука"""
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info(f"Webhook set to {WEBHOOK_URL}/{BOT_TOKEN}")

def main():
    """Главная функция для запуска Flask-сервера и настройки вебхука"""
    port = int(os.environ.get('PORT', 10000))

    # Сначала настраиваем вебхук
    asyncio.run(setup_webhook())

    # Затем запускаем Flask-приложение
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == '__main__':
    main()
