# bot_server.py
# Telegram SCORM bot + simple Flask static server
# Reads TOKEN and BASE_URL from environment variables.

import os
import io
import zipfile
import secrets
import shutil
import threading
import json
import datetime
from pathlib import Path
from flask import Flask, send_from_directory, abort, request, redirect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# --- CONFIG (from env) ---
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN environment variable is required")

BASE_URL = os.environ.get("BASE_URL", "https://your-service.onrender.com")
PORT = int(os.environ.get("PORT", 5000))

DATA_DIR = Path("data/courses")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path("data/courses_db.json")
MAX_ZIP_SIZE = 200 * 1024 * 1024
DISALLOWED_EXTS = {".exe", ".dll", ".bat", ".sh", ".com", ".py"}

app = Flask(__name__)
COURSES = {}
TEMP_UPLOADS = {}
ASK_NUMBER, ASK_TITLE = range(2)

# Persistence
def load_db():
    global COURSES
    if DB_PATH.exists():
        try:
            COURSES = json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            COURSES = {}

def save_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DB_PATH.write_text(json.dumps(COURSES, ensure_ascii=False, indent=2), encoding="utf-8")

load_db()

# ---- Flask routes ----
@app.route("/courses/<course_id>/<path:filename>")
def serve_course_file(course_id, filename):
    meta = COURSES.get(course_id)
    if not meta:
        return abort(404)
    token = request.args.get("token")
    if not token or token != meta.get("token"):
        return abort(403)
    course_path = Path(meta["path"])
    requested = (course_path / filename).resolve()
    if not str(requested).startswith(str(course_path.resolve())):
        return abort(403)
    if not requested.exists():
        return abort(404)
    return send_from_directory(course_path, filename)

@app.route("/courses/<course_id>/")
def serve_course_index(course_id):
    meta = COURSES.get(course_id)
    if not meta:
        return abort(404)
    token = request.args.get("token")
    if not token or token != meta.get("token"):
        return abort(403)
    course_path = Path(meta["path"])
    for index_name in ("trainer/custom_trainer_cp/index.html", "context.html", "index.html", "help/en-US/contents/start.htm"):
        p = course_path / index_name
        if p.exists():
            rel = str(Path(index_name).as_posix())
            return redirect(f"/courses/{course_id}/{rel}?token={token}")
    files = sorted([str(p.relative_to(course_path).as_posix()) for p in course_path.rglob("*.html")])
    html = "<h3>Available HTML files:</h3>" + "<br>".join(
        f"<a href='/courses/{course_id}/{f}?token={meta['token']}' target='_blank'>{f}</a>" for f in files
    )
    return html

# ---- Telegram handlers ----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 Пришли ZIP (SCORM/H5P) как документ. Я распакую и попрошу ввести номер и название.")

async def recv_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    if not doc:
        await msg.reply_text("Пожалуйста, отправь файл как документ (.zip).")
        return ConversationHandler.END
    if doc.file_size and doc.file_size > MAX_ZIP_SIZE:
        await msg.reply_text("Файл слишком большой.")
        return ConversationHandler.END
    fname = doc.file_name or "upload.zip"
    if not fname.lower().endswith(".zip"):
        await msg.reply_text("Поддерживаются только .zip архивы.")
        return ConversationHandler.END
    file = await context.bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    bio.seek(0)
    TEMP_UPLOADS[update.effective_chat.id] = {"bytes": bio, "filename": fname, "uploader": update.effective_user.id}
    await msg.reply_text("Файл получен. Введи НОМЕР (короткий идентификатор) для этого файла:")
    return ASK_NUMBER

async def ask_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in TEMP_UPLOADS:
        await update.message.reply_text("Нет загруженного файла. Пришли ZIP заново.")
        return ConversationHandler.END
    TEMP_UPLOADS[chat_id]["number"] = update.message.text.strip()
    await update.message.reply_text("Окей. Теперь пришли НАЗВАНИЕ для этого файла:")
    return ASK_TITLE

async def ask_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in TEMP_UPLOADS:
        await update.message.reply_text("Нет загруженного файла. Пришли ZIP заново.")
        return ConversationHandler.END
    temp = TEMP_UPLOADS.pop(chat_id)
    number = temp.get("number","")
    title = update.message.text.strip()
    bio = temp["bytes"]
    uploader = temp.get("uploader")
    course_id = secrets.token_urlsafe(8)
    course_dir = DATA_DIR / course_id
    course_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(bio) as z:
            for member in z.infolist():
                nm = member.filename
                # safety
                if nm.startswith("/") or ".." in Path(nm).parts:
                    continue
                ext = Path(nm).suffix.lower()
                if ext in DISALLOWED_EXTS:
                    continue
                target = course_dir / nm
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except Exception as e:
        shutil.rmtree(course_dir, ignore_errors=True)
        await update.message.reply_text("Ошибка при распаковке архива.")
        return ConversationHandler.END
    token = secrets.token_urlsafe(24)
    meta = {
        "id": course_id,
        "owner": uploader,
        "number": number,
        "title": title,
        "filename": temp.get("filename"),
        "path": str(course_dir.resolve()),
        "token": token,
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    COURSES[course_id] = meta
    save_db()
    webapp_url = f"{BASE_URL}/courses/{course_id}/?token={token}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть курс (Mini App)", web_app=WebAppInfo(url=webapp_url))]])
    await update.message.reply_text(f"Курс сохранён.\nID: {course_id}\nНомер: {number}\nНазвание: {title}", reply_markup=kb)
    return ConversationHandler.END

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_courses = [c for c in COURSES.values() if c.get("owner") == user.id]
    if not user_courses:
        await update.message.reply_text("У тебя нет загруженных курсов.")
        return
    keyboard = []
    text_lines = []
    for c in sorted(user_courses, key=lambda x: x.get("created_at",""), reverse=True):
        text_lines.append(f"{c.get('number','')} — {c.get('title','')} (ID: {c.get('id')})")
        open_url = f"{BASE_URL}/courses/{c['id']}/?token={c['token']}"
        keyboard.append([InlineKeyboardButton(f"Открыть: {c.get('number','')} — {c.get('title','')}", web_app=WebAppInfo(url=open_url))])
    await update.message.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(keyboard))

async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip().lower()
    if not query:
        await update.message.reply_text("Использование: /find <номер или часть названия>")
        return
    user = update.effective_user
    user_courses = [c for c in COURSES.values() if c.get("owner") == user.id]
    found = [c for c in user_courses if query in (c.get("number","").lower() + " " + c.get("title","").lower())]
    if not found:
        await update.message.reply_text("Ничего не найдено.")
        return
    keyboard = []
    text_lines = []
    for c in found:
        text_lines.append(f"{c.get('number','')} — {c.get('title','')} (ID: {c.get('id')})")
        open_url = f"{BASE_URL}/courses/{c['id']}/?token={c['token']}"
        keyboard.append([InlineKeyboardButton(f"Открыть: {c.get('number','')} — {c.get('title','')}", web_app=WebAppInfo(url=open_url))])
    await update.message.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(keyboard))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    TEMP_UPLOADS.pop(chat_id, None)
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ---- Run Flask + Bot ----
def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

def main():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    application = ApplicationBuilder().token(TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL & ~filters.COMMAND, recv_document)],
        states={
            ASK_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_number)],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_title)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
    )
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(CommandHandler("find", find_cmd))
    application.add_handler(CommandHandler("cancel", cancel))
    application.run_polling()

if __name__ == "__main__":
    main()
