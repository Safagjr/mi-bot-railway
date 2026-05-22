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

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables globales
bot_app = None
scheduler = None

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

# ==================== GEMINI ====================
async def entender_con_gemini(texto_usuario, fecha_actual):
    """Versión simplificada y probada de Gemini para lenguaje natural"""
    
    if not GEMINI_API_KEY:
        return {"accion": "conversar", "respuesta": "Usa /recordar 30min Tu mensaje"}
    
    prompt = f"""Responde SOLO con un JSON. Sin texto adicional.
Fecha actual en Colombia: {fecha_actual}
Usuario: "{texto_usuario}"

Reglas:
- Si quiere CREAR un recordatorio: {{"accion": "crear", "mensaje": "texto del recordatorio", "tiempo": "30min"}}
- Si quiere VER sus recordatorios: {{"accion": "listar"}}
- Si quiere ELIMINAR un recordatorio: {{"accion": "eliminar", "palabra": "palabra clave"}}
- Si es SALUDO o CONVERSACIÓN normal: {{"accion": "conversar", "respuesta": "tu respuesta amigable"}}

JSON:"""

    try:
        response = requests.post(
            GEMINI_URL,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=10
        )
        
        if response.status_code != 200:
            logger.error(f"Gemini error HTTP: {response.status_code}")
            return {"accion": "conversar", "respuesta": "Usa /recordar 30min Tu mensaje"}
        
        data = response.json()
        if 'candidates' not in data:
            logger.error(f"Gemini respuesta sin candidates: {data}")
            return {"accion": "conversar", "respuesta": "Usa /recordar 30min Tu mensaje"}
        
        texto = data['candidates'][0]['content']['parts'][0]['text']
        texto = texto.replace('```json', '').replace('```', '').strip()
        
        resultado = json.loads(texto)
        logger.info(f"Gemini respondió: {resultado}")
        return resultado
        
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return {"accion": "conversar", "respuesta": "Usa /recordar 30min Tu mensaje"}

def parsear_tiempo(texto):
    """Convierte texto como '30min', '2h', '1d' a minutos"""
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
    if not url:
        raise ValueError("DATABASE_URL no configurada")
    if 'channel_binding' in url:
        url = url.split('&channel_binding')[0]
    if 'sslmode=require' not in url:
        url += '?sslmode=require'
    return psycopg2.connect(url)

async def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                remind_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_remind_at ON reminders(remind_at)')
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ Base de datos lista")
    except Exception as e:
        logger.error(f"Error al inicializar DB: {e}")

async def add_reminder(chat_id, message, remind_at):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO reminders (chat_id, message, remind_at) VALUES (%s, %s, %s)',
            (chat_id, message, remind_at)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error al guardar: {e}")
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
        logger.error(f"Error al obtener: {e}")
        return []

async def delete_reminder(reminder_id, chat_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM reminders WHERE id = %s AND chat_id = %s', (reminder_id, chat_id))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted > 0
    except Exception as e:
        logger.error(f"Error al eliminar: {e}")
        return False

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
        logger.error(f"Error al eliminar: {e}")
        return 0

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *¡Bienvenido a tu Asistente Personal con IA!* 🌟\n\n"
        "🤖 *Puedes hablarme en lenguaje natural:*\n"
        "• 'Recuérdame en 5min tomar agua'\n"
        "• 'Qué tengo pendiente'\n"
        "• 'Cancela el recordatorio de la reunión'\n"
        "• 'Hola'\n\n"
        "📌 *O usa comandos clásicos:*\n"
        "• `/recordar 30min Tu mensaje`\n"
        "• `/listar`\n"
        "• `/cancelar palabra`\n"
        "• `/health`\n\n"
        "✨ *¡Pruébame!* Escríbeme como le hablarías a una persona.",
        parse_mode='Markdown'
    )

async def recordar_comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ' '.join(context.args)
    if not texto:
        await update.message.reply_text("📝 *Uso:* `/recordar 30min Tu mensaje`", parse_mode='Markdown')
        return
    
    try:
        partes = texto.split()
        tiempo_str = partes[0]
        mensaje = ' '.join(partes[1:]) if len(partes) > 1 else "Recordatorio"
        
        minutos = parsear_tiempo(tiempo_str)
        remind_at = datetime.now(pytz.utc) + timedelta(minutes=minutos)
        
        if await add_reminder(update.effective_chat.id, mensaje, remind_at):
            if scheduler:
                scheduler.add_job(
                    send_reminder,
                    DateTrigger(run_date=remind_at),
                    args=[update.effective_chat.id, mensaje, remind_at]
                )
            tz_bog = pytz.timezone("America/Bogota")
            hora_local = remind_at.astimezone(tz_bog)
            await update.message.reply_text(
                f"✅ *Recordatorio guardado!*\n📝 {mensaje}\n⏰ {hora_local.strftime('%d/%m %H:%M')}",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Error al guardar")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Error. Usa: `/recordar 30min Tu mensaje`", parse_mode='Markdown')

async def listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_user_reminders(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("📭 *No tienes recordatorios pendientes.*", parse_mode='Markdown')
        return
    
    tz_bog = pytz.timezone("America/Bogota")
    respuesta = "📋 *Tus recordatorios:*\n\n"
    for r in rows:
        fecha_local = r['remind_at'].astimezone(tz_bog)
        respuesta += f"• {r['message']} - {fecha_local.strftime('%d/%m %H:%M')}\n"
    await update.message.reply_text(respuesta, parse_mode='Markdown')

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("📝 *Uso:* `/cancelar palabra` o `/cancelar 5`", parse_mode='Markdown')
        return
    
    parametro = ' '.join(args)
    if parametro.isdigit():
        if await delete_reminder(int(parametro), update.effective_chat.id):
            await update.message.reply_text(f"✅ Recordatorio #{parametro} cancelado", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ No se encontró el recordatorio #{parametro}", parse_mode='Markdown')
    else:
        deleted = await delete_reminder_by_keyword(update.effective_chat.id, parametro)
        if deleted > 0:
            await update.message.reply_text(f"✅ {deleted} recordatorio(s) cancelado(s)", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ No se encontraron recordatorios con '{parametro}'", parse_mode='Markdown')

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🟢 *Bot funcionando con IA*", parse_mode='Markdown')

# ==================== LENGUAJE NATURAL ====================
async def manejar_lenguaje_natural(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa mensajes en lenguaje natural usando Gemini"""
    texto = update.message.text
    chat_id = update.effective_chat.id
    tz_bog = pytz.timezone("America/Bogota")
    fecha_actual = datetime.now(tz_bog).strftime('%Y-%m-%d %H:%M')
    
    await update.message.chat.send_action("typing")
    
    resultado = await entender_con_gemini(texto, fecha_actual)
    accion = resultado.get("accion")
    
    if accion == "crear":
        mensaje = resultado.get("mensaje", "Recordatorio")
        tiempo_str = resultado.get("tiempo", "30min")
        minutos = parsear_tiempo(tiempo_str)
        remind_at = datetime.now(pytz.utc) + timedelta(minutes=minutos)
        
        if await add_reminder(chat_id, mensaje, remind_at):
            if scheduler:
                scheduler.add_job(send_reminder, DateTrigger(run_date=remind_at), args=[chat_id, mensaje, remind_at])
            hora_local = remind_at.astimezone(tz_bog)
            await update.message.reply_text(f"✅ *¡Listo!* Te recordaré '{mensaje}' a las {hora_local.strftime('%H:%M')}", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Error al guardar el recordatorio")
            
    elif accion == "listar":
        rows = await get_user_reminders(chat_id)
        if not rows:
            await update.message.reply_text("📭 No tienes recordatorios pendientes.")
        else:
            respuesta = "📋 *Tus recordatorios:*\n\n"
            for r in rows:
                fecha_local = r['remind_at'].astimezone(tz_bog)
                respuesta += f"• {r['message']} - {fecha_local.strftime('%d/%m %H:%M')}\n"
            await update.message.reply_text(respuesta, parse_mode='Markdown')
            
    elif accion == "eliminar":
        palabra = resultado.get("palabra", "")
        if not palabra:
            await update.message.reply_text("¿Qué recordatorio quieres cancelar?")
            return
        deleted = await delete_reminder_by_keyword(chat_id, palabra)
        if deleted > 0:
            await update.message.reply_text(f"✅ {deleted} recordatorio(s) cancelado(s)")
        else:
            await update.message.reply_text(f"❌ No encontré recordatorios con '{palabra}'")
            
    elif accion == "conversar":
        respuesta = resultado.get("respuesta", "¿En qué puedo ayudarte?")
        await update.message.reply_text(respuesta)
    else:
        await update.message.reply_text("🤔 No entendí. Puedes usar `/recordar 30min Tu mensaje`")

# ==================== ENVÍO DE RECORDATORIOS ====================
async def send_reminder(chat_id, message, remind_at):
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=f"⏰ *RECORDATORIO:*\n{message}", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error enviando recordatorio: {e}")

# ==================== HEALTH CHECK SERVER ====================
async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("🟢 Health check server started")

# ==================== PUNTO DE ENTRADA ====================
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
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_lenguaje_natural))
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.start()
    
    loop.create_task(start_health_server())
    
    logger.info("🚀 Bot con Gemini iniciado!")
    bot_app.run_polling()