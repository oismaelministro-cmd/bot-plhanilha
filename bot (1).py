import os
import json
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]
ALLOWED_USER_ID   = int(os.environ["ALLOWED_USER_ID"])
OPENAI_API_KEY    = os.environ["OPENAI_API_KEY"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MONTH_NAMES = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    now = datetime.now()
    tab_name = f"{MONTH_NAMES[now.month]} - {now.year}"
    try:
        return spreadsheet.worksheet(tab_name)
    except Exception:
        return spreadsheet.get_worksheet(0)


def append_row(data: dict):
    ws = get_sheet()
    now = datetime.now()
    row = [
        data.get("data", now.strftime("%d/%m/%Y")),
        data.get("movimento", ""),
        data.get("tr", ""),
        data.get("descricao", ""),
        data.get("cliente_fornecedor", ""),
        data.get("banco", ""),
        data.get("valor", ""),
        data.get("observacoes", ""),
        "",  # Conciliado
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_all_records() -> list[dict]:
    ws = get_sheet()
    rows = ws.get_all_values()
    if len(rows) < 8:
        return []
    headers = rows[6]
    records = []
    for row in rows[7:]:
        if any(cell.strip() for cell in row):
            record = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
            records.append(record)
    return records


def build_financial_summary() -> str:
    records = get_all_records()
    if not records:
        return "Nenhum lançamento encontrado na planilha do mês atual."

    now = datetime.now()
    tab_name = f"{MONTH_NAMES[now.month]} - {now.year}"

    entradas_total = 0.0
    saidas_total = 0.0
    por_banco: dict[str, dict] = {}
    por_categoria: dict[str, float] = {}
    lancamentos: list[str] = []

    for r in records:
        movimento = r.get("Movimento", r.get("movimento", "")).strip()
        descricao = r.get("Descrição", r.get("descricao", r.get("Descrição", ""))).strip()
        banco     = r.get("Banco", r.get("banco", "")).strip()
        cliente   = r.get("Cliente/Fornecedor", r.get("cliente_fornecedor", "")).strip()
        data_     = r.get("Data", r.get("data", "")).strip()

        valor_raw = r.get("Valor", r.get("valor", "0")).strip()
        valor_raw = valor_raw.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
        try:
            valor = float(valor_raw)
        except Exception:
            valor = 0.0

        is_entrada = "entrada" in movimento.lower()

        if banco not in por_banco:
            por_banco[banco] = {"entradas": 0.0, "saidas": 0.0}
        if is_entrada:
            entradas_total += valor
            por_banco[banco]["entradas"] += valor
        else:
            saidas_total += valor
            por_banco[banco]["saidas"] += valor

        cat = descricao.lower() if descricao else "sem descrição"
        por_categoria[cat] = por_categoria.get(cat, 0.0) + valor

        lancamentos.append(
            f"  [{data_}] {movimento} | {descricao} | {cliente or '-'} | {banco} | R${valor:.2f}"
        )

    saldo = entradas_total - saidas_total

    linhas_banco = []
    for banco, vals in por_banco.items():
        if banco:
            saldo_b = vals["entradas"] - vals["saidas"]
            linhas_banco.append(
                f"  {banco}: entradas R${vals['entradas']:.2f} / saídas R${vals['saidas']:.2f} / saldo R${saldo_b:.2f}"
            )

    top_gastos = sorted(por_categoria.items(), key=lambda x: x[1], reverse=True)[:5]
    linhas_gastos = [f"  {cat}: R${v:.2f}" for cat, v in top_gastos]

    resumo = (
        f"=== RESUMO FINANCEIRO — {tab_name} ===\n"
        f"Total de lançamentos: {len(records)}\n"
        f"Entradas totais: R${entradas_total:.2f}\n"
        f"Saídas totais:   R${saidas_total:.2f}\n"
        f"Saldo do período: R${saldo:.2f}\n\n"
        f"--- Por banco ---\n" + "\n".join(linhas_banco) + "\n\n"
        f"--- Top 5 categorias de gasto ---\n" + "\n".join(linhas_gastos) + "\n\n"
        f"--- Todos os lançamentos ---\n" + "\n".join(lancamentos[-60:])
    )
    return resumo


# ─── OPENAI ───────────────────────────────────────────────────────────────────
PARSE_SYSTEM = (
    "Você é um assistente financeiro que extrai dados de lançamentos financeiros. "
    "O usuário vai descrever uma entrada ou saída de dinheiro em linguagem natural. "
    "Retorne SOMENTE um JSON válido (sem markdown, sem explicações) com os campos:\n"
    '{\n'
    '  "movimento": "Entrada" ou "Saída",\n'
    '  "tr": "tipo resumido, ex: pagamento, venda, compra",\n'
    '  "descricao": "descrição curta do item",\n'
    '  "cliente_fornecedor": "nome da pessoa ou empresa, se mencionado",\n'
    '  "banco": "NuBank, Santander, Caixinha ou banco mencionado",\n'
    '  "valor": "valor em reais, ex: 150.00",\n'
    '  "observacoes": "qualquer observação extra",\n'
    '  "data": "data no formato DD/MM/YYYY — use hoje se não informado"\n'
    "}\n"
    f"Hoje é {datetime.now().strftime('%d/%m/%Y')}."
)

CONSULTANT_SYSTEM = """Você é o consultor financeiro pessoal da Porto Glass, uma empresa de vidros e películas.
Você tem acesso ao extrato financeiro completo do mês e deve:

1. Responder perguntas sobre as finanças de forma clara e direta
2. Dar alertas quando identificar problemas (saldo negativo, gastos altos, tendências ruins)
3. Dar insights úteis e acionáveis
4. Ser direto, usar emojis para facilitar leitura
5. Sempre terminar com 1 recomendação prática quando relevante

Alertas importantes para identificar:
- Saldo mensal negativo ou próximo de zero
- Muitas saídas em sequência sem entradas
- Categoria de gasto representando mais de 30% do total
- Saídas sem descrição ou cliente (risco de perda de controle)

Responda sempre em português brasileiro, de forma amigável mas profissional.
"""

ai_client = OpenAI(api_key=OPENAI_API_KEY)


def parse_lancamento(text: str) -> dict:
    response = ai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        max_tokens=512,
        messages=[
            {"role": "system", "content": PARSE_SYSTEM},
            {"role": "user", "content": text},
        ],
    )
    raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def is_financial_question(text: str) -> bool:
    question_keywords = [
        "quanto", "qual", "como", "estou", "tenho", "gastei", "recebi",
        "saldo", "resumo", "relatório", "analise", "analisa", "situação",
        "alerta", "dica", "conselho", "posso", "devo", "vale", "melhor",
        "pior", "mais", "menos", "total", "mês", "semana", "hoje",
        "nubank", "santander", "caixinha", "banco",
        "?", "me diz", "me fala", "me mostra",
    ]
    text_lower = text.lower()
    lancamento_keywords = ["saída", "entrada", "paguei", "recebi r$", "saiu", "entrou"]
    for kw in lancamento_keywords:
        if kw in text_lower and "r$" in text_lower:
            return False
    for kw in question_keywords:
        if kw in text_lower:
            return True
    return False


def consult_gpt(question: str, financial_summary: str) -> str:
    prompt = (
        f"Dados financeiros atuais da empresa:\n\n{financial_summary}\n\n"
        f"Pergunta do usuário: {question}"
    )
    response = ai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        max_tokens=1024,
        messages=[
            {"role": "system", "content": CONSULTANT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()


def generate_alert(data: dict, financial_summary: str) -> str | None:
    prompt = (
        f"Acabou de ser registrado este lançamento:\n{json.dumps(data, ensure_ascii=False)}\n\n"
        f"Dados financeiros atuais:\n{financial_summary}\n\n"
        "Com base nesse lançamento e nos dados atuais, existe algum alerta importante ou insight relevante? "
        "Se sim, responda com uma mensagem curta (máx 3 linhas) começando com um emoji de alerta (⚠️, 📊, 💡). "
        "Se não há nada importante a alertar, responda exatamente: NENHUM"
    )
    response = ai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        max_tokens=256,
        messages=[
            {"role": "system", "content": CONSULTANT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    result = response.choices[0].message.content.strip()
    return None if result.upper() == "NENHUM" else result


# ─── KEYBOARD ─────────────────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Resumo do mês", "💰 Saldo por banco"],
        ["⚠️ Alertas", "📈 Maiores gastos"],
        ["❓ Ajuda"],
    ],
    resize_keyboard=True,
)


# ─── HANDLERS ─────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Olá! Sou o assistente financeiro da *Porto Glass*.\n\n"
        "Posso te ajudar a:\n"
        "• 📝 *Registrar* entradas e saídas na planilha\n"
        "• 📊 *Analisar* suas finanças do mês\n"
        "• ⚠️ *Alertar* sobre riscos e oportunidades\n"
        "• 💡 *Responder* qualquer dúvida financeira\n\n"
        "Use os botões abaixo ou me envie um lançamento, como:\n"
        "_Saída R$50 gasolina Santander_\n\n"
        "Ou faça uma pergunta:\n"
        "_Como estão minhas finanças esse mês?_",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    if text == "📊 Resumo do mês":
        await handle_consulta(update, "Me dê um resumo completo do mês com saldo, entradas, saídas e situação geral.")
        return
    if text == "💰 Saldo por banco":
        await handle_consulta(update, "Qual o saldo atual em cada banco? NuBank, Santander e Caixinha separados.")
        return
    if text == "⚠️ Alertas":
        await handle_consulta(update, "Existem alertas ou problemas financeiros que devo saber? Analise os dados e me avise.")
        return
    if text == "📈 Maiores gastos":
        await handle_consulta(update, "Quais são meus maiores gastos do mês? Liste por categoria e valor.")
        return
    if text == "❓ Ajuda":
        await update.message.reply_text(
            "🤖 *Como usar o assistente:*\n\n"
            "*Registrar lançamento:*\n"
            "  • _Saída R$50 gasolina Santander_\n"
            "  • _Entrada R$800 pagamento Ismael NuBank_\n"
            "  • _Saída película fumê R$600 Gilberto_\n\n"
            "*Perguntar ao consultor:*\n"
            "  • _Como estão minhas finanças?_\n"
            "  • _Quanto gastei com gasolina esse mês?_\n"
            "  • _Qual meu saldo no Santander?_\n"
            "  • _Tenho algum alerta importante?_\n"
            "  • _Posso fazer um gasto de R$500 hoje?_\n\n"
            "*Atalhos rápidos:* use os botões abaixo 👇",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if is_financial_question(text):
        await handle_consulta(update, text)
    else:
        await handle_lancamento(update, text)


async def handle_lancamento(update: Update, text: str):
    await update.message.reply_text("⏳ Registrando lançamento...")

    try:
        data = parse_lancamento(text)
        append_row(data)

        emoji = "🟢" if data.get("movimento") == "Entrada" else "🔴"
        reply = (
            f"{emoji} *{data.get('movimento', '')}* registrada!\n\n"
            f"📅 {data.get('data', '')}  |  🏦 {data.get('banco', '')}\n"
            f"📝 {data.get('descricao', '')}"
        )
        if data.get("cliente_fornecedor"):
            reply += f"  |  👤 {data.get('cliente_fornecedor')}"
        reply += f"\n💰 R$ {data.get('valor', '')}\n\n✅ Salvo na planilha!"

        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

        try:
            summary = build_financial_summary()
            alert = generate_alert(data, summary)
            if alert:
                await update.message.reply_text(alert, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Erro ao gerar alerta: {e}")

    except json.JSONDecodeError:
        await update.message.reply_text(
            "⚠️ Não entendi esse lançamento. Tente assim:\n"
            "_Saída R$100 gasolina Santander_\n"
            "_Entrada R$500 venda de espelho Gilberto NuBank_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Erro no lançamento: {e}")
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def handle_consulta(update: Update, question: str):
    await update.message.reply_text("🔍 Analisando sua planilha...")

    try:
        summary = build_financial_summary()
        answer = consult_gpt(question, summary)
        await update.message.reply_text(answer, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Erro na consulta: {e}")
        await update.message.reply_text(f"❌ Erro ao consultar: {str(e)}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "🎤 Áudio recebido! Por enquanto só processo texto.\n"
        "Escreva o lançamento ou a pergunta.",
        reply_markup=MAIN_KEYBOARD,
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("✅ Bot Porto Glass iniciado!")
    app.run_polling()


if __name__ == "__main__":
    main()
