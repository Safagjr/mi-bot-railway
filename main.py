import os
import logging
import asyncio
import json
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import psycopg2
import psycopg2.extras
import pytz
from aiohttp import web

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

bot_app = None
scheduler = None

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
logger.info(f"API Key presente: {GEMINI_API_KEY is not None}")

# ==================== GEMINI ====================
async def entender_con_gemini(texto_usuario):
    """Función simplificada para probar Gemini"""
    if not GEMINI_API_KEY:
        return {"accion": "conversar", "respuesta": "API Key no configurada. Usa /recordar"}
    
    prompt = f"Responde SOLO con un JSON. Usuario: '{texto_usuario}'. Si es saludo responde {{'accion':'conversar','respuesta':'Hola! ¿Cómo estás?'}}. Si pide recordatorio responde {{'accion':'crear','mensaje':'texto','tiempo':'5min'}}"
    
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
        response = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            texto = data['candidates'][0]['content']['parts'][0]['text']
            texto = texto.replace('```json', '').replace('```', '').strip()
            return json.loads(texto)
        else:
            logger.error(f"Error Gemini: {response.status_code}")
            return {"accion": "conversar", "respuesta": f"Usa /recordar 30min Tu mensaje"}
    except Exception as e:
        logger.error(f"Error: {e}")
        return {"accion": "conversar", "respuesta": "Usa /recordar 30min Tu mensaje"}

def parsear_tiempo(texto):
    texto = texto.lower()
    if 'min' in texto:
        return int(''.join(filter(str.isdigit, texto)))
    elif 'h' in texto:
        return int(''.join(filter(str.isdigit, texto))) * 60
    elif 'd' in texto:
        return int(''.join(filter(str.isdigit, texto))) * 60 * 24
    else:
        return 30

# ==================== BASE DE DATOS ====================
def get_db_connection():
    url = os.environ.get('DATABASE_URL')
    if 'channel_binding' in url:
        url = url.split('&channel_binding')[0]
    return psycopg2.connect(url, sslmode='require')

async def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                remind_at TIMESTAMPTZ NOT NULL
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ Base de datos lista")
    except Exception as e:
        logger.error(f"Error DB: {e}")

async def add_reminder(chat_id, message, remind_at):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO reminders (chat_id, message, remind_at) VALUES (%s, %s, %s)', (chat_id, message, remind_at))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error guardar: {e}")
        return False

async def get_user_reminders(chat_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT id, message, remind_at FROM reminders WHERE chat_id = %s ORDER BY remind_at', (chat_id,))
        records = cur.fetchall()
        cur.close()
        conn.close()
        return records
    except Exception as e:
        logger.error(f"Error obtener: {e}")
        return []

async def delete_reminder_by_keyword(chat_id, keyword):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM reminders WHERE chat_id = %s AND message ILIKE %s', (chat_id, f'%{keyword}%'))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as e:
        logger.error(f"Error eliminar: {e}")
        return 0

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Asistente Personal*\n\n"
        "📌 *Comandos:*\n"
        "• `/recordar 30min Algo`\n"
        "• `/listar`\n"
        "• `/cancelar palabra`\n"
        "• `/health`\n\n"
        "🤖 *Lenguaje natural:*\n"
        "• 'Recuérdame en 5min tomar agua'\n"
        "• 'Hola'",
        parse_mode='Markdown'
    )

async def recordar_comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ' '.join(context.args)
    if not texto:
        await update.message.reply_text("Uso: /recordar 30min Tu mensaje")
        return
    try:
        partes = texto.split()
        tiempo_str = partes[0]
        mensaje = ' '.join(partes[1:]) if len(partes) > 1 else "Recordatorio"
        minutos = parsear_tiempo(tiempo_str)
        remind_at = datetime.now(pytz.utc) + timedelta(minutes=minutos)
        if await add_reminder(update.effective_chat.id, mensaje, remind_at):
            scheduler.add_job(send_reminder, DateTrigger(run_date=remind_at), args=[update.effective_chat.id, mensaje, remind_at])
            tz_bog = pytz.timezone("America/Bogota")
            await update.message.reply_text(f"✅ Recordatorio: {mensaje} a las {remind_at.astimezone(tz_bog).strftime('%H:%M')}")
        else:
            await update.message.reply_text("❌ Error")
    except Exception as e:
        await update.message.reply_text("Error. Usa: /recordar 30min Mensaje")

async def listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_user_reminders(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No hay recordatorios.")
        return
    tz_bog = pytz.timezone("America/Bogota")
    respuesta = "📋 Tus recordatorios:\n"
    for r in rows:
        respuesta += f"• {r['message']} - {r['remind_at'].astimezone(tz_bog).strftime('%d/%m %H:%M')}\n"
    await update.message.reply_text(respuesta)

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /cancelar palabra")
        return
    palabra = ' '.join(args)
    deleted = await delete_reminder_by_keyword(update.effective_chat.id, palabra)
    if deleted > 0:
        await update.message.reply_text(f"✅ {deleted} recordatorio(s) cancelado(s)")
    else:
        await update.message.reply_text(f"❌ No encontré '{palabra}'")

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 Bot funcionando")

# ==================== LENGUAJE NATURAL ====================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    chat_id = update.effective_chat.id
    tz_bog = pytz.timezone("America/Bogota")
    
    await update.message.chat.send_action("typing")
    resultado = await entender_con_gemini(texto)
    
    accion = resultado.get("accion")
    if accion == "crear":
        mensaje = resultado.get("mensaje", "Recordatorio")
        minutos = parsear_tiempo(resultado.get("tiempo", "30min"))
        remind_at = datetime.now(pytz.utc) + timedelta(minutes=minutos)
        if await add_reminder(chat_id, mensaje, remind_at):
            scheduler.add_job(send_reminder, DateTrigger(run_date=remind_at), args=[chat_id, mensaje, remind_at])
            await update.message.reply_text(f"✅ Recordatorio: {mensaje} en {minutos} minutos")
        else:
            await update.message.reply_text("❌ Error")
    elif accion == "conversar":
        await update.message.reply_text(resultado.get("respuesta", "Hola! ¿En qué puedo ayudarte?"))
    else:
        await update.message.reply_text("Usa /recordar 30min Tu mensaje")

async def send_reminder(chat_id, message, remind_at):
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=f"⏰ {message}")
    except Exception as e:
        logger.error(f"Error enviar: {e}")

# ==================== HEALTH CHECK ====================
async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("🟢 Health check iniciado")

# ==================== MAIN ====================
if __name__ == '__main__':
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("ERROR: No hay token")
        exit(1)
    
    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("recordar", recordar_comando))
    bot_app.add_handler(CommandHandler("listar", listar))
    bot_app.add_handler(CommandHandler("cancelar", cancelar))
    bot_app.add_handler(CommandHandler("health", health))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.start()
    
    loop.create_task(start_health_server())
    
    logger.info("🚀 Bot iniciado")
    bot_app.run_polling()