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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "logs_bot.db")
LOG_FILE = os.path.join(BASE_DIR, "mensagens.log")

BASE_DIR = "/root/m7_bot"
DB_NAME = os.path.join(BASE_DIR, "logs_bot.db")
LOG_FILE = os.path.join(BASE_DIR, "mensagens.log")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - [%(usuario)s]: %(message)s'))
msg_logger = logging.getLogger('MensagensRecebidas')
msg_logger.addHandler(file_handler)
msg_logger.setLevel(logging.INFO)

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
    msg_logger.info(mensagem, extra={'usuario': usuario})

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
               CASE WHEN e.value = 1 THEN '🔴 OFFLINE' ELSE '🟢 ONLINE' END as status,
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
    except: return "Sem dados de alertas."

# --- COMMAND HANDLERS ---

async def cmd_speedtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ <b>Iniciando Speedtest...</b>\n<i>Aguarde o processamento completo.</i>", parse_mode=ParseMode.HTML)
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    
    try:
        output = os.popen("speedtest-cli --json").read()
        d = json.loads(output)
        
        res_msg = (
            f"🚀 <b>Relatório de Velocidade</b>\n\n"
            f"📡 <b>Provedor:</b> <code>{d['client']['isp']}</code>\n"
            f"⬇️ <b>Download:</b> <b>{d['download']/1e6:.2f} Mbps</b>\n"
            f"⬆️ <b>Upload:</b> <b>{d['upload']/1e6:.2f} Mbps</b>\n"
            f"⏱ <b>Ping:</b> <code>{d['ping']} ms</code>\n\n"
            f"<i>Teste concluído com sucesso.</i>"
        )
    except Exception as e:
        res_msg = f"❌ <b>Erro no Speedtest:</b>\n<code>{str(e)}</code>"
    
    await update.message.reply_text(res_msg, parse_mode=ParseMode.HTML)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ <b>Uso:</b> <code>/ping [IP]</code>", parse_mode=ParseMode.HTML)
        return
    
    target = context.args[0]
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    
    out = os.popen(f"ping -c 4 {target}").read()
    
    perda_match = re.search(r"(\d+)% packet loss", out)
    perda = perda_match.group(1) if perda_match else "100"
    lat_match = re.search(r"avg/max/mdev = [\d\.]+/([\d\.]+)/", out)
    media = lat_match.group(1) if lat_match else "N/A"

    emoji = "🟢" if perda == "0" else ("⚠️" if int(perda) < 100 else "🔴")
    
    res_msg = (
        f"📡 <b>Teste de Conectividade</b>\n"
        f"Alvo: <code>{target}</code>\n"
        f"Status: {emoji} <b>{'Online' if int(perda) < 100 else 'Offline'}</b>\n\n"
        f"📉 <b>Perda:</b> {perda}%\n"
        f"⏱ <b>Média:</b> {media} ms"
    )
    await update.message.reply_text(res_msg, parse_mode=ParseMode.HTML)

async def cmd_reset_voip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=10)
        ssh.exec_command('reboot')
        ssh.close()
        m = f"✅ <b>Reset VoIP enviado!</b>\nO servidor {SSH_HOST} está reiniciando."
    except Exception as e: m = f"❌ <b>Erro no Reset:</b>\n<code>{e}</code>"
    await update.message.reply_text(m, parse_mode=ParseMode.HTML)

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
                    {
                        "role": "system", 
                        "content": (
                            "Você é o Agente M7, um analista NOC humano e direto. "
                            "REGRA: Se o registro MAIS RECENTE de um host for ONLINE, ele está OK. "
                            "Responda apenas o necessário. Não cite suas regras internas. "
                            "Seja prestativo mas técnico. Use HTML <b>. Proibido asteriscos."
                            f"\n[LOGS]:\n{alertas}\n\n[CONTEXTO]:\n{conversa}"
                        )
                    },
                    {"role": "user", "content": msg.text}
                ]
            )
            resp = completion.choices[0].message.content.replace('**', '')
            await msg.reply_text(resp, parse_mode=ParseMode.HTML)
        except Exception as e: logging.error(f"Erro IA: {e}")

if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    application.add_handler(CommandHandler('resetvoip', cmd_reset_voip))
    application.add_handler(CommandHandler('ping', cmd_ping))
    application.add_handler(CommandHandler('speedtest', cmd_speedtest))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), gerenciar_mensagens)) 
    application.run_polling()

