import logging
import paramiko
import sqlite3
import os
import psycopg2
import html
import re
import json
import subprocess
import requests
from requests.auth import HTTPDigestAuth
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters, CallbackQueryHandler, ConversationHandler
from datetime import datetime, timedelta
from groq import Groq
from telegram.constants import ChatAction, ParseMode

# --- CONFIGURAÇÃO (RECOMENDADO: USAR VARIÁVEIS DE AMBIENTE OU ARQUIVO .ENV) ---
TOKEN_TELEGRAM = os.getenv('TOKEN_TELEGRAM', 'SEU_TOKEN_AQUI')
CHAVE_GROQ = os.getenv('CHAVE_GROQ', 'SUA_CHAVE_GROQ_AQUI')

ZABBIX_DB = {
    "host": os.getenv('ZABBIX_HOST', '127.0.0.1'),
    "user": os.getenv('ZABBIX_USER', 'usuario_zabbix'),
    "password": os.getenv('ZABBIX_PASS', 'senha_zabbix'),
    "database": "zabbix",
    "port": "5432"
}

# Configurações de Servidores e SSH
SSH_HOST = os.getenv('SSH_VOIP_HOST', '0.0.0.0')
SSH_USER = os.getenv('SSH_VOIP_USER', 'user')
SSH_PASS = os.getenv('SSH_VOIP_PASS', 'pass')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "logs_bot.db")

# --- NVR CONFIG ---
NVR_HOST = os.getenv('NVR_HOST', '0.0.0.0')
NVR_PORTA = "555"
NVR_USER = os.getenv('NVR_USER', 'usuario_nvr')
NVR_PASS = os.getenv('NVR_PASS', 'senha_nvr')

# Estados da conversa /gravacao
GRAV_CANAL, GRAV_INICIO, GRAV_FIM = range(3)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
client = Groq(api_key=CHAVE_GROQ)

# --- AJUDA ---
async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_ajuda = (
        "<b>🛠️ Central de Comandos - Galeguinho</b>\n\n"
        "Aqui estão as ferramentas disponíveis para monitoramento:\n\n"
        "🖥️ <b>/hosts</b>\n"
        "<i>Lista todos os dispositivos cadastrados no Zabbix e seus respectivos IPs.</i>\n\n"
        "📡 <b>/ping [IP ou Nome]</b>\n"
        "<i>Testa a latência e perda de pacotes. Ex: /ping 8.8.8.8 ou /ping Mikrotik</i>\n\n"
        "🎥 <b>/camera [IP ou Nome]</b>\n"
        "<i>Grava um clipe de 10s da câmera informada. Ex: /camera Portaria</i>\n\n"
        "📼 <b>/gravacao</b>\n"
        "<i>Baixa uma gravação do NVR por canal, data e hora.</i>\n\n"
        "🌐 <b>/traceroute [IP ou host]</b>\n"
        "<i>Mostra os saltos até o destino. Ex: /traceroute 8.8.8.8</i>\n\n"
        "📋 <b>/logs</b>\n"
        "<i>Exibe os incidentes ativos no Zabbix.</i>\n\n"
        "🚀 <b>/speedtest</b>\n"
        "<i>Testa a velocidade real da internet no servidor Zabbix.</i>\n\n"
        "🔄 <b>/issabel</b>\n"
        "<i>Envia um comando de REBOOT imediato para o servidor VoIP Issabel via SSH.</i>\n\n"
        "💡 <i>Dica: Você também pode conversar comigo por texto para analisar logs!</i>"
    )
    await update.message.reply_text(texto_ajuda, parse_mode=ParseMode.HTML)

# --- HOSTS ---
def buscar_lista_hosts_zabbix():
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        query = """
        SELECT h.name, i.ip
        FROM hosts h
        JOIN interface i ON h.hostid = i.hostid
        WHERE h.status = 0
          AND h.flags IN (0, 4)
          AND i.main = 1
        ORDER BY h.name ASC;
        """
        cursor.execute(query)
        hosts = cursor.fetchall()
        conn.close()
        if not hosts:
            return "📭 <b>Nenhum host ativo encontrado.</b>"
        msg = "<b>🖥️ Dispositivos no Zabbix</b>\n\n"
        for nome, ip in hosts:
            msg += f"🔹 <code>{ip}</code> | <b>{nome}</b>\n"
        msg += f"\n📊 <i>Total de {len(hosts)} dispositivos.</i>"
        return msg
    except Exception as e:
        logging.error(f"Erro ao listar hosts: {e}")
        return "❌ <b>Erro ao acessar o banco do Zabbix.</b>"

async def cmd_hosts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    resultado = buscar_lista_hosts_zabbix()
    await update.message.reply_text(resultado, parse_mode=ParseMode.HTML)

def buscar_lista_hosts_zabbix_texto() -> str:
    """Retorna lista de hosts e IPs em texto simples para contexto da IA."""
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        query = """
        SELECT h.name, i.ip
        FROM hosts h
        JOIN interface i ON h.hostid = i.hostid
        WHERE h.status = 0 AND h.flags IN (0, 4) AND i.main = 1
        ORDER BY h.name ASC;
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()
        return "\n".join([f"{ip} = {nome}" for nome, ip in rows])
    except Exception as e:
        return f"Erro ao buscar hosts: {e}"

def _executar_ping(ip: str) -> str:
    try:
        out = subprocess.run(
            ["ping", "-c", "4", "-W", "2", ip],
            capture_output=True, text=True, timeout=15
        )
        return out.stdout or out.stderr
    except Exception as e:
        return f"Erro ao executar ping: {e}"

def _executar_traceroute(ip: str) -> str:
    try:
        out = subprocess.run(
            ["traceroute", "-n", "-w", "2", "-m", "15", ip],
            capture_output=True, text=True, timeout=45
        )
        return out.stdout or out.stderr
    except Exception as e:
        return f"Erro ao executar traceroute: {e}"

def _extrair_ips_dos_incidentes(dados: str) -> list:
    """Extrai IPs entre parênteses do texto de incidentes."""
    return re.findall(r'\((\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\)', dados)

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    dados = buscar_alertas_zabbix()

    if dados == "NENHUM_INCIDENTE_ATIVO":
        await update.message.reply_text("✅ <b>Nenhum incidente ativo no momento.</b>", parse_mode=ParseMode.HTML)
        return

    # Mensagem 1: lista os incidentes
    await update.message.reply_text(
        "<b>🚨 Incidentes Ativos no Zabbix</b>\n\n" + dados,
        parse_mode=ParseMode.HTML
    )

    # Palavras que indicam incidente de conectividade
    KEYWORDS_REDE = [
        "ping", "icmp", "link down", "unreachable", "offline",
        "unavailable", "no route", "timeout", "connection"
    ]

    # Separa incidentes de rede dos demais
    linhas_incidentes = dados.split("\n")[1:]  # pula o cabeçalho de contagem
    ips_rede = []
    tem_outros = False

    for linha in linhas_incidentes:
        linha_lower = linha.lower()
        if any(k in linha_lower for k in KEYWORDS_REDE):
            ips = re.findall(r'\((\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\)', linha)
            ips_rede.extend(ips)
        elif linha.strip():
            tem_outros = True

    # Roda diagnóstico só para incidentes de rede
    diagnosticos = ""
    if ips_rede:
        await update.message.reply_text("🔍 <i>Executando diagnóstico de rede automático...</i>", parse_mode=ParseMode.HTML)
        await update.message.reply_chat_action(action=ChatAction.TYPING)
        for ip in set(ips_rede):
            ping_result = _executar_ping(ip)
            trace_result = _executar_traceroute(ip)
            diagnosticos += f"\n--- Diagnóstico {ip} ---\nPING:\n{ping_result}\nTRACEROUTE:\n{trace_result}\n"

    if tem_outros and not ips_rede:
        diagnosticos = "Incidentes não relacionados a conectividade — sem diagnóstico de rede."
    elif not diagnosticos:
        diagnosticos = "Nenhum IP encontrado para diagnóstico automático."

    # IA analisa os resultados e dá um resumo
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": (
                    "Você é o Agente M7, atendente de suporte técnico sênior. "
                    "Você já rodou o ping e traceroute dos hosts com incidente. "
                    "Analise os resultados e dê um diagnóstico direto e humano — como um técnico experiente falando no chat. "
                    "Diga o que está acontecendo com base nos dados reais: se o host responde, onde a rota trava, qual o salto problemático. "
                    "Se o ping falhou completamente, diga isso. Se perdeu pacotes, mencione. "
                    "Seja direto e objetivo — sem listas longas, fale como humano. "
                    "Use <b>negrito</b> HTML para IPs e termos técnicos. NUNCA use asteriscos ou markdown."
                )},
                {"role": "user", "content": (
                    "Inventário de hosts cadastrados no Zabbix (use para identificar IPs nos saltos):\n" + buscar_lista_hosts_zabbix_texto() +
                    "\n\nIncidentes ativos:\n" + dados +
                    "\n\nResultados do diagnóstico automático (ping e traceroute):\n" + diagnosticos +
                    "\n\nO que está acontecendo e o que pode ser feito?"
                )}
            ]
        )
        analise = completion.choices[0].message.content.replace("**", "")
        await update.message.reply_text(
            "<b>🔧 Diagnóstico Automático</b>\n\n" + analise,
            parse_mode=ParseMode.HTML
        )
        salvar_mensagem(update.effective_chat.id, "Agente M7", analise)
    except Exception as e:
        logging.error(f"Erro IA /logs: {e}")

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

# --- ZABBIX: APENAS PROBLEMAS REALMENTE ATIVOS ---
def buscar_alertas_zabbix():
    """
    r_eventid IS NULL = problema ainda não foi resolvido.
    Sem esse filtro a IA recebe dados de incidentes antigos e alucina.
    """
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        cursor.execute("SET TIME ZONE 'America/Sao_Paulo';")
        query = """
        SELECT
            h.name AS host,
            COALESCE(iface.ip, '?') AS ip,
            p.name AS incidente,
            p.severity,
            to_timestamp(p.clock) AT TIME ZONE 'America/Sao_Paulo' AS inicio,
            EXTRACT(EPOCH FROM (NOW() - to_timestamp(p.clock)))::int AS segundos
        FROM problem p
        JOIN triggers t ON t.triggerid = p.objectid
        JOIN functions f ON f.triggerid = t.triggerid
        JOIN items i ON i.itemid = f.itemid
        JOIN hosts h ON h.hostid = i.hostid
        LEFT JOIN interface iface ON iface.hostid = h.hostid AND iface.main = 1
        WHERE p.r_eventid IS NULL
          AND p.source = 0
          AND p.object = 0
          AND h.status = 0
          AND t.status = 0
          AND t.value = 1
          AND p.severity > 1
        GROUP BY h.name, iface.ip, p.name, p.severity, p.clock
        ORDER BY p.severity DESC, p.clock ASC;
        """
        cursor.execute(query)
        res = cursor.fetchall()
        conn.close()

        if not res:
            return "NENHUM_INCIDENTE_ATIVO"

        severidades = {0: '⚪ Não classificado', 1: '🔵 Informação',
                       2: '🟡 Atenção', 3: '🟠 Média', 4: '🔴 Alta', 5: '🔥 Desastre'}
        alertas = []
        for host, ip, incidente, sev, inicio, segundos in res:
            label_sev = severidades.get(sev, '⚠️')
            seg = int(segundos)
            if seg < 60:
                duracao = f"{seg}s"
            elif seg < 3600:
                duracao = f"{seg // 60}min"
            else:
                duracao = f"{seg // 3600}h {(seg % 3600) // 60}min"
            alertas.append(
                f"{label_sev} | {host} ({ip}): {incidente} | Desde: {inicio.strftime('%d/%m %H:%M')} ({duracao})"
            )

        return f"{len(res)} incidente(s) ativo(s):\n" + "\n".join(alertas)

    except Exception as e:
        logging.error(f"Erro Zabbix: {e}")
        return f"Erro ao consultar Zabbix: {e}"

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
    except:
        res = "❌ Erro ao executar speedtest-cli."
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

async def cmd_traceroute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /traceroute [IP ou host]")
        return
    alvo = context.args[0]
    await update.message.reply_text(f"🔍 Executando traceroute para <code>{alvo}</code>...", parse_mode=ParseMode.HTML)
    await update.message.reply_chat_action(action=ChatAction.TYPING)
    try:
        resultado = subprocess.run(
            ["traceroute", "-n", "-w", "2", "-m", "20", alvo],
            capture_output=True, text=True, timeout=60
        )
        saida = resultado.stdout or resultado.stderr
        if not saida.strip():
            await update.message.reply_text("❌ Sem resposta do traceroute.")
            return

        # Formata os saltos
        linhas = saida.strip().split("\n")
        cabecalho = "<b>🔍 Traceroute → " + alvo + "</b>\n\n<code>"
        corpo = "\n".join(linhas)
        msg = cabecalho + corpo + "</code>"

        # Telegram limita 4096 chars
        if len(msg) > 4096:
            msg = msg[:4090] + "...</code>"

        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("❌ Timeout: traceroute demorou mais de 60s.")
    except FileNotFoundError:
        await update.message.reply_text("❌ <code>traceroute</code> não instalado. Rode: <code>apt install traceroute</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {html.escape(str(e))}")

async def cmd_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /camera [IP]")
        return
    ip = context.args[0]
    video_path = f"/tmp/cam_{ip}.mp4"
    rtsp_url = f"rtsp://m7:152535M7@{ip}:554/cam/realmonitor?channel=1&subtype=0"
    await update.message.reply_text(f"🎥 Tentando gravar 10s da câmera {ip}...")
    await update.message.reply_chat_action(action=ChatAction.UPLOAD_VIDEO)
    cmd = ['ffmpeg', '-y', '-rtsp_transport', 'tcp', '-timeout', '5000000',
           '-i', rtsp_url, '-t', '10', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
           '-preset', 'ultrafast', '-an', video_path]
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
            await update.message.reply_video(video=open(video_path, 'rb'), caption=f"📹 Clip: {ip}")
            os.remove(video_path)
        else:
            error_msg = process.stderr[-200:]
            await update.message.reply_text(f"❌ Falha na captura.\n\n<b>Log:</b> <code>{html.escape(error_msg)}</code>", parse_mode=ParseMode.HTML)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("❌ Timeout: A câmera demorou muito para responder.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")

# --- ISSABEL ---
async def cmd_reset_voip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[
        InlineKeyboardButton("✅ Sim, reiniciar agora", callback_data='reboot_sim'),
        InlineKeyboardButton("❌ Não, cancelar", callback_data='reboot_nao')
    ]]
    await update.message.reply_text(
        "<b>⚠️ ATENÇÃO: REBOOT DO ISSABEL</b>\n\n"
        "Você tem certeza que deseja reiniciar o servidor VoIP agora?\n"
        "Isso derrubará todas as chamadas em curso!",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirmar_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'reboot_sim':
        await query.edit_message_text("⏳ Conectando ao Issabel e enviando comando de reboot...")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=10)
            ssh.exec_command('reboot')
            ssh.close()
            await query.edit_message_text("✅ <b>O servidor Issabel está reiniciando!</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.edit_message_text(f"❌ <b>Erro ao reiniciar:</b> {e}", parse_mode=ParseMode.HTML)
    elif query.data == 'reboot_nao':
        await query.edit_message_text("❌ <b>Operação cancelada pelo usuário.</b>", parse_mode=ParseMode.HTML)

# ============================================================
# GRAVAÇÃO NVR — /gravacao
# ============================================================

ESPACO_MINIMO_BYTES = 300 * 1024 * 1024
LIMITE_TELEGRAM_BYTES = 45 * 1024 * 1024

def _limpar_temporarios():
    for f in os.listdir("/tmp"):
        if f.startswith("NVR_canal") and (f.endswith(".dav") or f.endswith(".mp4")):
            try:
                os.remove(f"/tmp/{f}")
                logging.info(f"Temporário removido: {f}")
            except:
                pass

def _checar_espaco():
    stat = os.statvfs("/tmp")
    livre = stat.f_bavail * stat.f_frsize
    if livre < ESPACO_MINIMO_BYTES:
        livre_mb = round(livre / 1024 / 1024)
        raise Exception(f"Espaço insuficiente em /tmp: apenas {livre_mb}MB livres. Tente novamente em instantes.")

def _baixar_dav(canal, inicio, fim):
    start = inicio.replace(" ", "%20")
    end   = fim.replace(" ", "%20")
    url = (f"http://{NVR_HOST}:{NVR_PORTA}/cgi-bin/loadfile.cgi"
           f"?action=startLoad&channel={canal}"
           f"&startTime={start}&endTime={end}&subtype=0&Types=dav")
    auth = HTTPDigestAuth(NVR_USER, NVR_PASS)
    r = requests.get(url, auth=auth, timeout=60, stream=True)
    if r.status_code != 200:
        raise Exception(f"NVR retornou HTTP {r.status_code}")
    # Timestamp no nome evita conflito com arquivos anteriores travados
    ts = datetime.now().strftime("%H%M%S")
    arquivo_dav = f"/tmp/NVR_canal{canal}_{ts}.dav"
    with open(arquivo_dav, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    if os.path.getsize(arquivo_dav) < 500:
        os.remove(arquivo_dav)
        raise Exception("Arquivo muito pequeno — sem gravação nesse período.")
    return arquivo_dav

def _converter_para_mp4(arquivo_dav):
    arquivo_mp4 = arquivo_dav.replace(".dav", ".mp4")
    bitrate_alvo = 800
    vf = "scale=854:480:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2"

    r = subprocess.run(
        ["ffmpeg", "-y", "-i", arquivo_dav,
         "-vcodec", "libx264", "-vf", vf,
         "-b:v", f"{bitrate_alvo}k", "-preset", "fast",
         "-pix_fmt", "yuv420p", "-an",
         "-movflags", "+faststart",  # garante que o container MP4 seja finalizado corretamente
         arquivo_mp4],
        capture_output=True, text=True
    )

    # Remove DAV independente do resultado
    if os.path.exists(arquivo_dav):
        os.remove(arquivo_dav)

    if r.returncode != 0 or not os.path.exists(arquivo_mp4) or os.path.getsize(arquivo_mp4) < 1000:
        raise Exception(f"Falha na conversão: {r.stderr[-500:]}")

    tamanho = os.path.getsize(arquivo_mp4)

    if tamanho > LIMITE_TELEGRAM_BYTES:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", arquivo_mp4],
            capture_output=True, text=True
        )
        try:
            duracao = float(probe.stdout.strip())
            bitrate_alvo = max(150, int((LIMITE_TELEGRAM_BYTES * 8) / duracao / 1000))
        except:
            bitrate_alvo = 300

        arquivo_mp4_v2 = arquivo_mp4.replace(".mp4", "_v2.mp4")
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", arquivo_mp4,
             "-vcodec", "libx264", "-vf", vf,
             "-b:v", f"{bitrate_alvo}k", "-preset", "fast",
             "-pix_fmt", "yuv420p", "-an",
             "-movflags", "+faststart",
             arquivo_mp4_v2],
            capture_output=True, text=True
        )
        os.remove(arquivo_mp4)
        if r2.returncode != 0 or not os.path.exists(arquivo_mp4_v2):
            raise Exception(f"Falha na recompressão: {r2.stderr[-500:]}")
        arquivo_mp4 = arquivo_mp4_v2
        tamanho = os.path.getsize(arquivo_mp4)

    return arquivo_mp4, bitrate_alvo, round(tamanho / 1024 / 1024, 1)

async def cmd_gravacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 <b>Download de Gravação NVR</b>\n\n"
        "Informe o <b>número do canal</b>:\n\n"
        "1️⃣  <code>1</code> — Acesso Pedestres\n"
        "2️⃣  <code>2</code> — Saída\n"
        "3️⃣  <code>3</code> — Parquinho / Club\n"
        "6️⃣  <code>6</code> — Club\n"
        "7️⃣  <code>7</code> — Acesso Visitante\n"
        "8️⃣  <code>8</code> — Portão de Entrada\n"
        "🔟  <code>10</code> — Portaria\n\n"
        "Digite /cancelar para sair.",
        parse_mode=ParseMode.HTML
    )
    return GRAV_CANAL

async def grav_receber_canal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto.isdigit() or int(texto) < 1:
        await update.message.reply_text("⚠️ Canal inválido. Digite um número (ex: <code>1</code>):", parse_mode=ParseMode.HTML)
        return GRAV_CANAL
    context.user_data['grav_canal'] = int(texto)
    await update.message.reply_text(
        "📅 Informe a <b>data e hora de início</b>:\n"
        "<code>AAAA-MM-DD HH:MM:SS</code>\n\n"
        "Ex: <code>2026-03-31 21:10:00</code>",
        parse_mode=ParseMode.HTML
    )
    return GRAV_INICIO

async def grav_receber_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        await update.message.reply_text("⚠️ Formato inválido. Use: <code>AAAA-MM-DD HH:MM:SS</code>", parse_mode=ParseMode.HTML)
        return GRAV_INICIO
    context.user_data['grav_inicio'] = texto
    await update.message.reply_text(
        "📅 Informe a <b>data e hora de fim</b>:\n"
        "<code>AAAA-MM-DD HH:MM:SS</code>\n\n"
        "Ex: <code>2026-03-31 21:15:00</code>\n"
        "<i>⚠️ Intervalo máximo: 15 minutos.</i>",
        parse_mode=ParseMode.HTML
    )
    return GRAV_FIM

async def grav_receber_fim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    try:
        datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        await update.message.reply_text("⚠️ Formato inválido. Use: <code>AAAA-MM-DD HH:MM:SS</code>", parse_mode=ParseMode.HTML)
        return GRAV_FIM

    canal  = context.user_data['grav_canal']
    inicio = context.user_data['grav_inicio']
    fim    = texto

    dt_inicio = datetime.strptime(inicio, "%Y-%m-%d %H:%M:%S")
    dt_fim    = datetime.strptime(fim,    "%Y-%m-%d %H:%M:%S")
    diferenca = (dt_fim - dt_inicio).total_seconds()
    if diferenca <= 0:
        await update.message.reply_text("⚠️ A data de fim deve ser posterior ao início. Tente novamente:")
        return GRAV_FIM
    if diferenca > 15 * 60:
        await update.message.reply_text("⚠️ Intervalo máximo é de <b>15 minutos</b>. Tente novamente:", parse_mode=ParseMode.HTML)
        return GRAV_FIM

    await update.message.reply_text(
        f"⏳ Baixando canal <b>{canal}</b>\nDe: <code>{inicio}</code>\nAté: <code>{fim}</code>",
        parse_mode=ParseMode.HTML
    )
    await update.message.reply_chat_action(action=ChatAction.UPLOAD_VIDEO)

    try:
        _limpar_temporarios()
        _checar_espaco()
        arquivo_dav = _baixar_dav(canal, inicio, fim)
        arquivo_mp4, bitrate, tamanho_mb = _converter_para_mp4(arquivo_dav)
        with open(arquivo_mp4, 'rb') as f:
            await update.message.reply_video(
                video=f,
                caption=(f"📹 Canal {canal} | {inicio} → {fim}\n"
                         f"<i>854x480 | {bitrate}kbps | {tamanho_mb}MB</i>"),
                supports_streaming=True,
                parse_mode=ParseMode.HTML
            )
        os.remove(arquivo_mp4)
    except Exception as e:
        await update.message.reply_text(
            f"❌ <b>Erro:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML
        )

    return ConversationHandler.END

async def grav_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END

# ============================================================
# IA
# ============================================================

KEYWORDS_MONITORAMENTO = [
    "host", "hosts", "problema", "problemas", "alerta", "alertas",
    "incidente", "incidentes", "down", "offline", "ping", "zabbix",
    "ativo", "ativos", "status", "severidade", "resumo", "persiste",
    "camera", "link", "vpn", "servidor", "dispositivo",
    "falhou", "erro", "lento", "fora", "caiu", "resolve", "resolvido",
    "cpu", "memoria", "uptime", "disco", "interface", "trafico",
    "temperatura", "metrica", "coleta", "uso", "carga", "consumo",
    "quanto", "esta", "como", "velocidade", "latencia"
]

def _e_pergunta_monitoramento(texto: str) -> bool:
    return any(k in texto.lower() for k in KEYWORDS_MONITORAMENTO)

def buscar_metricas_host(nome_host: str):
    """
    Busca últimos valores coletados de um host usando lastvalue/lastclock
    direto da tabela items — muito mais rápido que subqueries nas history_*.
    """
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        cursor.execute("SET TIME ZONE 'America/Sao_Paulo';")
        query = """
        SELECT
            i.name AS metrica,
            i.lastvalue,
            i.units,
            to_timestamp(i.lastclock) AT TIME ZONE 'America/Sao_Paulo' AS ultima_coleta
        FROM items i
        JOIN hosts h ON h.hostid = i.hostid
        WHERE h.status = 0
          AND i.status = 0
          AND i.state = 0
          AND i.lastclock > 0
          AND i.lastvalue IS NOT NULL
          AND i.lastvalue != ''
          AND LOWER(h.name) LIKE LOWER(%s)
        ORDER BY i.lastclock DESC
        LIMIT 80;
        """
        cursor.execute(query, (f"%{nome_host}%",))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return None

        linhas = []
        for metrica, valor, unidade, ultima_coleta in rows:
            unidade = unidade or ""
            coleta = ultima_coleta.strftime("%d/%m %H:%M") if ultima_coleta else "?"
            linhas.append(f"  {metrica}: {valor} {unidade} (coletado: {coleta})")
        return "\n".join(linhas) if linhas else None

    except Exception as e:
        logging.error(f"Erro metricas: {e}")
        return None

def buscar_hosts_disponiveis():
    try:
        conn = psycopg2.connect(**ZABBIX_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM hosts WHERE status = 0 AND flags IN (0,4) ORDER BY name;")
        rows = cursor.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except:
        return []

async def gerenciar_mensagens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or (msg.text and msg.text.startswith("/")): return
    salvar_mensagem(update.effective_chat.id, msg.from_user.first_name, msg.text)

    if f"@{context.bot.username}" in msg.text or update.effective_chat.type == "private":
        await msg.reply_chat_action(action=ChatAction.TYPING)
        conversa = buscar_contexto_conversa(update.effective_chat.id)

        system_prompt = (
            "Você é o Agente M7, atendente de suporte técnico sênior. "
            "Fale como um humano no chat — natural, direto, sem listas longas. "
            "Investigue junto com a pessoa: faça perguntas, sugira um passo de cada vez. "
            "Você conhece redes, Linux, infraestrutura e segurança. "
            "Este bot tem os comandos /ping e /traceroute disponíveis — sugira-os quando fizer sentido. "
            "Use <b>negrito</b> HTML só para comandos e termos técnicos. NUNCA use asteriscos ou markdown.\n\n"
            "[HISTORICO DA CONVERSA]:\n" + conversa
        )

        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": msg.text}
                ]
            )
            resp = completion.choices[0].message.content.replace("**", "")
            await msg.reply_text(resp, parse_mode=ParseMode.HTML)
            salvar_mensagem(update.effective_chat.id, "Agente M7", resp)
        except Exception as e:
            logging.error(f"Erro IA: {e}")

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    init_db()
    application = ApplicationBuilder().token(TOKEN_TELEGRAM).build()

    gravacao_conv = ConversationHandler(
        entry_points=[CommandHandler('gravacao', cmd_gravacao)],
        states={
            GRAV_CANAL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, grav_receber_canal)],
            GRAV_INICIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, grav_receber_inicio)],
            GRAV_FIM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, grav_receber_fim)],
        },
        fallbacks=[CommandHandler('cancelar', grav_cancelar)],
    )

    application.add_handler(CommandHandler('issabel', cmd_reset_voip))
    application.add_handler(CommandHandler('ping', cmd_ping))
    application.add_handler(CommandHandler('traceroute', cmd_traceroute))
    application.add_handler(CommandHandler('speedtest', cmd_speedtest))
    application.add_handler(CommandHandler('camera', cmd_camera))
    application.add_handler(CommandHandler('hosts', cmd_hosts))
    application.add_handler(CommandHandler('logs', cmd_logs))
    application.add_handler(CommandHandler('ajuda', cmd_ajuda))
    application.add_handler(CommandHandler('help', cmd_ajuda))
    application.add_handler(CommandHandler('start', cmd_ajuda))
    application.add_handler(gravacao_conv)
    application.add_handler(CallbackQueryHandler(confirmar_reboot))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), gerenciar_mensagens))
    application.run_polling()
