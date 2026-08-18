import os
import threading
from flask import Flask
from telethon import TelegramClient, events, Button
from telethon.errors import (
    PhoneCodeInvalidError, 
    PhoneCodeExpiredError, 
    SessionPasswordNeededError, 
    PasswordHashInvalidError
)

# =============================================================
# 1. إعداد خادم سحبي خفيف (Dummy Web Server for Render Port)
# =============================================================
app = Flask(__name__)

@app.route('/')
def home():
    # صفحة استجابة وهمية لإرضاء فحص الصحة (Health Check) في Render
    return "<h1>Bot Status: Running 24/7 🚀</h1>", 200

def run_flask():
    # الحصول على البورت المخصص من Render تلقائياً أو استخدام 10000 افتراضياً
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# =============================================================
# 2. إعدادات المتغيرات وقراءة البيانات الأمنية
# =============================================================
API_ID = int(os.environ.get("API_ID", "12345678"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

# تشغيل البوت الرئيسي
bot = TelegramClient('main_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# مخزن ذاكرة مؤقت لإدارة حالات المستخدمين
user_states = {}

# =============================================================
# 3. بناء كيبورد الأرقام الشفاف (Inline Keyboard)
# =============================================================
def make_numeric_keyboard(current_code=""):
    display = f"الكود الحالي: {current_code}" if current_code else "لم يتم إدخال أرقام بعد"
    buttons = [
        [Button.inline("1", b"k_1"), Button.inline("2", b"k_2"), Button.inline("3", b"k_3")],
        [Button.inline("4", b"k_4"), Button.inline("5", b"k_5"), Button.inline("6", b"k_6")],
        [Button.inline("7", b"k_7"), Button.inline("8", b"k_8"), Button.inline("9", b"k_9")],
        [Button.inline("❌ حذف", b"k_del"), Button.inline("0", b"k_0"), Button.inline("✅ تأكيد", b"k_confirm")]
    ]
    return display, buttons

# =============================================================
# 4. استقبال /start وإرسال زر مشاركة الرقم الرسمي
# =============================================================
@bot.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    user_id = event.sender_id
    user_states[user_id] = {"step": "WAITING_PHONE", "code": ""}
    
    phone_btn = [Button.request_phone("📱 مشاركة رقم الهاتف", resize=True, single_use=True)]
    await event.respond(
        "أهلاً بك! اضغط على الزر أدناه لمشاركة رقم هاتفك وبدء إنتاج ملف الجلسة:", 
        buttons=phone_btn
    )

# =============================================================
# 5. استقبال الرقم واستدعاء طلب الكود من تليجرام
# =============================================================
@bot.on(events.NewMessage)
async def process_phone(event):
    user_id = event.sender_id
    
    if user_id in user_states and user_states[user_id]["step"] == "WAITING_PHONE":
        if event.message.contact:
            phone_num = event.message.contact.phone_number
            if not phone_num.startswith('+'):
                phone_num = '+' + phone_num
            
            loading_msg = await event.respond("⏳ جاري الطلب من تليجرام إرسال كود التحقق...", buttons=Button.clear())
            
            sess_filename = f"user_sess_{user_id}"
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
                    f"✅ تم إرسال كود التحقق إلى حسابك على تليجرام.\n\nاستخدم اللوحة الشفافة أدناه لإدخال الكود:\n\n{disp_text}", 
                    buttons=btns
                )
                
            except Exception as error:
                await temp_client.disconnect()
                if os.path.exists(f"{sess_filename}.session"):
                    os.remove(f"{sess_filename}.session")
                await event.respond(f"❌ حدث خطأ أثناء طلب الكود: {str(error)}")

# =============================================================
# 6. التعامل مع الضغط على لوحة الأرقام الشفافة
# =============================================================
@bot.on(events.CallbackQuery(pattern=b'k_'))
async def process_keyboard(event):
    user_id = event.sender_id
    
    if user_id not in user_states or user_states[user_id]["step"] != "WAITING_CODE":
        await event.answer("⚠️ انتهاء صلاحية هذه الجلسة، أرسل /start من جديد.", alert=True)
        return
        
    action = event.data.decode('utf-8').replace('k_', '')
    state = user_states[user_id]
    
    if action.isdigit():
        if len(state["code"]) < 6:
            state["code"] += action
    elif action == "del":
        state["code"] = state["code"][:-1]
    elif action == "confirm":
        await verify_code_and_login(event, user_id)
        return

    disp_text, btns = make_numeric_keyboard(state["code"])
    await event.edit(
        f"✅ تم إرسال كود التحقق إلى حسابك على تليجرام.\n\nاستخدم اللوحة الشفافة أدناه لإدخال الكود:\n\n{disp_text}", 
        buttons=btns
    )
    await event.answer()

# =============================================================
# 7. معالجة الكود + كشف التحقق بخطوتين (2FA)
# =============================================================
async def verify_code_and_login(event, user_id):
    state = user_states[user_id]
    code = state["code"]
    client = state["client"]
    phone = state["phone"]
    code_hash = state["phone_code_hash"]
    
    if not code:
        await event.answer("⚠️ الرجاء إدخال الكود أولاً!", alert=True)
        return

    await event.edit("⏳ جاري التحقق من صحة الكود...")

    try:
        await client.sign_in(phone, code, phone_code_hash=code_hash)
        await deliver_session_file(user_id)

    except SessionPasswordNeededError:
        state["step"] = "WAITING_2FA"
        await event.edit("🔐 **حسابك محمي بتحقق بخطوتين (2FA).**\n\nيرجى كتابة كلمة السر الخاصة بك وإرسالها كرسالة نصية هنا:")
        
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        state["code"] = ""
        disp_text, btns = make_numeric_keyboard("")
        await event.edit(f"❌ الكود غير صحيح أو انتهت صلاحيته. حاول مجدداً:\n\n{disp_text}", buttons=btns)

# =============================================================
# 8. استقبال كلمة سر التحقق بخطوتين (2FA)
# =============================================================
@bot.on(events.NewMessage)
async def process_2fa_password(event):
    user_id = event.sender_id
    
    if user_id in user_states and user_states[user_id]["step"] == "WAITING_2FA":
        password = event.text
        state = user_states[user_id]
        client = state["client"]
        
        try:
            await client.sign_in(password=password)
            await deliver_session_file(user_id)
        except PasswordHashInvalidError:
            await event.respond("❌ كلمة السر غير صحيحة، أعد كتابتها مجدداً:")

# =============================================================
# 9. حفظ وتسليم ملف الجلسة فوراً وتنظيف السيرفر
# =============================================================
async def deliver_session_file(user_id):
    state = user_states[user_id]
    client = state["client"]
    sess_filename = state["sess_filename"]
    file_path = f"{sess_filename}.session"

    await client.disconnect()

    if os.path.exists(file_path):
        await bot.send_file(
            user_id,
            file_path,
            caption="🎉 **تم تسليتك الجلسة بنجاح!**\n\nإليك ملف الجلسة (.session) الخاص بك جاهز للاستخدام."
        )
        os.remove(file_path)
    
    del user_states[user_id]

# =============================================================
# 10. التشغيل المزدوج (Flask Server + Telethon Client)
# =============================================================
if __name__ == '__main__':
    # تشغيل سيرفر خفيف في الخلفية لربط البورت من Render
    web_thread = threading.Thread(target=run_flask)
    web_thread.daemon = True
    web_thread.start()
    
    print("🚀 خادم الويب يعمل وبوت تليجرام يتلقى الطلبات...")
    bot.run_until_disconnected()

