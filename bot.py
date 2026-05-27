import os
import json
import re
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
SPREADSHEET_ID  = os.environ["SPREADSHEET_ID"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

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


def get_all_records() -> list:
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


def build_financial_summary() -> dict:
    records = get_all_records()
    entradas_total = 0.0
    saidas_total = 0.0
    por_banco = {}
    por_categoria = {}

    for r in records:
        movimento = r.get("Movimento", r.get("movimento", "")).strip()
        descricao = r.get("Descrição", r.get("descricao", "")).strip()
        banco = r.get("Banco", r.get("banco", "")).strip()

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

    return {
        "entradas": entradas_total,
        "saidas": saidas_total,
        "saldo": entradas_total - saidas_total,
        "por_banco": por_banco,
        "por_categoria": por_categoria,
        "total": len(records),
    }


# ─── PARSER SEM IA ────────────────────────────────────────────────────────────
BANCOS = ["nubank", "santander", "caixinha", "caixa", "bradesco", "itaú", "itau", "bb", "inter"]

PALAVRAS_ENTRADA = ["entrada", "recebi", "recebeu", "entrou", "venda", "vendeu", "cobrei"]
PALAVRAS_SAIDA   = ["saída", "saida", "paguei", "pagou", "saiu", "comprei", "comprou", "gastei"]


def parse_lancamento(text: str) -> dict:
    text_lower = text.lower()
    now = datetime.now()

    # Movimento
    movimento = "Saída"
    for p in PALAVRAS_ENTRADA:
        if p in text_lower:
            movimento = "Entrada"
            break

    # Valor
    valor = "0.00"
    match = re.search(r"r?\$?\s*(\d+(?:[.,]\d+)?)", text_lower)
    if match:
        valor = match.group(1).replace(",", ".")

    # Banco
    banco = ""
    for b in BANCOS:
        if b in text_lower:
            banco = b.capitalize()
            break

    # Descrição: remove valor e banco do texto para pegar o resto
    descricao = text
    descricao = re.sub(r"r?\$?\s*\d+(?:[.,]\d+)?", "", descricao, flags=re.IGNORECASE).strip()
    for b in BANCOS:
        descricao = re.sub(b, "", descricao, flags=re.IGNORECASE).strip()
    for p in PALAVRAS_ENTRADA + PALAVRAS_SAIDA:
        descricao = re.sub(p, "", descricao, flags=re.IGNORECASE).strip()
    descricao = " ".join(descricao.split())

    return {
        "movimento": movimento,
        "tr": "venda" if movimento == "Entrada" else "pagamento",
        "descricao": descricao if descricao else text,
        "cliente_fornecedor": "",
        "banco": banco,
        "valor": valor,
        "observacoes": "",
        "data": now.strftime("%d/%m/%Y"),
    }


def is_financial_question(text: str) -> bool:
    keywords = [
        "quanto", "qual", "saldo", "resumo", "relatório", "relatorio",
        "gastei", "recebi", "total", "mês", "mes", "situação", "situacao",
        "banco", "maiores", "gastos", "entradas", "saídas", "saidas",
    ]
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True
    if "?" in text:
        return True
    return False


# ─── KEYBOARD ─────────────────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Resumo do mês", "💰 Saldo por banco"],
        ["📈 Maiores gastos", "❓ Ajuda"],
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
        "• 📊 *Ver resumo* das finanças do mês\n\n"
        "Exemplos de lançamento:\n"
        "_Saída R$50 gasolina Santander_\n"
        "_Entrada R$800 venda NuBank_\n"
        "_Paguei R$200 fornecedor Caixinha_",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    if text == "📊 Resumo do mês":
        await handle_resumo(update)
        return
    if text == "💰 Saldo por banco":
        await handle_saldo_banco(update)
        return
    if text == "📈 Maiores gastos":
        await handle_maiores_gastos(update)
        return
    if text == "❓ Ajuda":
        await update.message.reply_text(
            "🤖 *Como usar o assistente:*\n\n"
            "*Registrar lançamento:*\n"
            "  • _Saída R$50 gasolina Santander_\n"
            "  • _Entrada R$800 venda NuBank_\n"
            "  • _Paguei R$120 aluguel Caixinha_\n"
            "  • _Recebi R$500 Gilberto Santander_\n\n"
            "*Atalhos rápidos:* use os botões abaixo 👇",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if is_financial_question(text):
        await handle_resumo(update)
    else:
        await handle_lancamento(update, text)


async def handle_lancamento(update: Update, text: str):
    await update.message.reply_text("⏳ Registrando lançamento...")
    try:
        data = parse_lancamento(text)
        append_row(data)

        emoji = "🟢" if data.get("movimento") == "Entrada" else "🔴"
        reply = (
            f"{emoji} *{data.get('movimento')}* registrada!\n\n"
            f"📅 {data.get('data')}  |  🏦 {data.get('banco') or 'não informado'}\n"
            f"📝 {data.get('descricao')}\n"
            f"💰 R$ {data.get('valor')}\n\n"
            f"✅ Salvo na planilha!"
        )
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

    except Exception as e:
        logger.error(f"Erro no lançamento: {e}")
        await update.message.reply_text(f"❌ Erro ao registrar: {str(e)}")


async def handle_resumo(update: Update):
    await update.message.reply_text("🔍 Buscando dados da planilha...")
    try:
        s = build_financial_summary()
        now = datetime.now()
        tab = f"{MONTH_NAMES[now.month]} - {now.year}"

        saldo_emoji = "🟢" if s["saldo"] >= 0 else "🔴"

        reply = (
            f"📊 *Resumo — {tab}*\n\n"
            f"📥 Entradas: R$ {s['entradas']:.2f}\n"
            f"📤 Saídas: R$ {s['saidas']:.2f}\n"
            f"{saldo_emoji} Saldo: R$ {s['saldo']:.2f}\n"
            f"📋 Lançamentos: {s['total']}"
        )
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar resumo: {str(e)}")


async def handle_saldo_banco(update: Update):
    await update.message.reply_text("🔍 Buscando saldo por banco...")
    try:
        s = build_financial_summary()
        lines = ["💰 *Saldo por banco:*\n"]
        for banco, vals in s["por_banco"].items():
            if banco:
                saldo_b = vals["entradas"] - vals["saidas"]
                emoji = "🟢" if saldo_b >= 0 else "🔴"
                lines.append(f"{emoji} *{banco}*: R$ {saldo_b:.2f}")
        if len(lines) == 1:
            lines.append("Nenhum banco registrado ainda.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def handle_maiores_gastos(update: Update):
    await update.message.reply_text("🔍 Buscando maiores gastos...")
    try:
        s = build_financial_summary()
        top = sorted(s["por_categoria"].items(), key=lambda x: x[1], reverse=True)[:5]
        lines = ["📈 *Top 5 maiores gastos:*\n"]
        for i, (cat, val) in enumerate(top, 1):
            lines.append(f"{i}. {cat.title()}: R$ {val:.2f}")
        if len(lines) == 1:
            lines.append("Nenhum lançamento encontrado.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {str(e)}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "🎤 Áudio recebido! Por enquanto só processo texto.\nEscreva o lançamento.",
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
