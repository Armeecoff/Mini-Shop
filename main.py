import os
import sqlite3
import json
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import telebot
from telebot.types import LabeledPrice

# Инициализация
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "ТВОЙ_ТГ_ID"))  # Твой ID в телеграме

bot = telebot.TeleBot(BOT_TOKEN)
app = FastAPI()
templates = Jinja2Templates(directory="templates")


# Инициализация БД
def init_db():
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    # Таблица товаров
    cursor.execute('''CREATE TABLE IF NOT EXISTS products 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, price_stars INTEGER, price_rub INTEGER, description TEXT, auto_data TEXT)''')
    # Таблица заказов
    cursor.execute('''CREATE TABLE IF NOT EXISTS orders 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, product_id INTEGER, status TEXT, type TEXT)''')
    conn.commit()
    conn.close()


init_db()


# --- МАРШРУТЫ MINI APP ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Получаем список товаров из БД
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products")
    products = [
        {"id": r[0], "title": r[1], "price_stars": r[2], "price_rub": r[3], "description": r[4], "auto_data": r[5]} for
        r in cursor.fetchall()]
    conn.close()

    return templates.TemplateResponse("index.html", {"request": request, "products": products, "admin_id": ADMIN_ID})


# Добавление товара админом
@app.post("/admin/add-product")
async def add_product(title: str = Form(...), price_stars: int = Form(...), price_rub: int = Form(...),
                      description: str = Form(...), auto_data: str = Form("")):
    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (title, price_stars, price_rub, description, auto_data) VALUES (?, ?, ?, ?, ?)",
        (title, price_stars, price_rub, description, auto_data))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "success"})


# Создание заказа / Оплата
@app.post("/buy")
async def buy_product(data: dict):
    user_id = data.get("user_id")
    username = data.get("username")
    product_id = data.get("product_id")
    pay_type = data.get("type")  # 'stars' или 'yoomoney'

    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if not product:
        return JSONResponse({"status": "error", "message": "Товар не найден"})

    p_title, p_stars, p_rub, _, auto_data = product[1], product[2], product[3], product[4], product[5]

    # 1. Оплата Звездами (Отправляем Invoice через бота)
    if pay_type == "stars":
        try:
            prices = [LabeledPrice(label=p_title, amount=p_stars)]
            # Создаем инвойс в диалоге с юзером
            bot.send_invoice(
                chat_id=user_id,
                title=p_title,
                description=f"Оплата товара в Telegram Stars",
                invoice_payload=f"prod_{product_id}",
                provider_token="",  # Для звезд токен провайдера пустой
                currency="XTR",  # Код валюты для Telegram Stars
                prices=prices
            )
            return JSONResponse({"status": "invoice_sent"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    # 2. Оплата через ЮMoney (Ручная проверка)
    elif pay_type == "yoomoney":
        # Создаем заказ со статусом "pending"
        cursor.execute(
            "INSERT INTO orders (user_id, username, product_id, status, type) VALUES (?, ?, ?, 'pending', 'yoomoney')",
            (user_id, username, product_id))
        order_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Уведомляем админа
        msg = f"🔔 **Новый запрос на покупку!**\nПользователь: @{username} ({user_id})\nТовар: {p_title}\nСпособ: ЮMoney ({p_rub} руб.)\n\nЧтобы подтвердить, перейдите в админку."
        bot.send_message(ADMIN_ID, msg)

        return JSONResponse({"status": "yoomoney_pending", "order_id": order_id})


# --- ХЕНДЛЕРЫ ТЕЛЕГРАМ БОТА ---

# Успешная оплата Звездами (Pre-checkout квери)
@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


# Выдача товара после оплаты звездами
@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    payload = message.successful_payment.invoice_payload
    product_id = payload.split("_")[1]

    conn = sqlite3.connect("shop.db")
    cursor = conn.cursor()
    cursor.execute("SELECT title, auto_data FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()

    if product and product[1]:  # Если есть автовыдача
        bot.send_message(message.chat.id, f"🎉 Спасибо за оплату товара '{product[0]}'!\n\nВаш товар:\n`{product[1]}`",
                         parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id,
                         f"🎉 Спасибо за оплату товара '{product[0]}'!\n\nЖдите, админ скоро свяжется с вами для выдачи.")
        bot.send_message(ADMIN_ID,
                         f"💰 Товар '{product[0]}' оплачен Звездами пользователем @{message.from_user.username}!")
    conn.close()


# Запуск бота в фоновом потоке
import threading

threading.Thread(target=bot.infinity_polling, daemon=True).start()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)