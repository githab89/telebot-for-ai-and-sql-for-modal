import os
import telebot
from groq import Groq
import modal
import sqlite3

# 1. Настраиваем облачное окружение и просим Modal автоматически установить библиотеки
bot_image = modal.Image.debian_slim().pip_install([
    "pyTelegramBotAPI",
    "groq"
])

# Создаем постоянное хранилище (Volume) для базы данных, чтобы пользователи не стирались
volume = modal.Volume.from_name("bot-db-volume", create_if_missing=True)

app = modal.App("my-telegram-bot", image=bot_image)

# Настройки токенов
TELEGRAM_TOKEN = 'your_key'
bot = telebot.TeleBot(TELEGRAM_TOKEN)

ai_client = Groq(
    api_key='your_key'
)

# Путь к базе данных внутри контейнера Modal
DB_DIR = "/data"
DB_PATH = os.path.join(DB_DIR, "users.db")

# Функция для инициализации базы данных
def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица пользователей (ID, хэш/пароль, и статус залогинен ли сейчас)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            password TEXT NOT NULL,
            is_logged_in INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# Функция для безопасного разделения длинного текста под лимиты Telegram
def send_long_message(chat_id, text, reply_to_id):
    max_length = 4000
    if len(text) <= max_length:
        bot.send_message(chat_id, text, reply_to_message_id=reply_to_id)
    else:
        for i in range(0, len(text), max_length):
            bot.send_message(chat_id, text[i:i+max_length])

# Проверка, авторизован ли пользователь в данный момент
def is_user_authenticated(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT is_logged_in FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None and result[0] == 1

# ХЕНДЛЕР: Команда /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Чтобы начать общение с ИИ, тебе нужно зарегистрироваться или войти.\n\n"
                          "🔑 Используй /register для регистрации\n"
                          "🔓 Используй /login для входа")

# ХЕНДЛЕР: Регистрация /register
@bot.message_handler(commands=['register'])
def register_user(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    user_exists = cursor.fetchone()
    conn.close()
    
    if user_exists:
        bot.reply_to(message, "Вы уже зарегистрированы! Используйте /login для входа.")
        return
        
    msg = bot.reply_to(message, "📝 Придумайте и отправьте пароль для регистрации:")
    bot.register_next_step_handler(msg, process_register_password)

def process_register_password(message):
    user_id = message.from_user.id
    password = message.text
    
    if not password or password.startswith('/'):
        bot.reply_to(message, "❌ Неверный формат пароля. Попробуйте /register снова.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Сохраняем пользователя и сразу ставим статус "залогинен" (1)
    cursor.execute("INSERT INTO users (user_id, password, is_logged_in) VALUES (?, ?, 1)", (user_id, password))
    conn.commit()
    conn.close()
    
    # Принудительно синхронизируем данные с облачным диском Modal
    volume.commit()
    
    bot.reply_to(message, "✅ Успешная регистрация! Вы автоматически вошли в систему. Можете общаться с ИИ!")

# ХЕНДЛЕР: Логин /login
@bot.message_handler(commands=['login'])
def login_user(message):
    user_id = message.from_user.id
    
    if is_user_authenticated(user_id):
        bot.reply_to(message, "Вы уже вошли в систему!")
        return

    msg = bot.reply_to(message, "🔓 Введите ваш пароль:")
    bot.register_next_step_handler(msg, process_login_password)

def process_login_password(message):
    user_id = message.from_user.id
    password = message.text

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    
    if result and result[0] == password:
        cursor.execute("UPDATE users SET is_logged_in = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        volume.commit()
        bot.reply_to(message, "✅ Вы успешно вошли! Приятного общения с ИИ.")
    else:
        conn.close()
        bot.reply_to(message, "❌ Неверный пароль или вы еще не зарегистрированы. Используйте /register.")

# ХЕНДЛЕР: Выход из профиля /logout
@bot.message_handler(commands=['logout'])
def logout_user(message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_logged_in = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    volume.commit()
    bot.reply_to(message, "🚪 Вы вышли из системы. Чтобы снова общаться с ИИ, введите /login.")

# ХЕНДЛЕР: Команда /support
@bot.message_handler(commands=['support'])
def send_support(message):
    print(f"\n🚨 СИГНАЛ: Пользователь {message.from_user.username} отправил запрос на поломку!")
    bot.reply_to(message, "⚠️ Запрос отправлен разработчику. Если бот завис, админ перезапустит его в течение пары минут.")

# ХЕНДЛЕР: Обычный текст и логирование ИИ
@bot.message_handler(content_types=['text'])
def talk_with_ai(message):
    user_id = message.from_user.id
    
    # ПРОВЕРКА: Если пользователь не авторизован, ИИ ему не отвечает
    if not is_user_authenticated(user_id):
        bot.reply_to(message, "🔒 Доступ заблокирован. Пожалуйста, сначала зарегистрируйтесь (/register) или войдите в систему (/login).")
        return

    print(f"\n[ЗАПРОС] От ID {message.from_user.username}: {message.text}")
    try:
        response = ai_client.chat.completions.create(
            model="qwen/qwen3.8-27b",
            messages=[
                {
                    "role": "system",
                    "content": "Ты — всезнающий, дружелюбный и крутой ИИ-ассистент. Общайся на равных, отвечай коротко и по делу. Ты обязан отвечать на любые вопросы пользователя: обычный разговор, точная математика, история или программирование. Пиши на русском языке и без спец символов"
                },
                {"role": "user", "content": message.text}
            ],
            max_tokens=500 
        )
        
        ai_reply = response.choices[0].message.content
        
        print(f"🤖 [ОТВЕТ ИИ]: {ai_reply}")
        print("="*50)
        
        send_long_message(message.chat.id, ai_reply, message.message_id)
        
    except Exception as e:
        print(f"❌ Ошибка генерации: {e}")
        bot.reply_to(message, "Ой, превышен лимит запросов в минуту или упала сеть. Подожди секунд 10 и повтори!")


# 3. ГОВОРИМ ХОСТИНГУ: Подключаем к функции наш постоянный диск и запускаем бота
@app.function(timeout=86400, volumes={DB_DIR: volume})
def run_bot():
    init_db()  # Создаем базу данных при старте, если её ещё нет
    print("Облачный ИИ-Бот на базе Groq и Modal запущен...")
    bot.infinity_polling()

# Точка запуска для развертывания в Modal
if __name__ == '__main__':
    with app.run():
        run_bot.remote()
