
import logging
import os
import mimetypes
import re
import time
import csv
import hashlib
from pathlib import Path
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ConversationHandler, ContextTypes, MessageHandler, filters
)
from docx import Document
import PyPDF2
from dotenv import load_dotenv

CHOOSING, CHECK_DOCUMENT, VERIFY_PAYMENT = range(3)

TERMS_PATH = "terms.pdf"
LOG_FILE = "logs.csv"
CHECKS_DB_DIR = "unique_checks"

Path(CHECKS_DB_DIR).mkdir(exist_ok=True)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
user_docs = {}

RULES_TEXT = (
    "🔹 Прочитайте и примите правила использования бота:\n"
    "1. Не загружайте вредоносные или запрещённые файлы.\n"
    "2. Размер файла не должен превышать 20 МБ.\n"
    "3. Бот используется только для анализа текстов.\n\n"
    "📎 Скачайте пользовательское соглашение ниже."
)

def hash_file(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def is_new_check(file_path: str) -> bool:
    file_hash = hash_file(file_path)
    db_file = os.path.join(CHECKS_DB_DIR, file_hash)
    if os.path.exists(db_file):
        return False
    Path(db_file).touch()
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📥 Скачать соглашение", callback_data="download_terms")],
        [InlineKeyboardButton("✅ Принять", callback_data="accept_rules")]
    ]
    await (update.message or update.effective_message).reply_text(RULES_TEXT, reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING

async def send_terms_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if os.path.exists(TERMS_PATH):
        with open(TERMS_PATH, "rb") as f:
            await query.message.reply_document(InputFile(f, filename="Пользовательское_соглашение.pdf"))
    else:
        await query.message.reply_text("❌ Файл соглашения не найден.")

async def accept_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("Проверить документ", callback_data="check_doc")]]
    await query.edit_message_text("✅ Правила приняты. Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING

async def choose_check_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📄 Отправьте первый документ (PDF, DOCX, TXT).")
    return CHECK_DOCUMENT

def is_supported_file_type(mime_type: str) -> bool:
    return mime_type in [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain"
    ]

def extract_text(file_path: str, mime_type: str) -> str:
    if mime_type == "application/pdf":
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

def analyze_registration(text: str) -> str:
    normalized = re.sub(r"\s+", "", text.lower())
    return "✅ регистрация оригинальная" if "егопостановкинаучетпоместупребывания" in normalized else "❌ регистрация фальшивая"

def log_user_action(user_id, username, action, doc_name, result):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S"), user_id, username, action, doc_name, result])

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    if not document or document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ Файл должен быть менее 20 МБ.")
        return CHECK_DOCUMENT

    mime_type, _ = mimetypes.guess_type(document.file_name)
    if not mime_type or not is_supported_file_type(mime_type):
        await update.message.reply_text("❌ Поддерживаются только PDF, DOCX и TXT.")
        return CHECK_DOCUMENT

    user_id = update.effective_user.id
    file_path = f"first_doc_{user_id}_{document.file_name}"
    await (await document.get_file()).download_to_drive(file_path)

    try:
        text = extract_text(file_path, mime_type)
        analysis = analyze_registration(text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка обработки: {e}")
        return CHECK_DOCUMENT

    user_docs[user_id] = {
        "file_path": file_path,
        "file_name": document.file_name,
        "analysis": analysis,
    }

    log_user_action(user_id, update.effective_user.username, "загрузил документ", document.file_name, analysis)
    await update.message.reply_text("✅ Документ принят. Теперь отправьте чек.")
    return VERIFY_PAYMENT

async def verify_payment_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    if not document or document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("❌ Чек слишком большой (до 20 МБ).")
        return VERIFY_PAYMENT

    file_path = f"payment_doc_{update.effective_user.id}_{document.file_name}"
    await (await document.get_file()).download_to_drive(file_path)

    if not is_new_check(file_path):
        await update.message.reply_text("❌ Этот чек уже был использован. Попробуйте другой.")
        return VERIFY_PAYMENT

    try:
        content = extract_text(file_path, mimetypes.guess_type(file_path)[0])
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка чтения чека: {e}")
        return VERIFY_PAYMENT

    os.remove(file_path)
    normalized = re.sub(r"\s+", "", content.lower()).replace("₽", "").replace(",", "").replace(".", "")
    if "2785" in normalized and "+992000992183" in normalized:
        info = user_docs.get(update.effective_user.id, {})
        try:
            os.remove(info.get("file_path", ""))
        except:
            pass
        result = info.get("analysis", "❌ Анализ недоступен.")
        docname = info.get("file_name", "документ")
        log_user_action(update.effective_user.id, update.effective_user.username, "чек", document.file_name, "Чек подтверждён")

        await update.message.reply_text(
            f"✅ Чек принят.\n📄 Результат анализа «{docname}»:\n\n{result}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Проверить другой документ", callback_data="check_doc")]])
        )
        return CHOOSING
    else:
        await update.message.reply_text("❌ Чек не прошёл проверку. Убедитесь, что он содержит сумму 2785 и номер +992000992183.")
        return VERIFY_PAYMENT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Операция отменена. Введите /start для начала заново.")
    return ConversationHandler.END

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет доступа к админке.")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📁 Скачать логи", callback_data="admin_logs")]
    ]
    await update.message.reply_text("🔐 Панель администратора:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.edit_message_text("❌ У вас нет доступа.")
        return

    if query.data == "admin_stats":
        if not os.path.exists(LOG_FILE):
            await query.edit_message_text("📊 Пока нет данных.")
            return
        users = set()
        docs = checks = payments = 0
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 6: continue
                _, uid, _, action, _, result = row
                users.add(uid)
                if "документ" in action:
                    docs += 1
                if "регистрация" in result:
                    checks += 1
                if "чек" in action or "Чек подтверждён" in result:
                    payments += 1
        await query.edit_message_text(
            f"📊 Статистика:\n👥 Пользователей: {len(users)}\n📄 Документов: {docs}\n✅ Проверок: {checks}\n💸 Чеков: {payments}"
        )
    elif query.data == "admin_logs":
        if os.path.exists(LOG_FILE):
            await query.message.reply_document(InputFile(LOG_FILE, filename="logs.csv"))
        else:
            await query.edit_message_text("❌ Логи не найдены.")

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                CallbackQueryHandler(accept_rules, pattern="^accept_rules$"),
                CallbackQueryHandler(choose_check_document, pattern="^check_doc$"),
                CallbackQueryHandler(send_terms_file, pattern="^download_terms$"),
            ],
            CHECK_DOCUMENT: [MessageHandler(filters.Document.ALL, handle_document)],
            VERIFY_PAYMENT: [MessageHandler(filters.Document.ALL, verify_payment_document)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.run_polling()

if __name__ == "__main__":
    main()
