import os
import logging
import json
import asyncio
import aiohttp
import re
import sqlite3
from datetime import datetime
from collections import defaultdict
from cachetools import TTLCache, LRUCache
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ===== KONFIGURASI =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7648302703:AAHrs-CMJPoGK9D3BcEz6yDXtRjz59VFEc4")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-4cf71c581d068298df2c")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
DATABASE_NAME = "chat_history.db"
TOKEN_LIMIT_PER_DAY = 10000  # Batas token harian per user

# === LOGGING ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === CACHE & RATE LIMITING ===
user_request_timestamps = {}
RATE_LIMIT = 5

# Cache untuk respons AI yang sering muncul (LRU Cache dengan maks 1000 item)
response_cache = LRUCache(maxsize=1000)

# Cache untuk hitungan token harian (TTL 24 jam)
daily_token_cache = TTLCache(maxsize=1000, ttl=86400)

# === DATABASE SETUP ===
def init_database():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    # Tabel percakapan
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        tokens INTEGER DEFAULT 0
    )
    ''')
    
    # Tabel untuk statistik token
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date DATE NOT NULL,
        tokens_used INTEGER DEFAULT 0,
        UNIQUE(user_id, date)
    )
    ''')
    
    # Indeks untuk pencarian lebih cepat
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON conversations (user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations (timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_token_usage ON token_usage (user_id, date)')
    
    conn.commit()
    conn.close()

def get_user_messages(user_id: int, limit: int = 10):
    """Ambil riwayat percakapan user dari database"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT role, content 
    FROM conversations 
    WHERE user_id = ? 
    ORDER BY timestamp DESC 
    LIMIT ?
    ''', (user_id, limit))
    
    messages = [{"role": row[0], "content": row[1]} for row in cursor.fetchall()]
    messages.reverse()  # Urutkan dari yang terlama
    
    conn.close()
    return messages

def save_message(user_id: int, role: str, content: str, tokens: int = 0):
    """Simpan pesan ke database"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO conversations (user_id, role, content, tokens)
    VALUES (?, ?, ?, ?)
    ''', (user_id, role, content, tokens))
    
    # Update statistik token harian
    today = datetime.now().date().isoformat()
    cursor.execute('''
    INSERT OR IGNORE INTO token_usage (user_id, date, tokens_used)
    VALUES (?, ?, 0)
    ''', (user_id, today))
    
    cursor.execute('''
    UPDATE token_usage
    SET tokens_used = tokens_used + ?
    WHERE user_id = ? AND date = ?
    ''', (tokens, user_id, today))
    
    conn.commit()
    conn.close()

def clear_user_conversation(user_id: int):
    """Hapus riwayat percakapan user"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
    DELETE FROM conversations 
    WHERE user_id = ?
    ''', (user_id,))
    
    conn.commit()
    conn.close()

def get_daily_token_usage(user_id: int):
    """Dapatkan penggunaan token hari ini"""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    
    today = datetime.now().date().isoformat()
    cursor.execute('''
    SELECT tokens_used FROM token_usage
    WHERE user_id = ? AND date = ?
    ''', (user_id, today))
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else 0

# === FUNGSI UTILITAS ===
def sanitize_text(text: str) -> str:
    """Sanitasi teks untuk menghindari parsing error di Telegram"""
    text = re.sub(r'([*])', r'\1', text)
    return text[:4000]

async def send_message(update: Update, text: str):
    """Mengirim pesan dengan teks yang sudah disanitasi"""
    sanitized_text = sanitize_text(text)
    await update.message.reply_text(
        sanitized_text,
        parse_mode="Markdown",
        reply_to_message_id=update.message.message_id,
        disable_web_page_preview=True
    )

async def show_loading_bar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan loading bar animasi"""
    message = await update.message.reply_text("🔄 Memproses...")
    context.chat_data['loading_message'] = message
    
    for i in range(30):
        if context.chat_data.get('stop_loading', False):
            break
            
        try:
            await message.edit_text(
                f"⏱️ {i//2}.{i%2*5}s"
            )
        except:
            break
            
        await asyncio.sleep(0.5)
    
    return message

async def remove_loading_message(context: ContextTypes.DEFAULT_TYPE):
    """Menghapus pesan loading jika ada"""
    if 'loading_message' in context.chat_data:
        try:
            await context.chat_data['loading_message'].delete()
        except:
            pass
        del context.chat_data['loading_message']

def estimate_tokens(text: str) -> int:
    """Estimasi jumlah token dari teks (sederhana)"""
    # Rata-rata 1 token ~ 4 karakter dalam bahasa Inggris
    # Untuk bahasa lain mungkin berbeda, ini estimasi kasar
    return max(1, len(text) // 4)

async def call_deepseek_api(user_id: int, prompt: str, context: ContextTypes.DEFAULT_TYPE):
    """Panggil DeepSeek API dengan timeout"""
    # Cek cache terlebih dahulu
    cache_key = (user_id, prompt)
    if cache_key in response_cache:
        logger.info("Menggunakan respons dari cache")
        return response_cache[cache_key]
    
    # Cek batas token harian
    daily_usage = get_daily_token_usage(user_id)
    if daily_usage >= TOKEN_LIMIT_PER_DAY:
        return {"error": "Batas token harian telah tercapai"}
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    
    # Ambil riwayat percakapan dari database
    conversation = get_user_messages(user_id, limit=10)
    
    # Jika belum ada percakapan, tambahkan system prompt
    if not conversation:
        conversation = [
            {"role": "system", "content": "Anda adalah asisten AI yang membantu pengguna Telegram."}
        ]
        save_message(user_id, "system", conversation[0]["content"])
    
    # Hitung estimasi token untuk request
    prompt_tokens = estimate_tokens(prompt)
    conversation_tokens = sum(estimate_tokens(msg["content"]) for msg in conversation)
    total_estimated_tokens = prompt_tokens + conversation_tokens + 100  # Buffer untuk metadata
    
    if daily_usage + total_estimated_tokens > TOKEN_LIMIT_PER_DAY:
        return {"error": "Permintaan ini akan melebihi batas token harian"}
    
    payload = {
        "model": "deepseek-chat",
        "messages": conversation + [{"role": "user", "content": prompt}],
        "temperature": 0.5,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                DEEPSEEK_API_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                
                if resp.status != 200:
                    error_msg = await resp.text()
                    logger.error(f"API Error {resp.status}: {error_msg}")
                    return {"error": f"API Error {resp.status}"}
                
                data = await resp.json()
                if "choices" in data:
                    ai_reply = data["choices"][0]["message"]["content"]
                    # Simpan balasan AI ke database
                    reply_tokens = data.get("usage", {}).get("completion_tokens", estimate_tokens(ai_reply))
                    save_message(user_id, "assistant", ai_reply, reply_tokens)
                    
                    # Simpan ke cache jika relevan
                    if len(prompt) < 100:  # Hanya cache prompt pendek
                        response_cache[cache_key] = data
                    
                    # Update cache token harian
                    daily_token_cache[(user_id, datetime.now().date())] = daily_usage + reply_tokens
                
                return data
                
    except asyncio.TimeoutError:
        logger.error("API Timeout")
        return {"error": "Timeout"}
    except Exception as e:
        logger.error(f"API Connection Error: {e}")
        return {"error": str(e)}

# === HANDLER COMMAND ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Set system prompt di database
    clear_user_conversation(user_id)
    save_message(user_id, "system", "Anda adalah asisten AI yang membantu pengguna Telegram.")
    
    await send_message(update,
        "🤖 *DeepSeek AI Bot*\n\n"
        "Percakapan Anda akan disimpan di database lokal.\n"
        "Ketik /clear untuk memulai percakapan baru.\n"
        "Ketik /history untuk melihat riwayat percakapan.\n"
        "Ketik /tokens untuk melihat penggunaan token harian."
    )

async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_user_conversation(user_id)
    await send_message(update, "🔄 *Percakapan telah direset*")

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    messages = get_user_messages(user_id, limit=20)
    
    if not messages:
        await send_message(update, "📜 *Riwayat percakapan kosong*")
        return
    
    history_text = "📜 *Riwayat Percakapan Terakhir:*\n\n"
    for msg in messages[-5:]:  # Tampilkan hanya 5 pesan terakhir
        role = "Anda" if msg["role"] == "user" else "AI"
        history_text += f"*{role}:* {msg['content'][:200]}\n\n"
    
    await send_message(update, history_text)

async def show_token_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    daily_usage = get_daily_token_usage(user_id)
    
    await send_message(update,
        f"🧮 *Penggunaan Token Harian:*\n\n"
        f"• Token digunakan hari ini: {daily_usage}\n"
        f"• Batas harian: {TOKEN_LIMIT_PER_DAY}\n"
        f"• Persentase: {daily_usage/TOKEN_LIMIT_PER_DAY*100:.1f}%\n\n"
        f"Statistik direset setiap hari pada tengah malam UTC."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Rate limiting
    now = asyncio.get_event_loop().time()
    timestamps = user_request_timestamps.setdefault(user_id, [])
    timestamps = [t for t in timestamps if now - t < 60]
    
    if len(timestamps) >= RATE_LIMIT:
        await send_message(update, "⏳ *Terlalu banyak permintaan!* Tunggu 1 menit.")
        return
    
    timestamps.append(now)
    user_request_timestamps[user_id] = timestamps
    
    # Proses pesan dengan loading bar
    await update.message.reply_chat_action("typing")
    context.chat_data['stop_loading'] = False
    loading_task = asyncio.create_task(show_loading_bar(update, context))
    
    try:
        response = await call_deepseek_api(user_id, update.message.text, context)
        context.chat_data['stop_loading'] = True
        await loading_task
        await remove_loading_message(context)
        
        if "error" in response:
            await send_message(update, "❌ *Gagal memproses*: " + response["error"])
        else:
            answer = response["choices"][0]["message"]["content"]
            await send_message(update, answer)
            
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        context.chat_data['stop_loading'] = True
        await remove_loading_message(context)
        await send_message(update, "⚠️ *Terjadi kesalahan*")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=context.error)
    if update and isinstance(update, Update):
        context.chat_data['stop_loading'] = True
        if 'loading_message' in context.chat_data:
            await remove_loading_message(context)
        await send_message(update, "⚠️ *Terjadi kesalahan sistem*")

def main():
    # Inisialisasi database
    init_database()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_chat))
    app.add_handler(CommandHandler("history", show_history))
    app.add_handler(CommandHandler("tokens", show_token_usage))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🤖 Bot starting with enhanced features...")
    app.run_polling()

if __name__ == "__main__":
    main()
