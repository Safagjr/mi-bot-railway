import os
import logging
import asyncio
import re
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

async def delete_all_reminders(chat_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('DELETE FROM reminders WHERE chat_id = %s', (chat_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as e:
        logger.error(f"Error eliminar todos: {e}")
        return 0

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

# ==================== FUNCIÓN PARA PARSEAR TIEMPO ====================
def parsear_tiempo(texto):
    texto = texto.lower().strip()
    # Buscar números seguidos de min/h/d
    match = re.search(r'(\d+)\s*(min|m|h|d)', texto)
    if match:
        cantidad = int(match.group(1))
        unidad = match.group(2)
        if unidad in ['min', 'm']:
            return cantidad
        elif unidad == 'h':
            return cantidad * 60
        elif unidad == 'd':
            return cantidad * 60 * 24
    return 30  # Por defecto 30 minutos

# ==================== COMANDOS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Asistente Personal*\n\n"
        "📌 *Comandos:*\n"
        "• `/recordar 30min Algo`\n"
        "• `/listar`\n"
        "• `/cancelar palabra`\n"
        "• `/borrar_todo`\n"
        "• `/health`\n\n"
        "📝 *También puedes escribir:*\n"
        "• `recordar 5min tomar agua`\n"
        "• `lista`\n"
        "• `hola`\n"
        "• `cancelar todo`\n\n"
        "✨ *Sin necesidad de usar la barra!*",
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
            scheduler.add_job(
                send_reminder,
                DateTrigger(run_date=remind_at),
                args=[update.effective_chat.id, mensaje, remind_at],
                id=f"{update.effective_chat.id}_{remind_at.timestamp()}"
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
    for i, r in enumerate(rows, 1):
        fecha_local = r['remind_at'].astimezone(tz_bog)
        respuesta += f"{i}. {r['message']} - {fecha_local.strftime('%d/%m %H:%M')}\n"
    await update.message.reply_text(respuesta, parse_mode='Markdown')

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("📝 *Uso:* `/cancelar palabra`", parse_mode='Markdown')
        return
    
    palabra = ' '.join(args)
    if palabra.lower() == 'todo':
        deleted = await delete_all_reminders(update.effective_chat.id)
        if deleted > 0:
            await update.message.reply_text(f"✅ *{deleted} recordatorio(s) eliminados*", parse_mode='Markdown')
        else:
            await update.message.reply_text("📭 *No tenías recordatorios*", parse_mode='Markdown')
    else:
        deleted = await delete_reminder_by_keyword(update.effective_chat.id, palabra)
        if deleted > 0:
            await update.message.reply_text(f"✅ *{deleted} recordatorio(s) cancelado(s)*\n📝 Palabra: {palabra}", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ *No encontré recordatorios con '{palabra}'*", parse_mode='Markdown')

async def borrar_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deleted = await delete_all_reminders(update.effective_chat.id)
    if deleted > 0:
        await update.message.reply_text(f"✅ *{deleted} recordatorio(s) eliminados*", parse_mode='Markdown')
    else:
        await update.message.reply_text("📭 *No tenías recordatorios*", parse_mode='Markdown')

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await get_user_reminders(update.effective_chat.id)
    await update.message.reply_text(
        f"🟢 *Bot funcionando*\n📊 *Recordatorios activos:* {len(rows)}",
        parse_mode='Markdown'
    )

# ==================== LENGUAJE NATURAL (SIN GEMINI) ====================
async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.lower().strip()
    chat_id = update.effective_chat.id
    tz_bog = pytz.timezone("America/Bogota")
    
    # Saludos
    if texto in ['hola', 'buenas', 'que tal', 'hey', 'holi', 'buenos dias', 'buenas tardes']:
        await update.message.reply_text("🌟 *¡Hola!* ¿En qué puedo ayudarte?\n\nUsa `/start` para ver los comandos.", parse_mode='Markdown')
        return
    
    if texto in ['gracias', 'muchas gracias', 'gracias bot', 'thanks']:
        await update.message.reply_text("🤖 *¡De nada!* Estoy aquí para ayudarte.", parse_mode='Markdown')
        return
    
    # Comandos en lenguaje natural
    if texto == 'lista' or texto == 'listar' or texto == 'que tengo pendiente' or texto == 'mis recordatorios':
        rows = await get_user_reminders(chat_id)
        if not rows:
            await update.message.reply_text("📭 *No tienes recordatorios pendientes.*", parse_mode='Markdown')
        else:
            respuesta = "📋 *Tus recordatorios:*\n\n"
            for i, r in enumerate(rows, 1):
                fecha_local = r['remind_at'].astimezone(tz_bog)
                respuesta += f"{i}. {r['message']} - {fecha_local.strftime('%d/%m %H:%M')}\n"
            await update.message.reply_text(respuesta, parse_mode='Markdown')
        return
    
    if texto == 'cancelar todo' or texto == 'borrar todo' or texto == 'eliminar todo':
        deleted = await delete_all_reminders(chat_id)
        if deleted > 0:
            await update.message.reply_text(f"✅ *{deleted} recordatorio(s) eliminados*", parse_mode='Markdown')
        else:
            await update.message.reply_text("📭 *No tenías recordatorios*", parse_mode='Markdown')
        return
    
    # Crear recordatorio con lenguaje natural: "recordar 5min tomar agua"
    if texto.startswith('recordar'):
        partes = texto.split()
        if len(partes) >= 2:
            tiempo_str = partes[1]
            mensaje = ' '.join(partes[2:]) if len(partes) > 2 else "Recordatorio"
            
            minutos = parsear_tiempo(tiempo_str)
            remind_at = datetime.now(pytz.utc) + timedelta(minutes=minutos)
            
            if await add_reminder(chat_id, mensaje, remind_at):
                scheduler.add_job(
                    send_reminder,
                    DateTrigger(run_date=remind_at),
                    args=[chat_id, mensaje, remind_at],
                    id=f"{chat_id}_{remind_at.timestamp()}"
                )
                hora_local = remind_at.astimezone(tz_bog)
                await update.message.reply_text(
                    f"✅ *Recordatorio guardado!*\n📝 {mensaje}\n⏰ {hora_local.strftime('%d/%m %H:%M')}",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Error al guardar", parse_mode='Markdown')
        else:
            await update.message.reply_text("📝 *Uso:* `recordar 30min Tu mensaje`", parse_mode='Markdown')
        return
    
    # Si no reconoce el comando, mostrar ayuda
    await update.message.reply_text(
        "📌 *No entendí.*\n\n"
        "Puedes usar:\n"
        "• `recordar 30min Tu mensaje`\n"
        "• `lista`\n"
        "• `hola`\n"
        "• `cancelar palabra`\n\n"
        "Usa `/start` para ver todos los comandos.",
        parse_mode='Markdown'
    )

async def send_reminder(chat_id, message, remind_at):
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=f"⏰ *RECORDATORIO:*\n{message}", parse_mode='Markdown')
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
    bot_app.add_handler(CommandHandler("borrar_todo", borrar_todo))
    bot_app.add_handler(CommandHandler("health", health))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    scheduler = AsyncIOScheduler()
    scheduler.start()
    
    loop.create_task(start_health_server())
    
    logger.info("🚀 Bot iniciado correctamente")
    bot_app.run_polling()