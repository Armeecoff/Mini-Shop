import os
import sqlite3
import threading
import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import telebot
from telebot import apihelper
from telebot.types import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN", "ТОКЕН")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "123456789"))

bot       = telebot.TeleBot(BOT_TOKEN)
app       = FastAPI()
templates = Jinja2Templates(directory="templates")

# ── DB ─────────────────────────────────────────────────────────────
def db_conn():
    conn = sqlite3.connect("shop_v3.db", check_same_thread=False)
    return conn, conn.cursor()

def init_db():
    conn, cursor = db_conn()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, price_stars INTEGER, price_rub INTEGER,
            description TEXT, auto_data TEXT, media_url TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, product_id INTEGER,
            status TEXT, type TEXT, charge_id TEXT, amount INTEGER, date TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, text TEXT,
            direction TEXT, date TEXT, is_read INTEGER DEFAULT 0
        );
    ''')
    conn.commit(); conn.close()

init_db()

def register_user(user_id, username):
    conn, cursor = db_conn()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?,?,0)", (user_id, username))
    if username:
        cursor.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    conn.commit(); conn.close()

def save_message(user_id, username, text, direction):
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn, cursor = db_conn()
    cursor.execute(
        "INSERT INTO messages (user_id,username,text,direction,date,is_read) VALUES (?,?,?,?,?,?)",
        (user_id, username, text, direction, date_str, 1 if direction=="out" else 0)
    )
    conn.commit(); conn.close()

def contact_admin_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💬 Связаться с поддержкой", callback_data="contact_admin"))
    return kb

# ── FASTAPI ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "admin_id": ADMIN_ID})

@app.get("/api/products")
async def get_products():
    conn, cursor = db_conn()
    cursor.execute("SELECT id,title,price_stars,price_rub,description,auto_data,media_url FROM products")
    cols = ["id","title","stars","rub","desc","data","media"]
    prods = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return prods

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: int):
    conn, cursor = db_conn()
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    res = cursor.fetchone()
    balance = res[0] if res else 0
    cursor.execute(
        "SELECT orders.id,products.title,orders.status,orders.type,orders.amount,orders.date,orders.charge_id "
        "FROM orders LEFT JOIN products ON orders.product_id=products.id "
        "WHERE orders.user_id=? ORDER BY orders.id DESC", (user_id,)
    )
    history = [dict(zip(["id","title","status","type","amount","date","charge_id"], r)) for r in cursor.fetchall()]
    conn.close()
    return {"balance": balance, "history": history}

@app.get("/api/user/messages/{uid}")
async def get_user_messages(uid: int):
    conn, cursor = db_conn()
    cursor.execute(
        "SELECT id,user_id,username,text,direction,date FROM messages WHERE user_id=? ORDER BY id ASC",
        (uid,)
    )
    msgs = [dict(zip(["id","user_id","username","text","direction","date"], r)) for r in cursor.fetchall()]
    cursor.execute("UPDATE messages SET is_read=1 WHERE user_id=? AND direction='out'", (uid,))
    conn.commit(); conn.close()
    return {"messages": msgs}

@app.post("/api/user/send-message")
async def user_send_message(data: dict):
    uid      = int(data.get("user_id", 0))
    username = data.get("username", str(uid))
    text     = data.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)
    register_user(uid, username)
    save_message(uid, username, text, "in")
    try:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💬 Ответить напрямую", url=f"tg://user?id={uid}"))
        bot.send_message(
            ADMIN_ID,
            f"✉️ *Новое сообщение*\nОт: @{username} (`{uid}`)\n\n{text}",
            parse_mode="Markdown", reply_markup=kb
        )
    except: pass
    return {"status": "sent"}

@app.get("/api/admin/data")
async def get_admin_data():
    conn, cursor = db_conn()
    cursor.execute(
        "SELECT orders.id,orders.username,products.title,orders.type,orders.status,orders.amount,orders.charge_id "
        "FROM orders LEFT JOIN products ON orders.product_id=products.id ORDER BY orders.id DESC LIMIT 50"
    )
    txs = [dict(zip(["id","username","title","type","status","amount","charge_id"], r)) for r in cursor.fetchall()]
    cursor.execute("SELECT user_id,username,balance FROM users ORDER BY balance DESC LIMIT 50")
    users = [dict(zip(["user_id","username","balance"], r)) for r in cursor.fetchall()]
    conn.close()
    return {"transactions": txs, "users": users}

@app.get("/api/admin/dialogs")
async def get_admin_dialogs():
    conn, cursor = db_conn()
    cursor.execute("""
        SELECT m.user_id, m.username,
               (SELECT text FROM messages WHERE user_id=m.user_id ORDER BY id DESC LIMIT 1) as last_msg,
               (SELECT date FROM messages WHERE user_id=m.user_id ORDER BY id DESC LIMIT 1) as last_date,
               (SELECT COUNT(*) FROM messages WHERE user_id=m.user_id AND direction='in' AND is_read=0) as unread
        FROM messages m GROUP BY m.user_id ORDER BY last_date DESC
    """)
    dialogs = [dict(zip(["user_id","username","last_msg","last_date","unread"], r)) for r in cursor.fetchall()]
    conn.close()
    return {"dialogs": dialogs}

@app.get("/api/admin/dialog/{user_id}")
async def get_dialog(user_id: int):
    conn, cursor = db_conn()
    cursor.execute(
        "SELECT id,user_id,username,text,direction,date FROM messages WHERE user_id=? ORDER BY id ASC",
        (user_id,)
    )
    msgs = [dict(zip(["id","user_id","username","text","direction","date"], r)) for r in cursor.fetchall()]
    cursor.execute("UPDATE messages SET is_read=1 WHERE user_id=? AND direction='in'", (user_id,))
    conn.commit(); conn.close()
    return {"messages": msgs}

@app.post("/api/admin/reply")
async def admin_reply(data: dict):
    target_id = int(data.get("user_id"))
    text      = data.get("text", "")
    if not text:
        return JSONResponse({"error": "Пустое сообщение"}, status_code=400)
    save_message(target_id, "admin", text, "out")
    try:
        bot.send_message(target_id, f"💬 *Сообщение от поддержки:*\n\n{text}", parse_mode="Markdown")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"status": "sent"}

@app.post("/api/buy")
async def buy_item(data: dict):
    user_id    = int(data.get("user_id"))
    username   = data.get("username", "unknown")
    product_id = int(data.get("product_id"))
    pay_type   = data.get("type")

    register_user(user_id, username)
    conn, cursor = db_conn()
    cursor.execute("SELECT title,price_stars,price_rub,auto_data FROM products WHERE id=?", (product_id,))
    prod = cursor.fetchone()
    if not prod:
        conn.close()
        return JSONResponse({"error": "Товар не найден"}, status_code=404)

    title, price_stars, price_rub, auto_data = prod
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if pay_type == "balance":
        cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if not row or row[0] < price_rub:
            conn.close()
            return JSONResponse({"error": "Недостаточно баланса"}, status_code=400)
        cursor.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (price_rub, user_id))
        cursor.execute(
            "INSERT INTO orders (user_id,username,product_id,status,type,amount,date) VALUES (?,?,?,'paid','balance',?,?)",
            (user_id, username, product_id, price_rub, date_str)
        )
        conn.commit(); conn.close()
        msg = f"🎉 *Покупка оформлена!*\nТовар: *{title}*\n\n"
        msg += f"`{auto_data}`" if auto_data else "Администратор свяжется с вами для выдачи."
        try: bot.send_message(user_id, msg, parse_mode="Markdown", reply_markup=contact_admin_kb())
        except: pass
        if not auto_data:
            try: bot.send_message(ADMIN_ID, f"🔔 @{username} купил *{title}* с баланса. Нужна выдача!", parse_mode="Markdown")
            except: pass
        return {"status": "success"}

    elif pay_type == "stars":
        conn.close()
        try:
            bot.send_invoice(
                user_id, title, f"Оплата {title}", f"stars_{product_id}", "", "XTR",
                [LabeledPrice(title, price_stars)]
            )
        except: pass
        return {"status": "invoice_sent"}

    elif pay_type == "yoomoney":
        cursor.execute(
            "INSERT INTO orders (user_id,username,product_id,status,type,amount,date) VALUES (?,?,?,'pending','yoomoney',?,?)",
            (user_id, username, product_id, price_rub, date_str)
        )
        order_id = cursor.lastrowid
        conn.commit(); conn.close()
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{order_id}"))
        kb.add(InlineKeyboardButton("❌ Отклонить",   callback_data=f"decline_{order_id}"))
        try:
            bot.send_message(
                ADMIN_ID,
                f"💰 *Заявка ЮMoney №{order_id}*\nЮзер: @{username} ({user_id})\nТовар: {title}\nСумма: {price_rub} руб.",
                reply_markup=kb, parse_mode="Markdown"
            )
            bot.send_message(
                user_id,
                f"⏳ *Заявка принята!*\nТовар: *{title}*\n\nОжидайте подтверждения администратора.",
                parse_mode="Markdown", reply_markup=contact_admin_kb()
            )
        except: pass
        return {"status": "pending"}

@app.post("/api/admin/save-product")
async def save_product(
    id: str = Form("new"), title: str = Form(...), price_stars: int = Form(...),
    price_rub: int = Form(...), description: str = Form(...),
    auto_data: str = Form(""), media_url: str = Form("")
):
    conn, cursor = db_conn()
    if id == "new":
        cursor.execute(
            "INSERT INTO products (title,price_stars,price_rub,description,auto_data,media_url) VALUES (?,?,?,?,?,?)",
            (title, price_stars, price_rub, description, auto_data, media_url)
        )
        new_id = cursor.lastrowid
    else:
        new_id = int(id)
        cursor.execute(
            "UPDATE products SET title=?,price_stars=?,price_rub=?,description=?,auto_data=?,media_url=? WHERE id=?",
            (title, price_stars, price_rub, description, auto_data, media_url, new_id)
        )
    conn.commit(); conn.close()
    return {"status": "success", "id": new_id}

@app.post("/api/admin/delete-product")
async def delete_product(data: dict):
    pid = int(data.get("id", 0))
    if not pid:
        return JSONResponse({"error": "Не указан id"}, status_code=400)
    conn, cursor = db_conn()
    cursor.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return {"status": "deleted"}

@app.post("/api/admin/change-balance")
async def api_change_balance(user_id: int = Form(...), amount: int = Form(...)):
    conn, cursor = db_conn()
    cursor.execute("INSERT OR IGNORE INTO users (user_id,username,balance) VALUES (?,'unknown',0)", (user_id,))
    cursor.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    conn.commit(); conn.close()
    try: bot.send_message(user_id, f"💰 Ваш баланс изменён на *{amount:+} руб.*", parse_mode="Markdown")
    except: pass
    return {"status": "success"}

# ── BOT HANDLERS ────────────────────────────────────────────────────

@bot.message_handler(commands=['start','shop'])
def cmd_start(message):
    register_user(message.from_user.id, message.from_user.username)
    bot.reply_to(message, "🌟 Добро пожаловать! Откройте магазин через кнопку меню.")

@bot.message_handler(commands=['profile'])
def cmd_profile(message):
    register_user(message.from_user.id, message.from_user.username)
    conn, cursor = db_conn()
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,))
    res = cursor.fetchone()
    balance = res[0] if res else 0
    conn.close()
    bot.reply_to(message,
        f"👤 *Профиль*\nID: `{message.from_user.id}`\n@{message.from_user.username}\n💰 Баланс: *{balance} руб.*",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['setbalance'])
def cmd_set_balance(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        _, t_id, amt = message.text.split()
        conn, cursor = db_conn()
        cursor.execute("INSERT OR IGNORE INTO users (user_id,username,balance) VALUES (?,'unknown',0)", (int(t_id),))
        cursor.execute("UPDATE users SET balance=? WHERE user_id=?", (int(amt), int(t_id)))
        conn.commit(); conn.close()
        bot.reply_to(message, f"✅ Баланс `{t_id}` = *{amt} руб.*", parse_mode="Markdown")
        bot.send_message(int(t_id), f"💰 Ваш баланс установлен: *{amt} руб.*", parse_mode="Markdown")
    except:
        bot.reply_to(message, "Формат: `/setbalance [user_id] [сумма]`")

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_stars_payment(message):
    pmnt       = message.successful_payment
    product_id = int(pmnt.invoice_payload.split("_")[1])
    charge_id  = pmnt.telegram_payment_charge_id
    date_str   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn, cursor = db_conn()
    cursor.execute("SELECT title,auto_data FROM products WHERE id=?", (product_id,))
    prod = cursor.fetchone()
    cursor.execute(
        "INSERT INTO orders (user_id,username,product_id,status,type,charge_id,amount,date) VALUES (?,?,?,'paid','stars',?,?,?)",
        (message.from_user.id, message.from_user.username, product_id, charge_id, pmnt.total_amount, date_str)
    )
    conn.commit(); conn.close()
    delivery = (prod[1] if prod and prod[1] else "Ожидайте выдачи от администратора.")
    bot.send_message(
        message.chat.id,
        f"🎉 *Оплата ⭐ Stars принята!*\nТовар: *{prod[0] if prod else '—'}*\n\n`{delivery}`",
        parse_mode="Markdown", reply_markup=contact_admin_kb()
    )

@bot.callback_query_handler(func=lambda call: call.data == "contact_admin")
def handle_contact_admin(call):
    u = call.from_user
    register_user(u.id, u.username)
    save_message(u.id, u.username or str(u.id), "🙋 Пользователь хочет связаться с поддержкой", "in")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💬 Написать напрямую", url=f"tg://user?id={u.id}"))
    try:
        bot.send_message(
            ADMIN_ID,
            f"📩 *Запрос на поддержку*\nОт: @{u.username or 'без ника'} (`{u.id}`)\n\nПользователь нажал «Связаться с поддержкой» после покупки.",
            parse_mode="Markdown", reply_markup=kb
        )
    except: pass
    bot.answer_callback_query(call.id, "✅ Запрос отправлен!")
    bot.send_message(
        u.id,
        "📨 *Запрос принят!*\nАдминистратор свяжется с вами. Также можете написать любое сообщение сюда — оно дойдёт до поддержки.",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_") or call.data.startswith("decline_"))
def handle_admin_callbacks(call):
    action, order_id = call.data.split("_", 1)
    order_id = int(order_id)
    conn, cursor = db_conn()
    cursor.execute("SELECT user_id,username,product_id,amount FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order:
        bot.answer_callback_query(call.id, "Заказ не найден"); conn.close(); return
    u_id, u_name, p_id, amt = order
    cursor.execute("SELECT title,auto_data FROM products WHERE id=?", (p_id,))
    prod = cursor.fetchone()

    if action == "confirm":
        cursor.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
        conn.commit(); conn.close()
        bot.edit_message_text(f"✅ Заказ №{order_id} подтверждён.", call.message.chat.id, call.message.message_id)
        delivery = prod[1] if prod and prod[1] else "Ожидайте сообщения от администратора."
        try:
            bot.send_message(u_id,
                f"✅ *Платёж подтверждён!*\nТовар: *{prod[0] if prod else '—'}*\n\n`{delivery}`",
                parse_mode="Markdown", reply_markup=contact_admin_kb()
            )
        except: pass
        bot.answer_callback_query(call.id, "✅ Подтверждено")

    elif action == "decline":
        cursor.execute("UPDATE orders SET status='declined' WHERE id=?", (order_id,))
        conn.commit(); conn.close()
        bot.edit_message_text(f"❌ Заказ №{order_id} отклонён.", call.message.chat.id, call.message.message_id)
        try:
            bot.send_message(u_id,
                f"❌ Платёж за *{prod[0] if prod else 'товар'}* отклонён. Свяжитесь с поддержкой.",
                parse_mode="Markdown", reply_markup=contact_admin_kb()
            )
        except: pass
        bot.answer_callback_query(call.id, "❌ Отклонено")

@bot.message_handler(func=lambda msg: True)
def support_chat(message):
    if message.from_user.id == ADMIN_ID:
        if message.reply_to_message and "User_ID:" in (message.reply_to_message.text or ""):
            try:
                target_id = int(message.reply_to_message.text.split("User_ID:")[1].split("\n")[0].strip())
                save_message(target_id, "admin", message.text, "out")
                bot.send_message(target_id, f"💬 *Сообщение от поддержки:*\n\n{message.text}", parse_mode="Markdown")
                bot.reply_to(message, "✅ Отправлено!")
            except:
                bot.reply_to(message, "Не удалось распарсить User_ID")
    else:
        register_user(message.from_user.id, message.from_user.username)
        save_message(message.from_user.id, message.from_user.username or str(message.from_user.id), message.text, "in")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💬 Ответить напрямую", url=f"tg://user?id={message.from_user.id}"))
        try:
            bot.send_message(
                ADMIN_ID,
                f"✉️ *Тикет*\nОт: @{message.from_user.username}\nUser_ID: {message.from_user.id}\n\n{message.text}\n_(Reply для ответа через бота)_",
                parse_mode="Markdown", reply_markup=kb
            )
        except: pass
        bot.reply_to(message, "📨 Отправлено в поддержку! Ответ придёт сюда.")

threading.Thread(target=bot.infinity_polling, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
