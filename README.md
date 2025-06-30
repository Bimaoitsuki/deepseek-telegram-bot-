# 🤖 DeepSeek Telegram AI Bot

Bot Telegram berbasis Python yang memanfaatkan [DeepSeek](https://deepseek.com/) API untuk menjawab pertanyaan pengguna, dengan dukungan penyimpanan riwayat percakapan lokal dan sistem pembatasan token harian.

---

## 🧠 Fitur Utama

- 🔁 Menyimpan riwayat percakapan pengguna ke SQLite
- 📊 Pemantauan penggunaan token per hari (per pengguna)
- 🧼 Sanitasi teks untuk Telegram Markdown
- ⚙️ Cache respons AI untuk pertanyaan pendek
- ⏳ Rate limiting untuk mencegah spam
- 💬 Dukungan loading bar dan respons asinkron
- 🔐 Mudah dikonfigurasi melalui variabel lingkungan

---

## 🚀 Cara Menjalankan

### 1. Clone repo dan masuk direktori
```bash
git clone https://github.com/username/deepseek-telegram-bot.git
cd deepseek-telegram-bot
