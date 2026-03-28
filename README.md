## 🤖 Bot M7 - Monitoramento Redes

O **Bot M7** é um bot de Telegram integrado ao Zabbix e Groq (Llama 3) para monitoramento de infraestrutura em tempo real. Ele permite consultar alertas, realizar testes de rede e gerenciar serviços via SSH.

### 📋 Pré-requisitos

Antes de rodar o bot, você precisará de:
* Python 3.9 ou superior.
* Banco de dados PostgreSQL (Zabbix).
* Acesso SSH para servidores (opcional para o comando reset (reboot servidor), utilize qualquer outro comando que preferir).
* `speedtest-cli` instalado no sistema operacional do bot.

### 🛠️ Instalação

1.  **Clone o repositório:**
    ```bash
    git clone https://github.com/seu-usuario/m7-bot.git
    cd m7-bot
    ```

2.  **Instale as dependências do Python:**
    ```bash
    pip install python-telegram-bot paramiko psycopg2-binary groq
    ```

3.  **Instale o Speedtest CLI no Linux:**
    ```bash
    sudo apt update && sudo apt install speedtest-cli
    ```

### ⚙️ Configuração (Variáveis de Ambiente)

Para segurança, o bot não armazena senhas no código. Você deve exportar as seguintes variáveis no seu terminal ou colocar num arquivo `.env`:

```bash
# Telegram e IA
export TELEGRAM_TOKEN='seu_token_aqui'
export GROQ_API_KEY='sua_chave_groq_aqui'

# Banco de Dados Zabbix
export ZABBIX_DB_HOST='172.x.x.x'
export ZABBIX_DB_USER='zabbix'
export ZABBIX_DB_PASS='sua_senha'
export ZABBIX_DB_NAME='zabbix'

# Acesso SSH VoIP
export SSH_VOIP_HOST='192.x.x.x'
export SSH_VOIP_USER='root'
export SSH_VOIP_PASS='sua_senha'
```

### 🚀 Como Rodar

Basta executar o script principal:
```bash
python bot.py
```

### 🕹️ Comandos Disponíveis

* `/speedtest`: Realiza um teste de velocidade da conexão do servidor.
* `/ping [IP/Host]`: Verifica a latência e perda de pacotes de um alvo.
* `/resetvoip`: Envia um comando de reboot via SSH para o servidor Issabel.
* **Mensagens Diretas**: O bot usa a IA para analisar os últimos 25 eventos do Zabbix e responder dúvidas sobre o status da rede.
