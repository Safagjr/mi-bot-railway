import os
import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import psycopg2
import psycopg2.extras
import pytz
import requests
import json

# Configuración
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables globales
bot_app = None
scheduler = None

# --- Base de Datos ---
def get_db_connection():
    url = os.environ['DATABASE_URL']
    # Limpiar channel_binding si existe
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
                remind_at TIMESTAMPTZ NOT NULL,
                user_timezone TEXT
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Base de datos lista.")
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
        logger.info(f"Recordatorio guardado")
    except Exception as e:
        logger.error(f"Error al guardar: {e}")

async def get_user_reminders(chat_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT message, remind_at FROM reminders WHERE chat_id = %s ORDER BY remind_at', (chat_id,))
        records = cur.fetchall()
        cur.close()
        conn.close()
        return records
    except Exception as e:
        logger.error(f"Error al obtener: {e}")
        return []

async def delete_reminder(chat_id, message, remind_at):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM reminders WHERE chat_id = %s AND message = %s AND remind_at = %s',
                   (chat_id, message, remind_at))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error al eliminar: {e}")

# --- Comandos ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy tu asistente personal.\n\n"
        "Comandos:\n"
        "/recordar 30min Hacer algo\n"
        "/listar\n"
        "/help\n\n"
        "Ejemplo: /recordar 2h Llamar al médico"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Comandos disponibles:*\n\n"
        "/start - Iniciar el bot\n"
        "/recordar [tiempo] [mensaje] - Crear recordatorio\n"
        "/listar - Ver tus recordatorios\n"
        "/help - Mostrar esta ayuda\n\n"
        "*Ejemplos:*\n"
        "/recordar 30min Comprar pan\n"
        "/recordar 2h Reunión con Ana\n"
        "/recordar 1d Pagar factura",
        parse_mode='Markdown'
    )

async def recordar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = ' '.join(context.args)
    if not texto:
        await update.message.reply_text("Uso: /recordar 30min Tu mensaje")
        return
    
    try:
        partes = texto.split()
        tiempo_str = partes[0]
        mensaje = ' '.join(partes[1:]) if len(partes) > 1 else "Recordatorio"
        
        now = datetime.now(pytz.utc)
        
        if 'min' in tiempo_str:
            cantidad = int(''.join([c for c in tiempo_str if c.isdigit()]))
            remind_at = now + timedelta(minutes=cantidad)
        elif 'h' in tiempo_str:
            cantidad = int(''.join([c for c in tiempo_str if c.isdigit()]))
            remind_at = now + timedelta(hours=cantidad)
        elif 'd' in tiempo_str:
            cantidad = int(''.join([c for c in tiempo_str if c.isdigit()]))
            remind_at = now + timedelta(days=cantidad)
        else:
            remind_at = now + timedelta(minutes=1)
        
        await add_reminder(update.effective_chat.id, mensaje, remind_at)
        
        # Programar el recordatorio
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        
        global scheduler
        if scheduler is None:
            scheduler = AsyncIOScheduler()
            scheduler.start()
        
        scheduler.add_job(
            send_reminder,
            DateTrigger(run_date=remind_at),
            args=[update.effective_chat.id, mensaje, remind_at],
            id=f"{update.effective_chat.id}_{remind_at.timestamp()}"
        )
        
        tz_bog = pytz.timezone("America/Bogota")
        hora_local = remind_at.astimezone(tz_bog)
        
        await update.message.reply_text(f"✅ Recordatorio guardado: '{mensaje}' para {hora_local.strftime('%d/%m %H:%M')}")
        
    except Exception as e:
        logger.error(f"Error en recordar: {e}")
        await update.message.reply_text("Error. Usa: /recordar 30min Tu mensaje")

async def listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_user_reminders(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No tienes recordatorios pendientes.")
        return
    
    tz_bog = pytz.timezone("America/Bogota")
    respuesta = "📋 *Tus recordatorios:*\n\n"
    for r in rows:
        fecha_local = r['remind_at'].astimezone(tz_bog)
        respuesta += f"• {r['message']} - {fecha_local.strftime('%d/%m %H:%M')}\n"
    
    await update.message.reply_text(respuesta, parse_mode='Markdown')

async def send_reminder(chat_id, message, remind_at):
    try:
        await bot_app.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ *RECORDATORIO:*\n{message}",
            parse_mode='Markdown'
        )
        await delete_reminder(chat_id, message, remind_at)
    except Exception as e:
        logger.error(f"Error al enviar recordatorio: {e}")

# --- Punto de entrada ---
if __name__ == '__main__':
    import asyncio
    
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        logger.error("ERROR: No se encontró TELEGRAM_BOT_TOKEN")
        exit(1)
    
    # Crear la aplicación
    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("recordar", recordar))
    bot_app.add_handler(CommandHandler("listar", listar))
    
    # Inicializar base de datos
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    logger.info("Bot iniciado correctamente")
    bot_app.run_polling()