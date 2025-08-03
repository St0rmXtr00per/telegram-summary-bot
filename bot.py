import logging
import os
import sys
import asyncio
import aiohttp
import requests
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from docx import Document
import threading
from concurrent.futures import ThreadPoolExecutor
import time

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app для вебхука
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

# Создаем синхронный бот для отправки сообщений
sync_bot = Bot(token=BOT_TOKEN)

# Thread pool для обработки файлов
executor = ThreadPoolExecutor(max_workers=3)

# Функции для обработки текста и API
def prepare_episode_text(text: str) -> str:
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

def summarize_episode_with_huggingface_sync(episode_text: str, file_name: str) -> str:
    """Синхронная версия функции для вызова API"""
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
        
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
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
        elif response.status_code == 503:
            return "⏳ Модель загружается, попробуйте через 1-2 минуты"
        elif response.status_code == 429:
            return "⏳ Превышены лимиты API, попробуйте позже"
        else:
            logger.error(f"Hugging Face API error: {response.status_code} - {response.text}")
            return f"❌ Ошибка API: {response.status_code}"
    
    except requests.exceptions.Timeout:
        logger.error("Timeout calling Hugging Face API")
        return "⏳ Таймаут API, попробуйте еще раз"
    except Exception as e:
        logger.error(f"Error calling Hugging Face API: {e}")
        return f"❌ Ошибка при обращении к API: {str(e)}"

def format_episode_summary(summary: str, file_name: str) -> str:
    episode_name = file_name.replace('.srt', '').replace('.docx', '')
    formatted_summary = f"""📺 **Краткий пересказ: {episode_name}**

||{summary.strip()}||

_Пересказ создан на основе диалогов персонажей_"""
    
    return formatted_summary

def extract_text_from_docx(file_path: str) -> str:
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

def send_message_sync(chat_id: int, text: str, parse_mode=None, reply_to_message_id=None):
    """Синхронная отправка сообщения"""
    try:
        if parse_mode:
            sync_bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_message_id
            )
        else:
            sync_bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id
            )
        return True
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return False

def edit_message_sync(chat_id: int, message_id: int, text: str, parse_mode=None):
    """Синхронное редактирование сообщения"""
    try:
        if parse_mode:
            sync_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode
            )
        else:
            sync_bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text
            )
        return True
    except Exception as e:
        logger.error(f"Error editing message: {e}")
        return False

def delete_message_sync(chat_id: int, message_id: int):
    """Синхронное удаление сообщения"""
    try:
        sync_bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.error(f"Error deleting message: {e}")
        return False

def download_file_sync(file_id: str, file_path: str):
    """Синхронная загрузка файла"""
    try:
        file_info = sync_bot.get_file(file_id)
        file_info.download_to_drive(file_path)
        return True
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return False

def process_document_sync(update_data: dict):
    """Синхронная обработка документа"""
    try:
        logger.info("Processing document synchronously")
        
        # Получаем данные из update
        if 'message' in update_data:
            message_data = update_data['message']
        elif 'channel_post' in update_data:
            message_data = update_data['channel_post']
        else:
            logger.error("No message or channel_post in update")
            return
        
        if 'document' not in message_data:
            logger.error("No document in message")
            return
        
        chat_id = message_data['chat']['id']
        message_id = message_data['message_id']
        document = message_data['document']
        
        file_name = document.get('file_name', 'unknown')
        file_size = document.get('file_size', 0)
        file_id = document['file_id']
        
        logger.info(f"Processing file: {file_name}, size: {file_size} bytes")
        
        if file_size > 20 * 1024 * 1024:
            send_message_sync(chat_id, "❌ Файл слишком большой (максимум 20MB)")
            return
        
        if not (file_name.lower().endswith('.docx') or file_name.lower().endswith('.srt')):
            send_message_sync(
                chat_id,
                "❌ Поддерживаются только файлы .srt (субтитры) и .docx (документы)\n"
                "Отправьте файл субтитров с диалогами персонажей."
            )
            return
        
        # Отправляем статусное сообщение
        status_response = sync_bot.send_message(chat_id, "🔄 Анализирую диалоги персонажей...")
        status_message_id = status_response.message_id
        
        file_path = f"/tmp/{file_name}"
        
        try:
            # Загружаем файл
            logger.info("Downloading file...")
            if not download_file_sync(file_id, file_path):
                edit_message_sync(chat_id, status_message_id, "❌ Ошибка при загрузке файла")
                return
            
            # Извлекаем текст
            logger.info("Extracting dialogue text...")
            if file_name.lower().endswith('.docx'):
                raw_text = extract_text_from_docx(file_path)
            else:
                raw_text = extract_text_from_srt(file_path)
            
            if not raw_text or not raw_text.strip():
                edit_message_sync(chat_id, status_message_id, "❌ Файл не содержит диалогов персонажей")
                return
            
            episode_text = prepare_episode_text(raw_text)
            
            if not episode_text or len(episode_text) < 100:
                edit_message_sync(chat_id, status_message_id, "❌ Недостаточно диалогов для создания пересказа")
                return
            
            edit_message_sync(chat_id, status_message_id, "🤖 Создаю пересказ серии...")
            
            # Создаем пересказ
            logger.info("Creating episode summary...")
            summary = summarize_episode_with_huggingface_sync(episode_text, file_name)
            
            # Удаляем статусное сообщение
            delete_message_sync(chat_id, status_message_id)
            
            # Отправляем результат
            send_message_sync(
                chat_id,
                summary,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=message_id
            )
            
            logger.info("Successfully created episode summary")
            
        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
            edit_message_sync(
                chat_id,
                status_message_id,
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
        logger.error(f"Unexpected error in process_document_sync: {e}")

def setup_webhook_sync():
    """Синхронная настройка вебхука"""
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        sync_bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")
        return False

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_handler():
    """Обработчик вебхука"""
    try:
        update_json = request.get_json(force=True)
        logger.info(f"Received webhook update: {update_json}")
        
        # Проверяем, есть ли документ в сообщении
        has_document = False
        if 'message' in update_json and 'document' in update_json['message']:
            has_document = True
        elif 'channel_post' in update_json and 'document' in update_json['channel_post']:
            has_document = True
        
        if has_document:
            # Обрабатываем документ в отдельном потоке
            executor.submit(process_document_sync, update_json)
        elif 'message' in update_json and update_json['message'].get('text', '').startswith('/start'):
            # Обрабатываем команду /start
            message_data = update_json['message']
            chat_id = message_data['chat']['id']
            
            welcome_message = """🎬 **Бот для пересказа серий**

Отправьте мне файл субтитров или документ:
🎬 `.srt` - файл субтитров с диалогами персонажей
📄 `.docx` - документ с диалогами в формате [Имя]: текст

Я создам краткий пересказ основных событий серии на основе диалогов персонажей. Пересказ будет скрыт под спойлер, чтобы не портить впечатление другим.

**Важно:** Я использую только информацию из вашего файла и не добавляю ничего от себя.

Просто отправьте файл и ждите пересказ!"""
            
            send_message_sync(chat_id, welcome_message, parse_mode=ParseMode.MARKDOWN)
        
        return "ok"
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}")
        return "error", 500

@app.route("/", methods=["GET"])
def health_check():
    """Проверка работоспособности"""
    return "Bot is running!"

def main():
    """Главная функция для запуска Flask-сервера и настройки вебхука"""
    port = int(os.environ.get('PORT', 10000))

    # Настраиваем вебхук
    logger.info("Setting up webhook...")
    if not setup_webhook_sync():
        logger.error("Failed to set up webhook")
        sys.exit(1)

    # Запускаем Flask-приложение
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

if __name__ == '__main__':
    main()
