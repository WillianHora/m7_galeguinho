import logging
import paramiko
import sqlite3
import os
import psycopg2
import html
import re
import json
from datetime import datetime, timedelta
from groq import Groq
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# --- CONFIGURAÇÃO (VIA VARIÁVEIS DE AMBIENTE) ---
TOKEN_TELEGRAM = os.getenv('TELEGRAM_TOKEN')
CHAVE_GROQ = os.getenv('GROQ_API_KEY')

ZABBIX_DB = {
    "host": os.getenv('ZABBIX_DB_HOST'),
    "user": os.getenv('ZABBIX_DB_USER'),
    "password": os.getenv('ZABBIX_DB_PASS'),
    "database": os.getenv('ZABBIX_DB_NAME'),
    "port": os.getenv('ZABBIX_DB_PORT', "5432")
}

SSH_HOST = os.getenv('SSH_VOIP_HOST')
SSH_USER = os.getenv('SSH_VOIP_USER')
SSH_PASS = os.getenv('SSH_VOIP_PASS')


BASE_DIR = "/root/m7_bot"
DB_NAME = os.path.join(BASE_DIR, "logs_bot.db")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
client = Groq(api_key=CHAVE_GROQ)

# --- BANCO LOCAL ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS historico (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, usuario TEXT, mensagem TEXT, data TEXT)')
    conn.commit()
    conn.close()

def salvar_mensagem(chat_id, usuario, mensagem):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('INSERT INTO historico (chat_id, usuario, mensagem, data) VALUES (?, ?, ?, ?)', (chat_id, usuario, mensagem, agora))
    cursor.execute('DELETE FROM historico WHERE data < ?', ((datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),))
    conn.commit()
    conn.close()

def buscar_contexto_conversa(chat_id, limite=10):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT usuario, mensagem FROM historico WHERE chat_id = ? ORDER BY data DESC LIMIT ?', (chat_id, limite))
    rows = cursor.fetchall()
    conn.close()
    return "\n".join([f"{r[0]}: {r[1]}" for r in reversed(rows)])

# --- ZABBIX: ALERTAS ---
def buscar_alertas_zabbix():
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        cursor.execute("SET TIME ZONE 'America/Sao_Paulo';")
        query = """
        SELECT h.name, e.name, 
               CASE WHEN e.value = 1 THEN '🔴 PROBLEMA' ELSE '🟢 OK' END as status,
               to_timestamp(e.clock) as data_evento
        FROM events e
        JOIN items i ON i.itemid = (SELECT itemid FROM functions WHERE triggerid = e.objectid LIMIT 1)
        JOIN hosts h ON h.hostid = i.hostid
        WHERE e.source = 0 AND e.object = 0 
        ORDER BY e.clock DESC, e.eventid DESC LIMIT 25;
        """
        cursor.execute(query)
        res = cursor.fetchall()
        conn.close()
        return "\n".join([f"{r[2]} | {r[3].strftime('%H:%M:%S')} | {html.escape(r[0])}: {html.escape(r[1])}" for r in res])
    except: return "Sem dados."

# --- COMANDOS TÉCNICOS ---

async def cmd_speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ <b>Iniciando Speedtest...</b>", parse_mode=ParseMode.HTML)
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    try:
        output = os.popen("speedtest-cli --json").read()
        d = json.loads(output)
        res = (f"🚀 <b>Relatório de Performance</b>\n\n"
               f"📡 <b>ISP:</b> <code>{d['client']['isp']}</code>\n"
               f"⬇️ <b>Download:</b> <b>{d['download']/1e6:.2f} Mbps</b>\n"
               f"⬆️ <b>Upload:</b> <b>{d['upload']/1e6:.2f} Mbps</b>\n"
               f"⏱ <b>Ping:</b> <code>{d['ping']} ms</code>")
    except: res = "❌ Erro ao executar speedtest-cli."
    await update.message.reply_text(res, parse_mode=ParseMode.HTML)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /ping [IP]")
        return
    t = context.args[0]
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    out = os.popen(f"ping -c 4 {t}").read()
    perda = re.search(r"(\d+)% packet loss", out).group(1) if "%" in out else "100"
    lat = re.search(r"avg/max/mdev = [\d\.]+/([\d\.]+)/", out).group(1) if "/" in out else "N/A"
    
    emoji = "🟢" if perda == "0" else "🔴"
    res = (f"📡 <b>Relatório de Ping</b>\n"
           f"Alvo: <code>{t}</code>\n"
           f"Status: {emoji} <b>{perda}% de perda</b>\n"
           f"Latência: <b>{lat} ms</b>")
    await update.message.reply_text(res, parse_mode=ParseMode.HTML)

async def cmd_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /camera [IP]")
        return
    ip = context.args[0]
    video_path = f"/tmp/cam_{ip}.mp4"
    rtsp_url = f"rtsp://admin:152535M7!@{ip}:554/cam/realmonitor?channel=1&subtype=0"
    
    await update.message.reply_text(f"🎥 Gravando 5s da câmera {ip}...")
    await update.message.reply_chat_action(action=ChatAction.UPLOAD_VIDEO)
    
    # Grava 5 segundos em MP4 (compatível com Telegram)
    cmd = ['ffmpeg', '-y', '-rtsp_transport', 'tcp', '-i', rtsp_url, '-t', '5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast', video_path]
    
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if os.path.exists(video_path):
            await update.message.reply_video(video=open(video_path, 'rb'), caption=f"📹 Clip: {ip}")
            os.remove(video_path)
        else: await update.message.reply_text("❌ Falha ao capturar vídeo.")
    except: await update.message.reply_text("❌ Erro de cnexão com a câmera.")

async def cmd_reset_voip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=10)
        ssh.exec_command('reboot')
        ssh.close()
        await update.message.reply_text("✅ Comando de reboot enviado ao Issabel.")
    except Exception as e: await update.message.reply_text(f"❌ Erro: {e}")

# --- IA ---
async def gerenciar_mensagens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or (msg.text and msg.text.startswith('/')): return 
    salvar_mensagem(update.effective_chat.id, msg.from_user.first_name, msg.text)

    if f"@{context.bot.username}" in msg.text or update.effective_chat.type == 'private':
        await msg.reply_chat_action(action=ChatAction.TYPING)
        alertas = buscar_alertas_zabbix()
        conversa = buscar_contexto_conversa(update.effective_chat.id)

        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": (
                        "Você é o Agente M7, NOC sênior. Responda direto e sem enrolação. "
                        "Não confunda alertas de sensor (temperatura/disco) com queda de host (offline). "
                        "Não explique sua lógica de regras. Use HTML <b>."
                        f"\n[LOGS]:\n{alertas}\n\n[CONTEXTO]:\n{conversa}")},
                    {"role": "user", "content": msg.text}
                ]
            )
            resp = completion.choices[0].message.content.replace('**', '')
            await msg.reply_text(resp, parse_mode=ParseMode.HTML)
        except: pass

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    application.add_handler(CommandHandler('resetvoip', cmd_reset_voip))
    application.add_handler(CommandHandler('ping', cmd_ping))
    application.add_handler(CommandHandler('speedtest', cmd_speedtest))
    application.add_handler(CommandHandler('camera', cmd_camera))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), gerenciar_mensagens)) 
    application.run_polling()


