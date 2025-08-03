import logging
import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
from flask import Flask, request, jsonify
from docx import Document
import threading
from concurrent.futures import ThreadPoolExecutor

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

# Создаем базовый URL для API
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Thread pool для обработки файлов
executor = ThreadPoolExecutor(max_workers=3)

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

def send_telegram_request(method: str, data: dict):
    """Универсальная функция для отправки запросов к Telegram API"""
    try:
        url = f"{TELEGRAM_API_BASE}/{method}"
        post_data = urllib.parse.urlencode(data).encode('utf-8')
        
        req = urllib.request.Request(url, data=post_data)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result
            
    except Exception as e:
        logger.error(f"Error in Telegram API request {method}: {e}")
        return None

def send_message(chat_id: int, text: str, parse_mode=None, reply_to_message_id=None):
    """Отправка сообщения"""
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_to_message_id:
        data["reply_to_message_id"] = reply_to_message_id
    
    result = send_telegram_request("sendMessage", data)
    if result and result.get('ok'):
        return result['result']
    return None

def edit_message(chat_id: int, message_id: int, text: str, parse_mode=None):
    """Редактирование сообщения"""
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    
    result = send_telegram_request("editMessageText", data)
    return result and result.get('ok', False)

def delete_message(chat_id: int, message_id: int):
    """Удаление сообщения"""
    data = {"chat_id": chat_id, "message_id": message_id}
    result = send_telegram_request("deleteMessage", data)
    return result and result.get('ok', False)

def download_file(file_id: str, file_path: str):
    """Загрузка файла"""
    try:
        # Получаем информацию о файле
        data = {"file_id": file_id}
        result = send_telegram_request("getFile", data)
        
        if not result or not result.get('ok'):
            logger.error(f"Failed to get file info: {result}")
            return False
        
        file_info = result['result']
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info['file_path']}"
        
        # Загружаем файл
        with urllib.request.urlopen(file_url, timeout=30) as response:
            with open(file_path, 'wb') as f:
                f.write(response.read())
        
        return True
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return False

def call_huggingface_api(episode_text: str, file_name: str) -> str:
    """Вызов API Hugging Face"""
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
        
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(api_url, data=data, headers=headers)
        
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                result = json.loads(response.read().decode('utf-8'))
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
                return f"❌ Ошибка API: {response.status}"
    
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return "⏳ Модель загружается, попробуйте через 1-2 минуты"
        elif e.code == 429:
            return "⏳ Превышены лимиты API, попробуйте позже"
        else:
            return f"❌ Ошибка API: {e.code}"
    except Exception as e:
        logger.error(f"Error calling Hugging Face API: {e}")
        return f"❌ Ошибка при обращении к API: {str(e)}"

def process_document(update_data: dict):
    """Обработка документа"""
    try:
        logger.info("Processing document")
        
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
        
        # Проверяем размер файла
        if file_size > 20 * 1024 * 1024:
            send_message(chat_id, "❌ Файл слишком большой (максимум 20MB)")
            return
        
        # Проверяем расширение файла
        if not (file_name.lower().endswith('.docx') or file_name.lower().endswith('.srt')):
            send_message(
                chat_id,
                "❌ Поддерживаются только файлы .srt (субтитры) и .docx (документы)\n"
                "Отправьте файл субтитров с диалогами персонажей."
            )
            return
        
        # Отправляем статусное сообщение
        status_response = send_message(chat_id, "🔄 Анализирую диалоги персонажей...")
        if not status_response:
            logger.error("Failed to send status message")
            return
        status_message_id = status_response['message_id']
        
        file_path = f"/tmp/{file_name}"
        
        try:
            # Загружаем файл
            logger.info("Downloading file...")
            if not download_file(file_id, file_path):
                edit_message(chat_id, status_message_id, "❌ Ошибка при загрузке файла")
                return
            
            # Извлекаем текст
            logger.info("Extracting dialogue text...")
            if file_name.lower().endswith('.docx'):
                raw_text = extract_text_from_docx(file_path)
            else:
                raw_text = extract_text_from_srt(file_path)
            
            if not raw_text or not raw_text.strip():
                edit_message(chat_id, status_message_id, "❌ Файл не содержит диалогов персонажей")
                return
            
            episode_text = prepare_episode_text(raw_text)
            
            if not episode_text or len(episode_text) < 100:
                edit_message(chat_id, status_message_id, "❌ Недостаточно диалогов для создания пересказа")
                return
            
            edit_message(chat_id, status_message_id, "🤖 Создаю пересказ серии...")
            
            # Создаем пересказ
            logger.info("Creating episode summary...")
            summary = call_huggingface_api(episode_text, file_name)
            
            # Удаляем статусное сообщение
            delete_message(chat_id, status_message_id)
            
            # Отправляем результат
            send_message(chat_id, summary, parse_mode="Markdown", reply_to_message_id=message_id)
            
            logger.info("Successfully created episode summary")
            
        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
            edit_message(
                chat_id,
                status_message_id,
                f"❌ Ошибка при обработке файла: {str(e)}\n"
                "Убедитесь, что файл содержит диалоги в формате [Персонаж]: текст"
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
        logger.error(f"Unexpected error in process_document: {e}")

def setup_webhook():
    """Настройка вебхука"""
    try:
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        data = {"url": webhook_url}
        result = send_telegram_request("setWebhook", data)
        
        if result and result.get('ok'):
            logger.info(f"Webhook set to {webhook_url}")
            return True
        else:
            logger.error(f"Failed to set webhook: {result}")
            return False
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
            executor.submit(process_document, update_json)
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
            
            send_message(chat_id, welcome_message, parse_mode="Markdown")
        
        return "ok"
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}")
        return "error", 500

@app.route("/", methods=["GET"])
def health_check():
    """Проверка работоспособности"""
    return "Bot is running!"

def main():
    """Главная функция"""
    port = int(os.environ.get('PORT', 10000))

    # Настраиваем вебхук
    logger.info("Setting up webhook...")
    if not setup_webhook():
        logger.error("Failed to set up webhook")
        sys.exit(1)

    # Запускаем Flask-приложение
    logger.info(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

if __name__ == '__main__':
    main()
