import os
import sqlite3
import threading
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import telebot
from telebot.types import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

# Настройки (Railway подтянет их из переменных окружения)
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789")) 

bot = telebot.TeleBot(BOT_TOKEN)
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Функция безопасного подключения к БД (чтобы потоки FastAPI и Telebot не ругались)
def db_conn():
    conn = sqlite3.connect("shop_v2.db", check_same_thread=False)
    return conn, conn.cursor()

# Создаем таблицы, если их нет
def init_db():
    conn, cursor = db_conn()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            price_stars INTEGER,
            price_rub INTEGER,
            description TEXT,
            auto_data TEXT,
            media_url TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            product_id INTEGER,
            status TEXT,
            type TEXT,
            charge_id TEXT
        );
    ''')
    conn.commit()
    conn.close()

init_db()

# --- WEB APP API ROUTERS ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn, cursor = db_conn()
    cursor.execute("SELECT * FROM products")
    rows = cursor.fetchall()
    conn.close()
    
    products = [
        {"id": r[0], "title": r[1], "price_stars": r[2], "price_rub": r[3], "description": r[4], "auto_data": r[5], "media_url": r[6]}
        for r in rows
    ]
    return templates.TemplateResponse("index.html", {"request": request, "products": products, "admin_id": ADMIN_ID})

# Создание инвойса Stars или отправка запроса ЮMoney
@app.post("/api/buy")
async def buy_item(data: dict):
    user_id = int(data.get("user_id"))
    username = data.get("username", "unknown")
    product_id = int(data.get("product_id"))
    pay_type = data.get("type") # 'stars' или 'yoomoney'
    
    conn, cursor = db_conn()
    cursor.execute("SELECT title, price_stars, price_rub, description FROM products WHERE id=?", (product_id,))
    prod = cursor.fetchone()
    
    if not prod:
        conn.close()
        return JSONResponse({"error": "Товар не найден"}, status_code=404)
        
    title, price_stars, price_rub, desc = prod
    
    if pay_type == "stars":
        conn.close()
        try:
            # Шлем официальный счет в Stars
            bot.send_invoice(
                chat_id=user_id,
                title=title,
                description=desc[:250],
                invoice_payload=f"stars_{product_id}",
                provider_token="", # Для звезд всегда пусто
                currency="XTR", 
                prices=[LabeledPrice(label=title, amount=price_stars)]
            )
            return {"status": "invoice_sent"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)
            
    elif pay_type == "yoomoney":
        # Создаем заказ со статусом "ожидает ручной проверки"
        cursor.execute(
            "INSERT INTO orders (user_id, username, product_id, status, type) VALUES (?, ?, ?, 'pending_check', 'yoomoney')",
            (user_id, username, product_id)
        )
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Кнопки для админа
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Подтвердить и Выдать", callback_data=f"adm_confirm_{order_id}"))
        kb.add(InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_decline_{order_id}"))
        
        bot.send_message(
            ADMIN_ID, 
            f"💰 **Заявка ЮMoney №{order_id}**\nПользователь: @{username} ({user_id})\nТовар: {title}\nСумма к проверке: {price_rub} руб.\n\nПроверьте кошелек и подтвердите выдачу.",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return {"status": "yoomoney_submitted"}

# Полное управление товарами (Добавление / Редактирование)
@app.post("/api/admin/save-product")
async def save_product(
    id: str = Form("new"), title: str = Form(...), price_stars: int = Form(...),
    price_rub: int = Form(...), description: str = Form(...), 
    auto_data: str = Form(""), media_url: str = Form("")
):
    conn, cursor = db_conn()
    if id == "new":
        cursor.execute(
            "INSERT INTO products (title, price_stars, price_rub, description, auto_data, media_url) VALUES (?, ?, ?, ?, ?, ?)",
            (title, price_stars, price_rub, description, auto_data, media_url)
        )
    else:
        cursor.execute(
            "UPDATE products SET title=?, price_stars=?, price_rub=?, description=?, auto_data=?, media_url=? WHERE id=?",
            (title, price_stars, price_rub, description, auto_data, media_url, int(id))
        )
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- TELEGRAM BOT LOGIC ---

# 1. Автоматическая пре-проверка платежа Stars
@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# 2. Успешный платеж Stars -> Автовыдача
@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    pmnt = message.successful_payment
    payload = pmnt.invoice_payload # stars_ID
    product_id = int(payload.split("_")[1])
    charge_id = pmnt.telegram_payment_charge_id # ID транзакции для рефанда
    
    conn, cursor = db_conn()
    cursor.execute("SELECT title, auto_data FROM products WHERE id=?", (product_id,))
    prod = cursor.fetchone()
    
    # Логируем транзакцию в базу данных
    cursor.execute(
        "INSERT INTO orders (user_id, username, product_id, status, type, charge_id) VALUES (?, ?, ?, 'paid', 'stars', ?)",
        (message.from_user.id, message.from_user.username, product_id, charge_id)
    )
    conn.commit()
    conn.close()
    
    if prod:
        title, auto_data = prod
        if auto_data:
            bot.send_message(message.chat.id, f"🎉 **Оплата получена!**\nВаш товар '{title}':\n\n`{auto_data}`", parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, f"🎉 **Оплата получена!**\nТовар: '{title}'\n\n_Автовыдача не настроена. Админ свяжется с вами в ближайшее время!_", parse_mode="Markdown")
            bot.send_message(ADMIN_ID, f"🔔 Юзер @{message.from_user.username} купил '{title}' за Stars. Нужна ручная выдача!")

# 3. Коллбеки кнопок Админа (Подтверждение ЮMoney)
@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_"))
def admin_buttons(call):
    action, _, order_id = call.data.split("_")
    conn, cursor = db_conn()
    
    cursor.execute(
        "SELECT orders.user_id, products.title, products.auto_data FROM orders JOIN products ON orders.product_id = products.id WHERE orders.id=?", 
        (int(order_id),)
    )
    order_data = cursor.fetchone()
    
    if not order_data:
        bot.answer_callback_query(call.id, "Заказ не найден.")
        conn.close()
        return

    user_id, title, auto_data = order_data

    if action == "confirm":
        cursor.execute("UPDATE orders SET status='paid' WHERE id=?", (int(order_id),))
        conn.commit()
        bot.answer_callback_query(call.id, "Заказ одобрен!")
        bot.edit_message_text(f"✅ Заказ №{order_id} одобрен. Товар выдан.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        
        if auto_data:
            bot.send_message(user_id, f"✅ **Ваш платеж по ЮMoney проверен!**\nКупленный товар '{title}':\n\n`{auto_data}`", parse_mode="Markdown")
        else:
            bot.send_message(user_id, f"✅ **Ваш платеж по ЮMoney проверен!**\nТовар: '{title}'\n\nСкоро админ напишет вам для передачи товара!")
            
    elif action == "decline":
        cursor.execute("UPDATE orders SET status='declined' WHERE id=?", (int(order_id),))
        conn.commit()
        bot.answer_callback_query(call.id, "Заказ отклонен.")
        bot.edit_message_text(f"❌ Заказ №{order_id} отклонен админом.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        bot.send_message(user_id, f"❌ Ваш платеж за товар '{title}' отклонен админом. Если произошла ошибка, свяжитесь с поддержкой.")
        
    conn.close()

# 4. Команда /dse для манибэка звезд
@bot.message_handler(commands=['dse'])
def refund_stars_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        # Панель вызова: /dse charge_id
        _, charge_id = message.text.split()
        
        # Делаем официальный возврат через API Telegram Stars
        bot.refund_star_payment(user_id=message.from_user.id, telegram_payment_charge_id=charge_id)
        bot.reply_to(message, f"✅ Успешный возврат звезд по транзакции:\n`{charge_id}`", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка возврата. Проверьте format или ID транзакции.\nТекст ошибки: {e}")

# Простой эхо-реплай для диалогов (Админ отвечает на сообщение -> юзер получает ответ)
@bot.message_handler(func=lambda msg: True)
def handle_support_chat(message):
    if message.from_user.id == ADMIN_ID:
        if message.reply_to_message and "User_ID:" in message.reply_to_message.text:
            try:
                # Извлекаем ID юзера из текста исходного сообщения
                lines = message.reply_to_message.text.split("\n")
                target_user_id = int([line for line in lines if "User_ID:" in line][0].split(":")[1].strip())
                bot.send_message(target_user_id, f"💬 **Ответ от администратора:**\n\n{message.text}", parse_mode="Markdown")
                bot.reply_to(message, "✈️ Отправлено пользователю!")
            except Exception as e:
                bot.reply_to(message, f"Не удалось отправить ответ: {e}")
    else:
        # Если пишет обычный юзер — пересылаем админу в виде удобной карточки для реплая
        bot.send_message(
            ADMIN_ID, 
            f"✉️ **Новое сообщение в техподдержку!**\nОт: @{message.from_user.username}\nUser_ID: {message.from_user.id}\n\nТекст:\n{message.text}\n\n_(Используйте Reply/Ответить, чтобы написать ему)_"
        )
        bot.reply_to(message, "📨 Ваше сообщение передано админу. Скоро вы получите ответ прямо здесь!")

# Поток бота
threading.Thread(target=bot.infinity_polling, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
