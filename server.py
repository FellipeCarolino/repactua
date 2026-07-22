"""
Backend da Calculadora de Superendividamento — versão SaaS.

Recursos:
- Leitura inteligente de documentos (holerite e contratos) via IA de visão da Claude.
- Contas de assinantes (advogados): cadastro, login, logout.
- Controle de assinatura (trial / ativo / inativo) e contador de consultas por mês.
- Base para integração de pagamento (Asaas) via webhook.

A chave de API da Anthropic fica só no servidor (ANTHROPIC_API_KEY) e nunca vai ao navegador.

Variáveis de ambiente:
- ANTHROPIC_API_KEY : chave da API da Anthropic (obrigatória para a IA).
- SECRET_KEY        : segredo das sessões de login (defina em produção).
- DATABASE_URL      : banco PostgreSQL (Railway injeta). Sem ela, usa SQLite local.
- ADMIN_EMAIL       : e-mail que vira administrador automaticamente.
- ASAAS_WEBHOOK_TOKEN : token simples para validar o webhook do Asaas (opcional).
"""

import base64
import io
import json
import os
import urllib.request
import urllib.error
import csv
import secrets
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, request, jsonify, send_from_directory, redirect, url_for, Response, session
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic

# ============================================================
# Configuração
# ============================================================
MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
MAX_PDF_PAGES = 12

LIMITE_POR_STATUS = {"ativo": 50, "trial": 3, "inativo": 0}

# --- Pagamento (Asaas) ---
ASAAS_API_KEY = os.environ.get("ASAAS_API_KEY", "")
ASAAS_BASE_URL = os.environ.get("ASAAS_BASE_URL", "https://api.asaas.com/v3").rstrip("/")
ASAAS_WEBHOOK_TOKEN = os.environ.get("ASAAS_WEBHOOK_TOKEN", "")
PLANO_VALOR = float(os.environ.get("PLANO_VALOR", "129.90"))
PLANO_DESC = "Assinatura Repactua — plano Profissional (50 consultas/mês)"

# --- Planos (2 opções) ---
PLANOS = {
    "individual": {
        "nome": "Individual",
        "valor": float(os.environ.get("PLANO_VALOR_IND", "129.90")),
        "max_membros": 1,
        "pool": 50,
        "desc": "Assinatura Repactua — Individual (1 acesso · 50 consultas/mês)",
        "resumo": "1 acesso · 50 consultas de IA por mês",
    },
    "escritorio": {
        "nome": "Escritório",
        "valor": float(os.environ.get("PLANO_VALOR_ESC", "229.90")),
        "max_membros": 5,
        "pool": 250,
        "desc": "Assinatura Repactua — Escritório (até 5 acessos · pool de 250 consultas/mês)",
        "resumo": "Até 5 acessos · pool de 250 consultas/mês (você distribui)",
    },
}


def valor_cobranca(plano):
    """Valor a cobrar. PLANO_VALOR (se definido) sobrepõe tudo — usado para testes baratos."""
    teste = os.environ.get("PLANO_VALOR")
    if teste:
        return float(teste)
    return PLANOS.get(plano, PLANOS["individual"])["valor"]

# --- Nota fiscal automática (NFS-e via Asaas) ---
NF_AUTO = os.environ.get("NF_AUTO", "0") == "1"
NF_SERVICO_ID = os.environ.get("NF_SERVICO_ID", "")           # ID do serviço registrado no Asaas
NF_SERVICO_CODIGO = os.environ.get("NF_SERVICO_CODIGO", "")   # código do serviço municipal
NF_SERVICO_NOME = os.environ.get("NF_SERVICO_NOME", "")       # descrição do serviço
NF_ISS = float(os.environ.get("NF_ISS", "0") or 0)            # alíquota de ISS (%)
NF_RETER_ISS = os.environ.get("NF_RETER_ISS", "0") == "1"
NF_DEDUCOES = float(os.environ.get("NF_DEDUCOES", "0") or 0)
NF_OBSERVACOES = os.environ.get("NF_OBSERVACOES", "")
NF_QUANDO = os.environ.get("NF_QUANDO", "ON_PAYMENT_CONFIRMATION")

# --- E-mail (recuperação de senha + alertas) ---
# Preferência: BREVO_API_KEY (API HTTPS — Railway bloqueia SMTP tradicional).
# Fallback: SMTP clássico (SMTP_HOST etc.), útil fora do Railway.
BREVO_API_KEY = (os.environ.get("BREVO_API_KEY") or "").strip()
SMTP_HOST = (os.environ.get("SMTP_HOST") or "").strip()
SMTP_PORT = int((os.environ.get("SMTP_PORT") or "587").strip() or 587)
SMTP_USER = (os.environ.get("SMTP_USER") or "").strip()
SMTP_PASS = (os.environ.get("SMTP_PASS") or "").strip()
SMTP_FROM = (os.environ.get("SMTP_FROM") or SMTP_USER).strip()
ALERTA_EMAIL = (os.environ.get("ALERTA_EMAIL") or "").strip()  # destino dos avisos (padrão: ADMIN_EMAIL)


def email_ativo():
    return bool(BREVO_API_KEY or SMTP_HOST)


def _enviar_email_impl(assunto, corpo, para=None):
    """Envia e levanta exceção em caso de erro (para diagnóstico)."""
    para = para or ALERTA_EMAIL or ADMIN_EMAIL
    if BREVO_API_KEY:
        payload = {
            "sender": {"name": "Repactua", "email": SMTP_FROM or ALERTA_EMAIL or ADMIN_EMAIL},
            "to": [{"email": para}],
            "subject": assunto,
            "textContent": corpo,
        }
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Brevo {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}")
    if not SMTP_HOST:
        raise RuntimeError("Nenhum provedor de e-mail configurado (BREVO_API_KEY ou SMTP_HOST).")
    msg = MIMEText(corpo, "plain", "utf-8")
    msg["Subject"] = assunto
    msg["From"] = SMTP_FROM
    msg["To"] = para
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


def _enviar_email(assunto, corpo, para=None):
    """Envia e-mail. Silencioso em caso de erro (não travar fluxos de negócio)."""
    try:
        return _enviar_email_impl(assunto, corpo, para)
    except Exception:
        return False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _carregar_env():
    caminho = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(caminho):
        return
    with open(caminho, "r", encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env()

ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "fellipe.carolino18@gmail.com").lower()

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-este-segredo-em-producao")

db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "calculadora.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "pagina_login"

client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente


# ============================================================
# Modelo de dados
# ============================================================
class Escritorio(db.Model):
    """Conta-mãe que assina o plano. Reúne 1 (Individual) ou até 5 (Escritório) usuários."""
    __tablename__ = "escritorio"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255))
    plano = db.Column(db.String(20), default="individual")  # individual | escritorio
    status = db.Column(db.String(20), default="trial")      # trial | ativo | inativo
    asaas_customer_id = db.Column(db.String(120))
    asaas_subscription_id = db.Column(db.String(120))  # p/ trocar de plano (upgrade)
    max_membros = db.Column(db.Integer, default=1)
    creditos_total = db.Column(db.Integer, default=50)  # pool de consultas/mês do escritório
    timbre = db.Column(db.Text)  # JSON do timbre da petição (compartilhado pelo escritório)
    telefone = db.Column(db.String(30))
    cidade = db.Column(db.String(120))
    uf = db.Column(db.String(4))
    acesso_ate = db.Column(db.Date)  # validade do período pago (NULL = sem expiração/cortesia)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    usuarios = db.relationship("User", backref="org", lazy=True,
                               foreign_keys="User.org_id")

    @property
    def total_membros(self):
        return len(self.usuarios or [])

    @property
    def vagas_restantes(self):
        return max((self.max_membros or 1) - self.total_membros, 0)

    @property
    def cota_distribuida(self):
        return sum((u.cota_mensal or 0) for u in (self.usuarios or []))

    @property
    def cota_disponivel(self):
        return max((self.creditos_total or 0) - self.cota_distribuida, 0)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    senha_hash = db.Column(db.String(255), nullable=False)
    nome = db.Column(db.String(255))
    escritorio = db.Column(db.String(255))
    oab = db.Column(db.String(60))
    status = db.Column(db.String(20), default="trial")  # legado — fonte de verdade é o Escritório
    is_admin = db.Column(db.Boolean, default=False)
    usage_mes = db.Column(db.String(7))   # "AAAA-MM"
    usage_contagem = db.Column(db.Integer, default=0)
    asaas_customer_id = db.Column(db.String(120))  # legado — migrado para o Escritório
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    org_id = db.Column(db.Integer, db.ForeignKey("escritorio.id"))
    papel = db.Column(db.String(20), default="dono")  # dono | membro
    cota_mensal = db.Column(db.Integer, default=50)    # créditos atribuídos a este usuário
    reset_token = db.Column(db.String(80))             # recuperação de senha
    reset_expira = db.Column(db.DateTime)

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha, method="pbkdf2:sha256")

    def conferir_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    @property
    def status_efetivo(self):
        """Status que vale para o usuário = status do escritório (fallback no legado)."""
        if self.org:
            st = self.org.status
            # período pago expirou (ex.: assinatura cancelada) → perde o acesso
            if st == "ativo" and self.org.acesso_ate and self.org.acesso_ate < date.today():
                return "inativo"
            return st
        return self.status or "trial"

    @property
    def limite_mensal(self):
        st = self.status_efetivo
        if st == "ativo":
            return self.cota_mensal if self.cota_mensal is not None else 50
        return LIMITE_POR_STATUS.get(st, 0)  # trial=3, inativo=0

    def _mes_atual(self):
        return datetime.utcnow().strftime("%Y-%m")

    def consultas_restantes(self):
        if self.usage_mes != self._mes_atual():
            return self.limite_mensal
        return max(self.limite_mensal - (self.usage_contagem or 0), 0)

    def pode_consultar(self):
        return self.status_efetivo in ("trial", "ativo") and self.consultas_restantes() > 0

    def registrar_consulta(self):
        mes = self._mes_atual()
        if self.usage_mes != mes:
            self.usage_mes = mes
            self.usage_contagem = 0
        self.usage_contagem = (self.usage_contagem or 0) + 1
        db.session.commit()


class LogAdmin(db.Model):
    """Auditoria das ações administrativas."""
    __tablename__ = "log_admin"
    id = db.Column(db.Integer, primary_key=True)
    quando = db.Column(db.DateTime, default=datetime.utcnow)
    admin_email = db.Column(db.String(255))
    acao = db.Column(db.String(255))
    alvo = db.Column(db.String(255))


def _log_admin(acao, alvo=""):
    try:
        email = session.get("admin_email") or (
            current_user.email if current_user.is_authenticated else "?")
        db.session.add(LogAdmin(admin_email=email, acao=acao, alvo=str(alvo)[:255]))
        db.session.commit()
    except Exception:
        db.session.rollback()


class Caso(db.Model):
    """Caso salvo de análise — fica no servidor, compartilhado pelo escritório."""
    __tablename__ = "caso"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("escritorio.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    nome = db.Column(db.String(255))
    payload = db.Column(db.Text)  # JSON: {"dados": {...}, "dividas": [...]}
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    autor = db.relationship("User", foreign_keys=[user_id])


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _migrar_schema():
    """Cria tabelas e adiciona colunas novas (produção tem tabela 'user' antiga)."""
    from sqlalchemy import text
    db.create_all()
    for ddl in (
        'ALTER TABLE "user" ADD COLUMN org_id INTEGER',
        'ALTER TABLE "user" ADD COLUMN papel VARCHAR(20)',
        'ALTER TABLE "user" ADD COLUMN cota_mensal INTEGER',
        'ALTER TABLE "user" ADD COLUMN reset_token VARCHAR(80)',
        'ALTER TABLE "user" ADD COLUMN reset_expira TIMESTAMP',
        'ALTER TABLE escritorio ADD COLUMN creditos_total INTEGER',
        'ALTER TABLE escritorio ADD COLUMN timbre TEXT',
        'ALTER TABLE escritorio ADD COLUMN asaas_subscription_id VARCHAR(120)',
        'ALTER TABLE escritorio ADD COLUMN telefone VARCHAR(30)',
        'ALTER TABLE escritorio ADD COLUMN cidade VARCHAR(120)',
        'ALTER TABLE escritorio ADD COLUMN uf VARCHAR(4)',
        'ALTER TABLE escritorio ADD COLUMN acesso_ate DATE',
    ):
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()
    # Cada usuário ainda sem escritório vira dono de um escritório Individual
    try:
        orfaos = User.query.filter((User.org_id.is_(None))).all()
        for u in orfaos:
            org = Escritorio(
                nome=(u.escritorio or u.nome or u.email),
                plano="individual",
                status=(u.status or "trial"),
                asaas_customer_id=u.asaas_customer_id,
                max_membros=1,
                creditos_total=50,
            )
            db.session.add(org)
            db.session.flush()
            u.org_id = org.id
            u.papel = "dono"
            u.cota_mensal = 50
        if orfaos:
            db.session.commit()
    except Exception:
        db.session.rollback()
    # Backfill de cotas/pool faltantes
    try:
        for u in User.query.filter(User.cota_mensal.is_(None)).all():
            u.cota_mensal = 50
        for o in Escritorio.query.filter(Escritorio.creditos_total.is_(None)).all():
            o.creditos_total = PLANOS.get(o.plano, PLANOS["individual"])["pool"]
        db.session.commit()
    except Exception:
        db.session.rollback()


with app.app_context():
    _migrar_schema()


# ============================================================
# Extração por IA (holerite / contrato)
# ============================================================
TIPOS_DIVIDA = [
    "cartao", "cheque", "emprestimo", "consignado",
    "financiamento_imovel", "financiamento_veiculo", "aluguel", "alimentos",
    "condominio", "fiscal", "energia", "saude", "educacao", "outro",
]

SCHEMA_HOLERITE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "nome": {"type": ["string", "null"]},
        "renda_bruta": {"type": ["number", "null"]},
        "inss": {"type": ["number", "null"]},
        "irrf": {"type": ["number", "null"]},
        "pensao": {"type": ["number", "null"]},
        "outros_descontos": {"type": ["number", "null"]},
        "consignados_total": {"type": ["number", "null"]},
        "consignados": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"credor": {"type": ["string", "null"]}, "parcela": {"type": ["number", "null"]}},
                "required": ["credor", "parcela"],
            },
        },
        "observacoes": {"type": ["string", "null"]},
    },
    "required": ["nome", "renda_bruta", "inss", "irrf", "pensao",
                 "outros_descontos", "consignados_total", "consignados", "observacoes"],
}

SCHEMA_CONTRATO = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "credor": {"type": ["string", "null"]},
        "tipo": {"type": "string", "enum": TIPOS_DIVIDA},
        "saldo_devedor": {"type": ["number", "null"]},
        "parcela_mensal": {"type": ["number", "null"]},
        "parcelas_contratadas": {"type": ["integer", "null"]},
        "parcelas_pagas": {"type": ["integer", "null"]},
        "em_folha": {"type": ["boolean", "null"]},
        "taxa_juros": {"type": ["string", "null"]},
        "observacoes": {"type": ["string", "null"]},
    },
    "required": ["credor", "tipo", "saldo_devedor", "parcela_mensal",
                 "parcelas_contratadas", "parcelas_pagas", "em_folha", "taxa_juros", "observacoes"],
}

PROMPT_HOLERITE = (
    "Você é um analista jurídico-financeiro especializado em folhas de pagamento brasileiras "
    "(CLT, servidores públicos e militares). "
    "Leia o documento e extraia os dados para análise de superendividamento. "
    "Separe proventos de descontos. 'renda_bruta' = soma dos proventos brutos. "
    "'inss' = contribuição previdenciária obrigatória: INSS, PSS, ou, em contracheques militares, "
    "a pensão militar / contribuição para a pensão militar. "
    "'irrf' = imposto de renda efetivamente RETIDO na folha; se não houver retenção "
    "(ex.: isento por moléstia grave ou faixa de isenção), retorne 0 — NUNCA estime ou calcule. "
    "'outros_descontos' = soma dos demais descontos obrigatórios/compulsórios que não sejam INSS/PSS, "
    "IRRF, pensão alimentícia ou consignados — ex.: FUSEX/fundo de saúde militar, assistência médica "
    "obrigatória, contribuição sindical, coparticipação de plano de saúde, taxa de ocupação de imóvel funcional. "
    "'consignados' = empréstimos/cartões consignados e financiamentos descontados em folha (liste credor e parcela). "
    "Em 'observacoes', registre se a folha indica isenção de IR e descontos relevantes que você classificou em 'outros_descontos'. "
    "Valores em reais como número decimal (ex.: 5800.50), sem 'R$' nem separador de milhar. "
    "Se um campo não existir, retorne null. Não invente valores."
)

PROMPT_CONTRATO = (
    "Você é um analista jurídico-financeiro especializado em contratos de crédito brasileiros. "
    "Leia o contrato e extraia os dados da dívida. 'tipo' = o valor da lista que melhor descreve. "
    "'saldo_devedor' = total em aberto. 'parcela_mensal' = prestação mensal. "
    "'em_folha' = true se consignado. Valores em reais como número decimal, sem 'R$' nem separador de milhar. "
    "Em 'observacoes', registre indícios de abusividade (juros acima do mercado, venda casada de seguro, "
    "tarifas não pactuadas, anatocismo, reendividamento). Se um campo não existir, retorne null. Não invente."
)


def _limitar_paginas_pdf(raw):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return raw
    try:
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) <= MAX_PDF_PAGES:
            return raw
        writer = PdfWriter()
        for i in range(MAX_PDF_PAGES):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return raw


def _content_block_for_upload(file_storage):
    raw = file_storage.read()
    if not raw:
        raise ValueError("Arquivo vazio.")
    filename = (file_storage.filename or "").lower()
    mimetype = (file_storage.mimetype or "").lower()
    if filename.endswith(".pdf") or "pdf" in mimetype:
        raw = _limitar_paginas_pdf(raw)
        data = base64.standard_b64encode(raw).decode("utf-8")
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    data = base64.standard_b64encode(raw).decode("utf-8")
    if filename.endswith(".png") or "png" in mimetype:
        media = "image/png"
    elif filename.endswith(".webp") or "webp" in mimetype:
        media = "image/webp"
    elif filename.endswith(".gif") or "gif" in mimetype:
        media = "image/gif"
    else:
        media = "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def _extrair(prompt, schema, file_storage):
    bloco = _content_block_for_upload(file_storage)
    response = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": [bloco, {"type": "text", "text": prompt}]}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("A análise foi recusada por política de segurança.")
    texto = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(texto)


def _checar_uso():
    """Retorna (ok, mensagem_erro_ou_None) para uso da IA pelo usuário atual."""
    if current_user.status_efetivo == "inativo":
        return False, "Sua assinatura está inativa. Regularize o pagamento para usar a leitura por IA."
    if current_user.consultas_restantes() <= 0:
        msg = f"Você atingiu o limite de {current_user.limite_mensal} consultas neste mês."
        org = current_user.org
        if current_user.papel == "dono" and org and org.plano == "individual":
            msg += " Faça upgrade para o plano Escritório em 'Minha conta' e tenha um pool de 250 consultas/mês."
        return False, msg
    return True, None


# ============================================================
# Páginas (HTML simples, embutido)
# ============================================================
# Logo Repactua reutilizável (selo azul-marinho + setas convergindo douradas)
def logo_repactua(tam=34):
    # Selo azul-marinho com duas setas convergindo (→ • ←) — conceito "acordo"
    return (f'<svg width="{tam}" height="{tam}" viewBox="0 0 80 80" '
            'style="vertical-align:middle;flex:none" aria-hidden="true">'
            '<rect width="80" height="80" rx="18" fill="#1a3a5c"/>'
            '<line x1="15" y1="40" x2="34" y2="40" stroke="#c8960c" stroke-width="6" stroke-linecap="round"/>'
            '<polyline points="27,30 37,40 27,50" fill="none" stroke="#c8960c" '
            'stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
            '<line x1="65" y1="40" x2="46" y2="40" stroke="#c8960c" stroke-width="6" stroke-linecap="round"/>'
            '<polyline points="53,30 43,40 53,50" fill="none" stroke="#c8960c" '
            'stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="40" cy="40" r="3.5" fill="#fff"/></svg>')


def _pagina_auth(titulo, corpo):
    return Response(PAGINA_BASE.replace("{{TITULO}}", titulo).replace("{{CORPO}}", corpo), mimetype="text/html")


PAGINA_BASE = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{TITULO}} · Repactua</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'%3E%3Crect width='80' height='80' rx='18' fill='%231a3a5c'/%3E%3Cpolyline points='26,22 38,40 26,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpolyline points='54,22 42,40 54,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='40' cy='40' r='3.5' fill='%23c8960c'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:linear-gradient(135deg,#1a3a5c,#2c5f8a);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;color:#1c2b3a}
.card{background:#fff;border-radius:14px;box-shadow:0 12px 48px rgba(0,0,0,.25);width:100%;max-width:420px;overflow:hidden}
.top{background:#1a3a5c;color:#fff;padding:24px 28px;text-align:center}
.top .logo{font-size:32px}
.top h1{font-size:1.2rem;margin-top:6px}
.top p{font-size:.8rem;opacity:.8;margin-top:2px}
.body{padding:28px}
.body h2{font-size:1.1rem;color:#1a3a5c;margin-bottom:4px}
.body .sub{font-size:.85rem;color:#5a6a7a;margin-bottom:18px}
label{display:block;font-size:.8rem;font-weight:600;color:#5a6a7a;margin:12px 0 5px;text-transform:uppercase;letter-spacing:.4px}
input{width:100%;padding:11px 14px;border:1.5px solid #d0d7e2;border-radius:8px;font-size:.95rem;background:#fafbfd;outline:none}
input:focus{border-color:#2c5f8a;background:#fff}
.btn{width:100%;padding:13px;border:none;border-radius:8px;background:#c8960c;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;margin-top:20px}
.btn:hover{background:#f0b429}
.link{text-align:center;margin-top:16px;font-size:.88rem;color:#5a6a7a}
.link a{color:#2c5f8a;font-weight:600;text-decoration:none}
.erro{background:#fdecea;color:#7a2218;border:1px solid #e8a49a;border-radius:8px;padding:10px 14px;font-size:.85rem;margin-bottom:14px}
.ok{background:#e9f7ee;color:#1b5e20;border:1px solid #7ec891;border-radius:8px;padding:10px 14px;font-size:.85rem;margin-bottom:14px}
.planos{display:flex;gap:10px;margin-top:6px}
.plano{flex:1;border:1.5px solid #d0d7e2;border-radius:10px;padding:12px;cursor:pointer;background:#fafbfd;text-transform:none;letter-spacing:0;margin:0;display:block}
.plano.sel{border-color:#c8960c;background:#fffaf0;box-shadow:0 0 0 2px rgba(200,150,12,.15)}
.plano input{display:none}
.plano b{display:block;color:#1a3a5c;font-size:.95rem}
.plano span{display:block;color:#c8960c;font-weight:700;font-size:1rem;margin:2px 0}
.plano small{display:block;color:#5a6a7a;font-size:.72rem;line-height:1.3}
</style></head><body><div class="card">
<div class="top">
<svg width="48" height="48" viewBox="0 0 80 80" aria-hidden="true" style="display:block;margin:0 auto 4px">
<line x1="14" y1="40" x2="33" y2="40" stroke="#e9b53a" stroke-width="7" stroke-linecap="round"/>
<polyline points="25,30 34,40 25,50" fill="none" stroke="#e9b53a" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
<line x1="66" y1="40" x2="47" y2="40" stroke="#e9b53a" stroke-width="7" stroke-linecap="round"/>
<polyline points="55,30 46,40 55,50" fill="none" stroke="#e9b53a" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="40" cy="40" r="4" fill="#e9b53a"/></svg>
<h1>Repactua</h1><p>Análise de superendividamento · para advogados</p></div>
<div class="body">{{CORPO}}</div></div></body></html>"""


def login_required_page(f):
    """Como login_required, mas redireciona páginas (não-API) para /login."""
    @wraps(f)
    @login_required
    def wrap(*a, **k):
        return f(*a, **k)
    return wrap


# ============================================================
# Rotas — Autenticação
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def pagina_login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    erro = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        user = User.query.filter_by(email=email).first()
        if user and user.conferir_senha(senha):
            login_user(user, remember=True)
            return redirect(url_for("index"))
        erro = '<div class="erro">E-mail ou senha incorretos.</div>'
    corpo = f"""<h2>Entrar</h2><div class="sub">Acesse sua conta para usar a calculadora.</div>{erro}
    <form method="post">
      <label>E-mail</label><input type="email" name="email" required placeholder="voce@escritorio.adv.br">
      <label>Senha</label><input type="password" name="senha" required placeholder="••••••••">
      <button class="btn" type="submit">Entrar</button>
    </form>
    <div class="link"><a href="/esqueci-senha">Esqueci minha senha</a></div>
    <div class="link">Ainda não tem conta? <a href="/signup">Criar conta</a></div>"""
    return _pagina_auth("Entrar", corpo)


@app.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    """Recuperação de senha: envia link por e-mail com token de 1 hora."""
    msg = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user and email_ativo():
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_expira = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()
            link = f"https://repactua.com.br/redefinir-senha?t={token}"
            _enviar_email(
                "Repactua — redefinição de senha",
                f"Olá!\n\nRecebemos um pedido para redefinir a senha da sua conta no Repactua.\n\n"
                f"Para criar uma nova senha, acesse o link abaixo (válido por 1 hora):\n{link}\n\n"
                f"Se você não pediu a redefinição, ignore este e-mail — sua senha continua a mesma.\n\n"
                f"Equipe Repactua · repactua.com.br",
                para=email)
        if email_ativo():
            # resposta genérica: não revela se o e-mail existe (evita enumeração de contas)
            msg = ('<div class="ok">Se este e-mail estiver cadastrado, enviamos um link de '
                   'redefinição. Verifique a caixa de entrada e o spam (válido por 1 hora).</div>')
        else:
            msg = ('<div class="erro">O envio automático de e-mail ainda não está configurado. '
                   'Entre em contato com o suporte pelo e-mail <b>' + (ALERTA_EMAIL or ADMIN_EMAIL) +
                   '</b> que redefinimos sua senha rapidinho.</div>')
    corpo = f"""<h2>Recuperar senha</h2>
    <div class="sub">Informe o e-mail da sua conta e enviaremos um link para criar uma nova senha.</div>{msg}
    <form method="post">
      <label>E-mail</label><input type="email" name="email" required placeholder="voce@escritorio.adv.br">
      <button class="btn" type="submit">Enviar link de redefinição</button>
    </form>
    <div class="link"><a href="/login">← Voltar ao login</a></div>"""
    return _pagina_auth("Recuperar senha", corpo)


@app.route("/redefinir-senha", methods=["GET", "POST"])
def redefinir_senha():
    """Define a nova senha a partir do token recebido por e-mail."""
    token = (request.values.get("t") or "").strip()
    user = User.query.filter_by(reset_token=token).first() if token else None
    valido = bool(user and user.reset_expira and user.reset_expira > datetime.utcnow())
    if not valido:
        corpo = """<h2>Link inválido ou expirado</h2>
        <div class="sub">O link de redefinição não é válido ou passou de 1 hora.</div>
        <div class="link"><a href="/esqueci-senha">Pedir um novo link</a> · <a href="/login">Voltar ao login</a></div>"""
        return _pagina_auth("Redefinir senha", corpo)
    if request.method == "POST":
        senha = request.form.get("senha") or ""
        confirma = request.form.get("confirma") or ""
        if len(senha) < 6:
            msg = '<div class="erro">A senha deve ter no mínimo 6 caracteres.</div>'
        elif senha != confirma:
            msg = '<div class="erro">As senhas não conferem.</div>'
        else:
            user.set_senha(senha)
            user.reset_token = None
            user.reset_expira = None
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("index"))
        corpo = f"""<h2>Criar nova senha</h2><div class="sub">Conta: {user.email}</div>{msg}
        <form method="post"><input type="hidden" name="t" value="{token}">
          <label>Nova senha</label><input type="password" name="senha" required placeholder="mínimo 6 caracteres">
          <label>Confirmar senha</label><input type="password" name="confirma" required placeholder="repita a senha">
          <button class="btn" type="submit">Salvar nova senha</button>
        </form>"""
        return _pagina_auth("Redefinir senha", corpo)
    corpo = f"""<h2>Criar nova senha</h2><div class="sub">Conta: {user.email}</div>
    <form method="post"><input type="hidden" name="t" value="{token}">
      <label>Nova senha</label><input type="password" name="senha" required placeholder="mínimo 6 caracteres">
      <label>Confirmar senha</label><input type="password" name="confirma" required placeholder="repita a senha">
      <button class="btn" type="submit">Salvar nova senha</button>
    </form>"""
    return _pagina_auth("Redefinir senha", corpo)


@app.route("/signup", methods=["GET", "POST"])
def pagina_signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    msg = ""
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        escritorio = (request.form.get("escritorio") or "").strip()
        if not email or len(senha) < 6:
            msg = '<div class="erro">Informe um e-mail válido e senha de no mínimo 6 caracteres.</div>'
        elif User.query.filter_by(email=email).first():
            msg = '<div class="erro">Já existe uma conta com este e-mail.</div>'
        else:
            status_inicial = "ativo" if email == ADMIN_EMAIL else "trial"
            org = Escritorio(nome=(escritorio or nome or email), plano="individual",
                             status=status_inicial, max_membros=1)
            db.session.add(org)
            db.session.flush()
            user = User(email=email, nome=nome, escritorio=escritorio, status=status_inicial,
                        org_id=org.id, papel="dono")
            user.set_senha(senha)
            if email == ADMIN_EMAIL:
                user.is_admin = True
            db.session.add(user)
            db.session.commit()
            _enviar_email("🆕 Repactua: novo cadastro",
                          f"Nova conta criada: {nome or email} <{email}>"
                          f"{' · escritório: ' + escritorio if escritorio else ''}")
            login_user(user, remember=True)
            return redirect(url_for("index"))
    corpo = f"""<h2>Criar conta</h2><div class="sub">Comece com algumas consultas de teste gratuitas.</div>{msg}
    <form method="post">
      <label>Nome</label><input name="nome" placeholder="Seu nome">
      <label>Escritório (opcional)</label><input name="escritorio" placeholder="Nome do escritório">
      <label>E-mail</label><input type="email" name="email" required placeholder="voce@escritorio.adv.br">
      <label>Senha</label><input type="password" name="senha" required placeholder="mínimo 6 caracteres">
      <button class="btn" type="submit">Criar conta</button>
    </form>
    <div class="link">Já tem conta? <a href="/login">Entrar</a></div>"""
    return _pagina_auth("Criar conta", corpo)


@app.route("/logout")
def pagina_logout():
    logout_user()
    return redirect(url_for("pagina_login"))


# ============================================================
# Rotas — Aplicação
# ============================================================
@app.route("/")
def index():
    if not current_user.is_authenticated:
        return send_from_directory(BASE_DIR, "landing.html")
    return render_home()


@app.route("/calculadora")
@login_required
def calculadora():
    return send_from_directory(BASE_DIR, "index.html")


def render_home():
    u = current_user
    org = u.org
    plano = org.plano if org else "individual"
    plano_nome = PLANOS.get(plano, {}).get("nome", "Individual")
    usados = (u.usage_contagem or 0) if u.usage_mes == datetime.utcnow().strftime("%Y-%m") else 0
    cota = u.limite_mensal or 0
    restantes = u.consultas_restantes()
    pct = int(usados * 100 / cota) if cota else 0
    primeiro_nome = (u.nome or u.email or "").split(" ")[0].split("@")[0].capitalize()
    saudacao = _saudacao_hora()
    hoje = datetime.utcnow().strftime("%d/%m/%Y")

    # casos do escritório
    qbase = Caso.query.filter_by(org_id=u.org_id) if u.org_id else Caso.query.filter_by(user_id=u.id)
    total_casos = qbase.count()
    recentes = qbase.order_by(Caso.atualizado_em.desc()).limit(6).all()

    # --- Indicadores (metric cards) ---
    metricas = [
        ("Consultas restantes", f"{restantes}", f"de {cota} este mês", "/calculadora", ""),
        ("Casos salvos", f"{total_casos}", "no escritório" if plano == "escritorio" else "na sua conta",
         "/calculadora?casos=1", ""),
        ("Plano", plano_nome, ("ativo" if u.status_efetivo == "ativo" else u.status_efetivo), "/conta", ""),
    ]
    if plano == "escritorio" and org:
        metricas.append(("Equipe", f"{org.total_membros}/{org.max_membros}", "acessos em uso",
                         "/conta", ""))
    cards_metricas = ""
    for label, valor, sub, href, _ in metricas:
        cards_metricas += (f'<a class="metric" href="{href}">'
                           f'<div class="m-label">{label}</div>'
                           f'<div class="m-valor">{valor}</div>'
                           f'<div class="m-sub">{sub}</div></a>')

    # --- Casos recentes ---
    if recentes:
        itens = ""
        for c in recentes:
            quando = (c.atualizado_em or c.criado_em or datetime.utcnow()).strftime("%d/%m/%Y")
            autor = (c.autor.nome or c.autor.email) if c.autor else ""
            sub_autor = f' · {autor}' if (plano == "escritorio" and autor) else ""
            itens += (f'<a class="recente" href="/calculadora?caso={c.id}">'
                      f'<span class="r-nome">{(c.nome or "Caso sem nome")}</span>'
                      f'<span class="r-data">{quando}{sub_autor}</span></a>')
        bloco_recentes = (f'<div class="card"><div class="card-h"><h2>Casos recentes</h2>'
                          f'<a class="vertodos" href="/calculadora?casos=1">Ver todos →</a></div>{itens}</div>')
    else:
        bloco_recentes = ('<div class="card"><h2>Casos recentes</h2>'
                          '<div class="vazio">Você ainda não salvou nenhum caso. Comece uma <a href="/calculadora">nova análise</a>!</div></div>')

    card_equipe = ""
    if u.papel == "dono" and plano == "escritorio":
        card_equipe = ('<a class="atalho" href="/conta">'
                       '<div class="a-ico">👥</div><div class="a-nome">Equipe</div>'
                       '<div class="a-sub">Membros e créditos</div></a>')
    alerta = ""
    if u.status_efetivo != "ativo":
        alerta = ('<a class="alerta" href="/assinar">⚠️ Sua conta não está ativa. '
                  'Clique para assinar e liberar as consultas →</a>')

    corpo = f"""<div class="topo">
      <div class="marca">{logo_repactua(36)} <div>Repactua<small>Análise de superendividamento</small></div></div>
      <div class="links"><a href="/conta">Minha conta</a>{' · <a href="/admin">Admin</a>' if u.is_admin else ''} · <a href="/logout">Sair</a></div>
    </div>

    <div class="hero">
      <div><h1>{saudacao}, {primeiro_nome} 👋</h1>
      <div class="hero-sub">Bem-vindo(a) ao seu painel · {hoje}</div></div>
      <a class="btn-novo" href="/calculadora">+ Nova análise</a>
    </div>
    {alerta}

    <div class="metrics">{cards_metricas}</div>
    <div class="bar-wrap"><div class="bar"><i style="width:{min(pct,100)}%"></i></div>
      <span class="bar-leg">{usados} de {cota} consultas usadas este mês</span></div>

    <div class="grid2">
      <div class="card">
        <h2>Atalhos</h2>
        <div class="atalhos">
          <a class="atalho destaque" href="/calculadora">
            <div class="a-ico">🧮</div><div class="a-nome">Nova análise</div>
            <div class="a-sub">Calcular superendividamento</div>
          </a>
          <a class="atalho" href="/calculadora?casos=1">
            <div class="a-ico">🗂️</div><div class="a-nome">Meus casos</div>
            <div class="a-sub">Abrir análises salvas</div>
          </a>
          <a class="atalho" href="/conta">
            <div class="a-ico">⚙️</div><div class="a-nome">Minha conta</div>
            <div class="a-sub">Plano, senha e dados</div>
          </a>
          {card_equipe}
        </div>
      </div>
      {bloco_recentes}
    </div>"""
    return Response(PAGINA_HOME.replace("{{CORPO}}", corpo), mimetype="text/html")


def _saudacao_hora():
    h = (datetime.utcnow().hour - 3) % 24  # horário de Brasília aproximado
    if h < 12:
        return "Bom dia"
    if h < 18:
        return "Boa tarde"
    return "Boa noite"


@app.route("/api/me")
@login_required
def api_me():
    org = current_user.org
    plano = (org.plano if org else "individual")
    return jsonify({
        "nome": current_user.nome, "email": current_user.email,
        "status": current_user.status_efetivo, "is_admin": current_user.is_admin,
        "limite": current_user.limite_mensal,
        "restantes": current_user.consultas_restantes(),
        "plano": plano,
        "plano_nome": PLANOS.get(plano, {}).get("nome", "Individual"),
        "papel": current_user.papel or "dono",
        "membros": (org.total_membros if org else 1),
        "max_membros": (org.max_membros if org else 1),
    })


# ============================================================
# Casos salvos (no servidor, compartilhados pelo escritório)
# ============================================================
def _caso_to_dict(c, completo=True):
    d = {
        "id": c.id,
        "nomeCaso": c.nome or "Caso sem nome",
        "salvoEm": (c.atualizado_em or c.criado_em or datetime.utcnow()).isoformat(),
        "autor": (c.autor.nome or c.autor.email) if c.autor else "",
    }
    try:
        payload = json.loads(c.payload or "{}")
    except Exception:
        payload = {}
    if completo:
        d["dados"] = payload.get("dados", {})
        d["dividas"] = payload.get("dividas", [])
    else:
        d["n_dividas"] = len(payload.get("dividas", []) or [])
    return d


@app.route("/api/casos", methods=["GET"])
@login_required
def casos_listar():
    q = Caso.query
    if current_user.org_id:
        q = q.filter_by(org_id=current_user.org_id)
    else:
        q = q.filter_by(user_id=current_user.id)
    casos = q.order_by(Caso.atualizado_em.desc()).all()
    return jsonify({"ok": True, "casos": [_caso_to_dict(c, completo=True) for c in casos]})


@app.route("/api/casos", methods=["POST"])
@login_required
def casos_salvar():
    body = request.get_json(silent=True) or {}
    nome = (body.get("nomeCaso") or "Caso sem nome").strip()[:255]
    payload = json.dumps({"dados": body.get("dados", {}), "dividas": body.get("dividas", [])})
    caso = Caso(org_id=current_user.org_id, user_id=current_user.id, nome=nome, payload=payload)
    db.session.add(caso)
    db.session.commit()
    return jsonify({"ok": True, "id": caso.id})


@app.route("/api/casos/<int:cid>", methods=["DELETE"])
@login_required
def casos_excluir(cid):
    c = db.session.get(Caso, cid)
    if not c or (current_user.org_id and c.org_id != current_user.org_id) or \
       (not current_user.org_id and c.user_id != current_user.id):
        return jsonify({"ok": False, "erro": "Caso não encontrado."}), 404
    db.session.delete(c)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/timbre", methods=["GET"])
@login_required
def timbre_obter():
    org = current_user.org
    try:
        cfg = json.loads(org.timbre) if (org and org.timbre) else {}
    except Exception:
        cfg = {}
    return jsonify({"ok": True, "timbre": cfg, "pode_editar": (current_user.papel == "dono")})


@app.route("/api/timbre", methods=["POST"])
@login_required
def timbre_salvar():
    org = current_user.org
    if not org:
        return jsonify({"ok": False, "erro": "Sem escritório."}), 400
    if current_user.papel != "dono":
        return jsonify({"ok": False, "erro": "Apenas o dono do escritório pode editar o timbre."}), 403
    body = request.get_json(silent=True) or {}
    cfg = {k: body.get(k, "") for k in ("nome", "advogado", "oab", "contato", "logo")}
    org.timbre = json.dumps(cfg)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/extract-holerite", methods=["POST"])
@login_required
def extract_holerite():
    ok, erro = _checar_uso()
    if not ok:
        return jsonify({"ok": False, "erro": erro, "limite": True}), 402
    if "file" not in request.files:
        return jsonify({"ok": False, "erro": "Nenhum arquivo enviado."}), 400
    try:
        dados = _extrair(PROMPT_HOLERITE, SCHEMA_HOLERITE, request.files["file"])
        current_user.registrar_consulta()
        return jsonify({"ok": True, "dados": dados, "restantes": current_user.consultas_restantes()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/extract-contrato", methods=["POST"])
@login_required
def extract_contrato():
    ok, erro = _checar_uso()
    if not ok:
        return jsonify({"ok": False, "erro": erro, "limite": True}), 402
    if "file" not in request.files:
        return jsonify({"ok": False, "erro": "Nenhum arquivo enviado."}), 400
    try:
        dados = _extrair(PROMPT_CONTRATO, SCHEMA_CONTRATO, request.files["file"])
        current_user.registrar_consulta()
        return jsonify({"ok": True, "dados": dados, "restantes": current_user.consultas_restantes()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/health")
def health():
    try:
        dialect = db.engine.dialect.name  # "postgresql" (permanente) ou "sqlite" (temporário)
    except Exception:
        dialect = "?"
    try:
        total_contas = User.query.count()
    except Exception:
        total_contas = None
    return jsonify({
        "ok": True, "model": MODEL,
        "tem_chave": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "banco": dialect, "contas": total_contas,
        "preco_individual": valor_cobranca("individual"),
        "preco_escritorio": valor_cobranca("escritorio"),
        "modo_teste_preco": bool(os.environ.get("PLANO_VALOR")),
        "smtp_configurado": email_ativo(),
    })


# ============================================================
# Assinatura (Asaas) — cobrança recorrente
# ============================================================
def asaas(method, path, payload=None):
    """Chamada à API do Asaas. Levanta RuntimeError em caso de erro."""
    if not ASAAS_API_KEY:
        raise RuntimeError("Pagamento ainda não configurado (ASAAS_API_KEY ausente).")
    url = ASAAS_BASE_URL + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "access_token": ASAAS_API_KEY, "Content-Type": "application/json", "User-Agent": "Repactua",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Asaas {e.code}: {corpo}")


@app.route("/assinar", methods=["GET"])
@login_required
def assinar():
    if current_user.status_efetivo == "ativo":
        return redirect(url_for("index"))
    ind, esc = PLANOS["individual"], PLANOS["escritorio"]
    pv_ind = ('%.2f' % ind["valor"]).replace('.', ',')
    pv_esc = ('%.2f' % esc["valor"]).replace('.', ',')
    corpo = f"""<h2>Assinar o Repactua</h2>
    <div class="sub">Escolha seu plano. A conta é ativada automaticamente após o pagamento, e a <b>nota fiscal</b> é emitida.</div>
    <form method="post">
      <label>Plano</label>
      <div class="planos">
        <label class="plano sel">
          <input type="radio" name="plano" value="individual" checked>
          <div><b>Individual</b><span>R$ {pv_ind}/mês</span><small>{ind['resumo']}</small></div>
        </label>
        <label class="plano">
          <input type="radio" name="plano" value="escritorio">
          <div><b>Escritório</b><span>R$ {pv_esc}/mês</span><small>{esc['resumo']}</small></div>
        </label>
      </div>
      <label>Nome / Razão social</label><input name="nome" value="{(current_user.nome or '').replace('"','')}" required>
      <label>CPF ou CNPJ</label><input name="cpfCnpj" required placeholder="somente números">
      <label>Telefone / Celular</label><input name="telefone" placeholder="(DDD) número">
      <label>CEP</label><input name="cep" id="cep" required placeholder="somente números" maxlength="9">
      <div style="display:flex;gap:10px">
        <div style="flex:3"><label>Endereço</label><input name="endereco" id="endereco" required></div>
        <div style="flex:1"><label>Número</label><input name="numero" required placeholder="nº"></div>
      </div>
      <label>Complemento (opcional)</label><input name="complemento" placeholder="sala, andar...">
      <label>Bairro</label><input name="bairro" id="bairro" required>
      <div style="display:flex;gap:10px">
        <div style="flex:3"><label>Cidade</label><input name="cidade" id="cidade" required></div>
        <div style="flex:1"><label>UF</label><input name="uf" id="uf" required maxlength="2" placeholder="UF"></div>
      </div>
      <label>E-mail</label><input type="email" value="{current_user.email}" disabled style="opacity:.7">
      <button class="btn" type="submit">Ir para o pagamento →</button>
    </form>
    <p style="font-size:.8rem;color:#5a6a7a;margin-top:14px">Você escolhe Pix, boleto ou cartão na próxima tela (Asaas). A conta é ativada automaticamente após a confirmação do pagamento, e a nota fiscal é emitida.</p>
    <div class="link"><a href="/">← Voltar</a></div>
    <script>
      document.getElementById('cep').addEventListener('blur', function() {{
        var cep = this.value.replace(/\\D/g, '');
        if (cep.length !== 8) return;
        fetch('https://viacep.com.br/ws/' + cep + '/json/')
          .then(function(r) {{ return r.json(); }})
          .then(function(d) {{
            if (d.erro) return;
            if (d.logradouro) document.getElementById('endereco').value = d.logradouro;
            if (d.bairro) document.getElementById('bairro').value = d.bairro;
            if (d.localidade) document.getElementById('cidade').value = d.localidade;
            if (d.uf) document.getElementById('uf').value = d.uf;
          }})
          .catch(function() {{}});
      }});
      document.querySelectorAll('.plano input').forEach(function(r) {{
        r.addEventListener('change', function() {{
          document.querySelectorAll('.plano').forEach(function(p) {{ p.classList.remove('sel'); }});
          this.closest('.plano').classList.add('sel');
        }});
      }});
    </script>"""
    return _pagina_auth("Assinar", corpo)


@app.route("/assinar", methods=["POST"])
@login_required
def assinar_post():
    nome = (request.form.get("nome") or current_user.nome or current_user.email).strip()
    cpf = "".join(filter(str.isalnum, request.form.get("cpfCnpj") or ""))
    cep = "".join(filter(str.isdigit, request.form.get("cep") or ""))
    plano = request.form.get("plano")
    if plano not in PLANOS:
        plano = "individual"
    org = current_user.org
    if org is None:  # segurança: garante um escritório
        org = Escritorio(nome=(nome or current_user.email), plano="individual",
                         status="trial", max_membros=1)
        db.session.add(org)
        db.session.flush()
        current_user.org_id = org.id
        current_user.papel = "dono"
        db.session.commit()
    dados_cliente = {
        "name": nome,
        "email": current_user.email,
        "cpfCnpj": cpf,
        "mobilePhone": "".join(filter(str.isdigit, request.form.get("telefone") or "")),
        "postalCode": cep,
        "address": (request.form.get("endereco") or "").strip(),
        "addressNumber": (request.form.get("numero") or "").strip(),
        "complement": (request.form.get("complemento") or "").strip(),
        "province": (request.form.get("bairro") or "").strip(),
    }
    try:
        if not org.asaas_customer_id:
            cliente = asaas("POST", "/customers", dados_cliente)
            org.asaas_customer_id = cliente.get("id")
            if nome and not current_user.nome:
                current_user.nome = nome
            db.session.commit()
        else:
            # atualiza os dados (inclui endereço necessário para a nota fiscal)
            try:
                asaas("POST", "/customers/%s" % org.asaas_customer_id, dados_cliente)
            except Exception:
                # cliente pode ter sido excluído no Asaas — cria um novo
                cliente = asaas("POST", "/customers", dados_cliente)
                org.asaas_customer_id = cliente.get("id")
                db.session.commit()
        # registra o plano escolhido no escritório
        org.plano = plano
        org.max_membros = PLANOS[plano]["max_membros"]
        org.creditos_total = PLANOS[plano]["pool"]
        org.telefone = (request.form.get("telefone") or "").strip() or org.telefone
        org.cidade = (request.form.get("cidade") or "").strip() or org.cidade
        org.uf = (request.form.get("uf") or "").strip().upper() or org.uf
        # cota do dono: Individual = 50; Escritório = pool inteiro (gestor redistribui aos membros)
        if plano == "escritorio":
            outros = sum((m.cota_mensal or 0) for m in org.usuarios if m.id != current_user.id)
            current_user.cota_mensal = max(PLANOS[plano]["pool"] - outros, 0)
        elif not current_user.cota_mensal:
            current_user.cota_mensal = 50
        db.session.commit()
        assinatura = asaas("POST", "/subscriptions", {
            "customer": org.asaas_customer_id,
            "billingType": "UNDEFINED",
            "value": valor_cobranca(plano),
            "nextDueDate": date.today().isoformat(),
            "cycle": "MONTHLY",
            "description": PLANOS[plano]["desc"],
        })
        org.asaas_subscription_id = assinatura.get("id")
        db.session.commit()
        # Configura emissão automática de nota fiscal para a assinatura (se ativado)
        if NF_AUTO and (NF_SERVICO_ID or NF_SERVICO_CODIGO or NF_SERVICO_NOME):
            cfg_nf = {
                "deductions": NF_DEDUCOES,
                "effectiveDatePeriod": NF_QUANDO,
                "receivedOnly": True,
                "observations": NF_OBSERVACOES or PLANOS[plano]["desc"],
                "taxes": {"retainIss": NF_RETER_ISS, "iss": NF_ISS,
                          "cofins": 0, "csll": 0, "inss": 0, "ir": 0, "pis": 0},
            }
            if NF_SERVICO_ID:
                cfg_nf["municipalServiceId"] = NF_SERVICO_ID
            if NF_SERVICO_CODIGO:
                cfg_nf["municipalServiceCode"] = NF_SERVICO_CODIGO
            if NF_SERVICO_NOME:
                cfg_nf["municipalServiceName"] = NF_SERVICO_NOME
            try:
                asaas("POST", "/subscriptions/%s/invoiceSettings" % assinatura.get("id"), cfg_nf)
            except Exception:
                pass  # não bloquear o pagamento se a configuração de NF falhar

        pagamentos = asaas("GET", "/subscriptions/%s/payments" % assinatura.get("id"))
        dados = (pagamentos.get("data") or [])
        url_pagamento = dados[0].get("invoiceUrl") if dados else None
        if not url_pagamento:
            raise RuntimeError("Não foi possível obter o link de pagamento.")
        return redirect(url_pagamento)
    except Exception as e:  # noqa: BLE001
        corpo = f"""<h2>Ops, algo deu errado</h2>
        <div class="erro">{str(e)[:300]}</div>
        <div class="link"><a href="/assinar">← Tentar de novo</a></div>"""
        return _pagina_auth("Erro", corpo)


# ============================================================
# Painel "Minha Conta" + gestão de equipe
# ============================================================
PAGINA_CONTA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Minha Conta · Repactua</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'%3E%3Crect width='80' height='80' rx='18' fill='%231a3a5c'/%3E%3Cpolyline points='26,22 38,40 26,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpolyline points='54,22 42,40 54,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='40' cy='40' r='3.5' fill='%23c8960c'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:#f4f6f9;min-height:100vh;color:#1c2b3a;padding:24px}
.wrap{max-width:760px;margin:0 auto}
.topo{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.topo h1{color:#1a3a5c;font-size:1.4rem;display:flex;align-items:center;gap:10px}
.topo a{color:#2c5f8a;text-decoration:none;font-size:.9rem;font-weight:600}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 14px rgba(0,0,0,.07);padding:22px;margin-bottom:18px}
.card h2{font-size:1.05rem;color:#1a3a5c;margin-bottom:14px;border-bottom:1px solid #eef1f5;padding-bottom:10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:8px 0;font-size:.92rem}
.badge{padding:3px 12px;border-radius:20px;font-size:.78rem;font-weight:700}
.b-ativo{background:#e9f7ee;color:#1b5e20}.b-trial{background:#fff4e0;color:#9a6700}.b-inativo{background:#fdecea;color:#7a2218}
.bar{height:12px;background:#eef1f5;border-radius:8px;overflow:hidden;margin-top:6px}
.bar > i{display:block;height:100%;background:#c8960c;border-radius:8px}
.muted{color:#5a6a7a;font-size:.82rem}
table{width:100%;border-collapse:collapse;margin-top:8px}
th{text-align:left;font-size:.72rem;text-transform:uppercase;color:#5a6a7a;padding:8px;border-bottom:1px solid #eef1f5}
td{padding:8px;border-bottom:1px solid #f3f5f8;font-size:.88rem;vertical-align:middle}
input,button{font-family:inherit}
input[type=text],input[type=email],input[type=password],input[type=number]{padding:9px 11px;border:1.5px solid #d0d7e2;border-radius:7px;font-size:.9rem;background:#fafbfd;width:100%}
.btn{padding:9px 16px;border:none;border-radius:7px;background:#c8960c;color:#fff;font-weight:700;cursor:pointer;font-size:.88rem}
.btn:hover{background:#f0b429}
.btn-sm{padding:5px 10px;font-size:.78rem;border-radius:6px;border:none;cursor:pointer}
.btn-rem{background:#fdecea;color:#a3271a}.btn-cota{background:#eef4fb;color:#2c5f8a}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid label{font-size:.74rem;text-transform:uppercase;color:#5a6a7a;font-weight:600;display:block;margin-bottom:4px}
.ok{background:#e9f7ee;color:#1b5e20;border:1px solid #7ec891;border-radius:8px;padding:10px 14px;font-size:.85rem;margin-bottom:14px}
.erro{background:#fdecea;color:#7a2218;border:1px solid #e8a49a;border-radius:8px;padding:10px 14px;font-size:.85rem;margin-bottom:14px}
.pool{background:#fffaf0;border:1px solid #f0d9a0;border-radius:8px;padding:12px;margin-bottom:12px;font-size:.9rem}
.upgrade-box{background:#fffaf0;border:1px solid #f0d9a0;border-radius:10px;padding:14px;margin-top:14px;font-size:.9rem}
.upgrade-box .btn{background:#c8960c;color:#fff;border:none;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer;font-size:.9rem}
.upgrade-box .btn:hover{background:#f0b429}
</style></head><body><div class="wrap">{{CORPO}}</div></body></html>"""


PAGINA_HOME = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Início · Repactua</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'%3E%3Crect width='80' height='80' rx='18' fill='%231a3a5c'/%3E%3Cpolyline points='26,22 38,40 26,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpolyline points='54,22 42,40 54,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='40' cy='40' r='3.5' fill='%23c8960c'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:#eef1f6;color:#1c2b3a;padding:24px}
.wrap{max-width:1160px;margin:0 auto}
.topo{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:10px}
.marca{display:flex;align-items:center;gap:11px;font-weight:700;font-size:1.18rem;color:#1a3a5c}
.marca small{display:block;font-weight:400;color:#5a6a7a;font-size:.8rem}
.links a{color:#2c5f8a;text-decoration:none;font-size:.88rem;font-weight:600}
.links a:hover{color:#c8960c}
.hero{background:#1a3a5c;color:#fff;border-radius:14px;padding:22px 24px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.hero h1{font-size:1.4rem;font-weight:700}
.hero-sub{color:rgba(255,255,255,.75);font-size:.86rem;margin-top:3px}
.btn-novo{background:#c8960c;color:#fff;text-decoration:none;font-weight:700;padding:11px 20px;border-radius:9px;font-size:.95rem;white-space:nowrap}
.btn-novo:hover{background:#f0b429}
.alerta{display:block;background:#fff4e0;color:#8a5a00;border:1px solid #f0d293;border-radius:10px;padding:12px 16px;margin-bottom:16px;text-decoration:none;font-size:.9rem;font-weight:600}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:14px}
.metric{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.06);padding:16px 18px;text-decoration:none;color:#1c2b3a;border:1.5px solid transparent}
.metric:hover{border-color:#c8960c}
.m-label{font-size:.74rem;text-transform:uppercase;letter-spacing:.4px;color:#7a8794;font-weight:600}
.m-valor{font-size:1.7rem;font-weight:700;color:#1a3a5c;margin:4px 0 1px}
.m-sub{font-size:.78rem;color:#8a97a5}
.bar-wrap{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.06);padding:14px 18px;margin-bottom:16px}
.bar{height:11px;background:#eef1f5;border-radius:8px;overflow:hidden}
.bar>i{display:block;height:100%;background:#c8960c;border-radius:8px}
.bar-leg{display:block;margin-top:8px;font-size:.8rem;color:#5a6a7a}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#fff;border-radius:12px;box-shadow:0 2px 14px rgba(0,0,0,.07);padding:20px}
.card-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.card h2{font-size:1rem;color:#1a3a5c;margin-bottom:12px}
.card-h h2{margin-bottom:0}
.atalhos{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.atalho{background:#f7f9fc;border-radius:10px;padding:14px;text-decoration:none;color:#1c2b3a;border:1.5px solid transparent}
.atalho:hover{border-color:#c8960c}
.atalho.destaque{background:#1a3a5c;color:#fff;grid-column:1/-1}
.a-ico{font-size:1.6rem;line-height:1}
.a-nome{font-weight:700;margin-top:7px}
.atalho.destaque .a-sub{color:rgba(255,255,255,.8)}
.a-sub{font-size:.76rem;color:#5a6a7a;margin-top:2px}
.recente{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #f0f3f7;text-decoration:none;color:#1c2b3a}
.recente:last-of-type{border-bottom:none}
.recente:hover .r-nome{color:#c8960c}
.r-nome{font-weight:600;font-size:.88rem}
.r-data{color:#8a97a5;font-size:.78rem;white-space:nowrap;margin-left:10px}
.vertodos{color:#2c5f8a;text-decoration:none;font-weight:600;font-size:.82rem}
.vertodos:hover{color:#c8960c}
.vazio{color:#5a6a7a;font-size:.88rem}.vazio a{color:#2c5f8a}
@media(max-width:640px){.grid2{grid-template-columns:1fr}}
</style></head><body><div class="wrap">{{CORPO}}</div></body></html>"""


def _badge(status):
    cls = {"ativo": "b-ativo", "trial": "b-trial", "inativo": "b-inativo"}.get(status, "b-trial")
    return f'<span class="badge {cls}">{status.upper()}</span>'


def _bloco_cancelar(u, org):
    """Link discreto de cancelamento (dono com assinatura paga ativa)."""
    if not org or u.papel != "dono" or not org.asaas_subscription_id:
        return ""
    return ('<form method="post" action="/conta/cancelar" style="margin-top:14px;text-align:right" '
            'onsubmit="return confirm(\'Cancelar a assinatura? Não haverá novas cobranças e o acesso '
            'continua até o fim do período já pago.\')">'
            '<button type="submit" style="background:none;border:none;color:#a3271a;font-size:.78rem;'
            'cursor:pointer;text-decoration:underline">Cancelar assinatura</button></form>')


def _bloco_upgrade(u, plano):
    """CTA de upgrade para Escritório, mostrado ao dono de um plano Individual."""
    if plano != "individual" or u.papel != "dono":
        return ""
    valor = ('%.2f' % PLANOS["escritorio"]["valor"]).replace('.', ',')
    return (
        '<div class="upgrade-box">'
        '<div><b>Precisa de mais consultas ou de uma equipe?</b>'
        f'<div class="muted" style="margin-top:3px">Suba para o <b>Escritório</b>: pool de <b>250 consultas/mês</b>, '
        f'até <b>5 acessos</b> e gestão de equipe. Por R$ {valor}/mês.</div></div>'
        '<form method="post" action="/conta/upgrade" style="margin-top:10px" '
        'onsubmit="return confirm(\'Confirmar upgrade para o plano Escritório? A próxima cobrança passará para R$ ' + valor + '.\')">'
        '<button class="btn" type="submit">⬆️ Fazer upgrade para Escritório</button></form>'
        '</div>'
    )


def render_conta(msg_ok="", msg_erro=""):
    u = current_user
    org = u.org
    plano = org.plano if org else "individual"
    plano_nome = PLANOS.get(plano, {}).get("nome", "Individual")
    usados = (u.usage_contagem or 0) if u.usage_mes == datetime.utcnow().strftime("%Y-%m") else 0
    cota = u.cota_mensal or 0
    pct = int(usados * 100 / cota) if cota else 0
    avisos = (f'<div class="ok">{msg_ok}</div>' if msg_ok else "") + \
             (f'<div class="erro">{msg_erro}</div>' if msg_erro else "")

    bloco_plano = f"""<div class="card">
      <h2>Plano</h2>
      <div class="row"><span>Plano atual</span><b>{plano_nome}</b></div>
      <div class="row"><span>Situação</span>{_badge(u.status_efetivo)}</div>
      <div class="row"><span>Suas consultas este mês</span><b>{usados} / {cota}</b></div>
      <div class="bar"><i style="width:{min(pct,100)}%"></i></div>
      <div class="muted" style="margin-top:8px">As consultas renovam todo mês.{' ' if u.status_efetivo=='ativo' else ' Assine para liberar 50/mês.'}</div>
      {'' if u.status_efetivo=='ativo' else '<div class="row" style="margin-top:10px"><a class="btn" href="/assinar" style="text-decoration:none">Assinar agora →</a></div>'}
      {_bloco_upgrade(u, plano)}
      {_bloco_cancelar(u, org)}
    </div>"""

    bloco_equipe = ""
    if u.papel == "dono" and plano == "escritorio" and org:
        linhas = ""
        for m in sorted(org.usuarios, key=lambda x: (x.papel != "dono", x.nome or x.email)):
            m_usados = (m.usage_contagem or 0) if m.usage_mes == datetime.utcnow().strftime("%Y-%m") else 0
            eh_dono = m.papel == "dono"
            acoes = ""
            if not eh_dono:
                acoes = f"""<form method="post" action="/conta/membro/{m.id}/remover" style="display:inline" onsubmit="return confirm('Remover {m.email}?')"><button class="btn-sm btn-rem">remover</button></form>"""
            linhas += f"""<tr>
              <td>{m.nome or '—'}<br><small class="muted">{m.email}</small></td>
              <td>{'dono' if eh_dono else 'membro'}</td>
              <td>
                <form method="post" action="/conta/membro/{m.id}/cota" style="display:flex;gap:6px;align-items:center">
                  <input type="number" name="cota" value="{m.cota_mensal or 0}" min="0" style="width:74px" min="0">
                  <button class="btn-sm btn-cota">salvar</button>
                </form>
              </td>
              <td>{m_usados}</td>
              <td>{acoes}</td>
            </tr>"""
        form_add = ""
        if org.vagas_restantes > 0:
            form_add = f"""<h2 style="margin-top:20px">Adicionar membro</h2>
            <form method="post" action="/conta/membro">
              <div class="grid">
                <div><label>Nome</label><input type="text" name="nome" placeholder="Nome do membro"></div>
                <div><label>E-mail (login)</label><input type="email" name="email" required placeholder="colega@escritorio.adv.br"></div>
                <div><label>Senha inicial</label><input type="text" name="senha" required placeholder="mínimo 6 caracteres"></div>
                <div><label>Cota de consultas/mês</label><input type="number" name="cota" value="0" min="0" max="{org.cota_disponivel}"></div>
              </div>
              <div style="margin-top:12px"><button class="btn">Criar membro</button></div>
            </form>"""
        else:
            form_add = '<div class="muted" style="margin-top:14px">Limite de 5 acessos atingido. Remova um membro para adicionar outro.</div>'
        bloco_equipe = f"""<div class="card">
          <h2>Equipe do escritório</h2>
          <div class="pool">
            <b>Pool de créditos:</b> {org.cota_distribuida} de {org.creditos_total} distribuídos ·
            <b>{org.cota_disponivel}</b> disponíveis para distribuir ·
            {org.total_membros}/{org.max_membros} acessos
          </div>
          <table>
            <thead><tr><th>Membro</th><th>Papel</th><th>Cota/mês</th><th>Usou</th><th></th></tr></thead>
            <tbody>{linhas}</tbody>
          </table>
          {form_add}
        </div>"""

    bloco_senha = """<div class="card">
      <h2>Segurança</h2>
      <form method="post" action="/conta/senha" class="grid">
        <div><label>Nova senha</label><input type="password" name="senha" required placeholder="mínimo 6 caracteres"></div>
        <div style="display:flex;align-items:flex-end"><button class="btn">Trocar senha</button></div>
      </form>
    </div>"""

    corpo = f"""<div class="topo">
      <h1>{logo_repactua(30)} <span>Minha Conta</span></h1>
      <div><a href="/calculadora">← Calculadora</a>{' · <a href="/admin">Admin</a>' if u.is_admin else ''} · <a href="/logout">Sair</a></div>
    </div>
    {avisos}{bloco_plano}{bloco_equipe}{bloco_senha}"""
    return Response(PAGINA_CONTA.replace("{{CORPO}}", corpo), mimetype="text/html")


@app.route("/conta")
@login_required
def conta():
    return render_conta()


@app.route("/conta/senha", methods=["POST"])
@login_required
def conta_senha():
    senha = request.form.get("senha") or ""
    if len(senha) < 6:
        return render_conta(msg_erro="A senha deve ter no mínimo 6 caracteres.")
    current_user.set_senha(senha)
    db.session.commit()
    return render_conta(msg_ok="Senha alterada com sucesso.")


def _exige_dono_escritorio():
    org = current_user.org
    if current_user.papel != "dono" or not org or org.plano != "escritorio":
        return None
    return org


@app.route("/conta/membro", methods=["POST"])
@login_required
def conta_membro_add():
    org = _exige_dono_escritorio()
    if not org:
        return render_conta(msg_erro="Apenas o dono de um plano Escritório pode adicionar membros.")
    nome = (request.form.get("nome") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    senha = request.form.get("senha") or ""
    try:
        cota = max(int(request.form.get("cota") or 0), 0)
    except ValueError:
        cota = 0
    if org.vagas_restantes <= 0:
        return render_conta(msg_erro="Limite de 5 acessos atingido.")
    if not email or len(senha) < 6:
        return render_conta(msg_erro="Informe e-mail válido e senha de no mínimo 6 caracteres.")
    if User.query.filter_by(email=email).first():
        return render_conta(msg_erro="Já existe uma conta com este e-mail.")
    if cota > org.cota_disponivel:
        return render_conta(msg_erro=f"Cota indisponível. Restam {org.cota_disponivel} créditos para distribuir.")
    m = User(email=email, nome=nome, escritorio=org.nome, org_id=org.id,
             papel="membro", cota_mensal=cota, status=org.status)
    m.set_senha(senha)
    db.session.add(m)
    db.session.commit()
    return render_conta(msg_ok=f"Membro {email} criado com cota de {cota} consultas/mês.")


@app.route("/conta/membro/<int:mid>/cota", methods=["POST"])
@login_required
def conta_membro_cota(mid):
    org = _exige_dono_escritorio()
    if not org:
        return render_conta(msg_erro="Ação não permitida.")
    m = db.session.get(User, mid)
    if not m or m.org_id != org.id:
        return render_conta(msg_erro="Membro não encontrado.")
    try:
        nova = max(int(request.form.get("cota") or 0), 0)
    except ValueError:
        return render_conta(msg_erro="Cota inválida.")
    # disponível considerando a cota atual deste membro
    disponivel_para_ele = org.cota_disponivel + (m.cota_mensal or 0)
    if nova > disponivel_para_ele:
        return render_conta(msg_erro=f"Cota acima do pool. Máximo para este membro: {disponivel_para_ele}.")
    m.cota_mensal = nova
    db.session.commit()
    return render_conta(msg_ok=f"Cota de {m.email} ajustada para {nova}.")


@app.route("/conta/membro/<int:mid>/remover", methods=["POST"])
@login_required
def conta_membro_remover(mid):
    org = _exige_dono_escritorio()
    if not org:
        return render_conta(msg_erro="Ação não permitida.")
    m = db.session.get(User, mid)
    if not m or m.org_id != org.id or m.papel == "dono":
        return render_conta(msg_erro="Não é possível remover este usuário.")
    db.session.delete(m)
    db.session.commit()
    return render_conta(msg_ok="Membro removido.")


def _trocar_plano_assinatura(org, novo_plano):
    """Atualiza a assinatura no Asaas (valor do novo plano) e o escritório local."""
    sub_id = org.asaas_subscription_id
    if not sub_id and org.asaas_customer_id:
        try:
            r = asaas("GET", "/subscriptions?customer=%s" % org.asaas_customer_id)
            data = r.get("data") or []
            ativas = [s for s in data if s.get("status") == "ACTIVE"] or data
            if ativas:
                sub_id = ativas[0].get("id")
                org.asaas_subscription_id = sub_id
        except Exception:
            pass
    if sub_id:
        try:
            asaas("PUT", "/subscriptions/%s" % sub_id, {
                "value": valor_cobranca(novo_plano),
                "description": PLANOS[novo_plano]["desc"],
                "updatePendingPayments": True,
            })
        except Exception:
            pass  # não trava o upgrade local se a API falhar
    org.plano = novo_plano
    org.max_membros = PLANOS[novo_plano]["max_membros"]
    org.creditos_total = PLANOS[novo_plano]["pool"]


@app.route("/conta/cancelar", methods=["POST"])
@login_required
def conta_cancelar():
    """Cliente cancela a assinatura: para as cobranças; acesso vale até o fim do período pago."""
    org = current_user.org
    if not org or current_user.papel != "dono":
        return render_conta(msg_erro="Apenas o dono da conta pode cancelar a assinatura.")
    sub_id = org.asaas_subscription_id
    if not sub_id:
        return render_conta(msg_erro="Não há assinatura paga para cancelar nesta conta.")
    try:
        asaas("DELETE", "/subscriptions/%s" % sub_id)
    except Exception:
        pass  # se já estava cancelada no Asaas, segue
    org.asaas_subscription_id = None
    if not org.acesso_ate:
        org.acesso_ate = date.today() + timedelta(days=30)
    db.session.commit()
    _enviar_email("🚫 Repactua: assinatura cancelada",
                  f"O cliente {org.nome or current_user.email} cancelou a assinatura. "
                  f"Acesso válido até {org.acesso_ate.strftime('%d/%m/%Y')}.")
    return render_conta(msg_ok=f"Assinatura cancelada. Não haverá novas cobranças, e seu acesso "
                               f"continua até {org.acesso_ate.strftime('%d/%m/%Y')}.")


@app.route("/conta/upgrade", methods=["POST"])
@login_required
def conta_upgrade():
    org = current_user.org
    if not org or current_user.papel != "dono":
        return render_conta(msg_erro="Apenas o dono da conta pode mudar o plano.")
    if org.plano == "escritorio":
        return render_conta(msg_erro="Você já está no plano Escritório.")
    _trocar_plano_assinatura(org, "escritorio")
    # o dono recebe o pool inteiro menos o que já estiver com outros membros
    outros = sum((m.cota_mensal or 0) for m in org.usuarios if m.id != current_user.id)
    current_user.cota_mensal = max(PLANOS["escritorio"]["pool"] - outros, 0)
    db.session.commit()
    valor = ('%.2f' % valor_cobranca("escritorio")).replace('.', ',')
    return render_conta(msg_ok=f"Upgrade para o plano Escritório concluído! Pool de 250 consultas/mês e até 5 acessos. A próxima cobrança será de R$ {valor}.")


# ============================================================
# Webhook do Asaas (ativa/desativa assinatura conforme pagamento)
# ============================================================
@app.route("/api/asaas-webhook", methods=["POST"])
def asaas_webhook():
    if ASAAS_WEBHOOK_TOKEN and request.headers.get("asaas-access-token") != ASAAS_WEBHOOK_TOKEN:
        return jsonify({"ok": False}), 401
    evento = request.get_json(silent=True) or {}
    tipo = evento.get("event", "")
    pagamento = evento.get("payment", {}) or {}
    cust_id = pagamento.get("customer")
    email = (pagamento.get("customerEmail") or "").lower()
    org = None
    if cust_id:
        org = Escritorio.query.filter_by(asaas_customer_id=cust_id).first()
    if not org and email:
        u = User.query.filter_by(email=email).first()
        org = u.org if u else None
    if org:
        if tipo in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
            org.status = "ativo"
            org.acesso_ate = date.today() + timedelta(days=37)  # 1 mês + folga
            if cust_id and not org.asaas_customer_id:
                org.asaas_customer_id = cust_id
            _enviar_email(
                "💰 Repactua: pagamento recebido",
                f"Pagamento confirmado de {org.nome or email or cust_id} "
                f"(R$ {pagamento.get('value', '?')}) — conta ativada/renovada.")
        elif tipo in ("PAYMENT_OVERDUE", "PAYMENT_DELETED", "PAYMENT_REFUNDED", "SUBSCRIPTION_DELETED"):
            org.status = "inativo"
            _enviar_email(
                "⚠️ Repactua: pagamento com problema",
                f"Evento {tipo} para {org.nome or email or cust_id} — conta inativada.")
        db.session.commit()
    return jsonify({"ok": True})


# ============================================================
# Admin (gestão simples de assinantes) — login próprio, separado do app
# ============================================================
def _admin_logado():
    """Admin via sessão própria (login separado) OU usuário logado que é admin."""
    if session.get("admin_ok"):
        return True
    return current_user.is_authenticated and current_user.is_admin


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    erro = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        u = User.query.filter_by(email=email).first()
        if u and u.is_admin and u.conferir_senha(senha):
            session["admin_ok"] = True
            session["admin_email"] = email
            return redirect(url_for("admin"))
        erro = '<div class="erro">Credenciais inválidas ou conta sem permissão de admin.</div>'
    corpo = f"""<h2>Painel Administrativo</h2>
    <div class="sub">Acesso restrito — gestão Repactua.</div>{erro}
    <form method="post">
      <label>E-mail de admin</label><input type="email" name="email" required>
      <label>Senha</label><input type="password" name="senha" required>
      <button class="btn" type="submit">Entrar no painel</button>
    </form>
    <div class="link"><a href="/">← Ir para o site</a></div>"""
    return _pagina_auth("Admin", corpo)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    session.pop("admin_email", None)
    return redirect(url_for("admin_login"))


# ---- Layout do admin (menu lateral compartilhado) ----
ADMIN_SHELL = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{TITULO}} · Repactua Admin</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'%3E%3Crect width='80' height='80' rx='18' fill='%231a3a5c'/%3E%3Cpolyline points='26,22 38,40 26,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Cpolyline points='54,22 42,40 54,58' fill='none' stroke='%23c8960c' stroke-width='6' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='40' cy='40' r='3.5' fill='%23c8960c'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}
body{background:#eef1f6;color:#1c2b3a;display:flex;min-height:100vh}
aside{width:225px;background:#13283e;color:#fff;position:fixed;top:0;bottom:0;left:0;display:flex;flex-direction:column}
.s-logo{display:flex;align-items:center;gap:10px;padding:18px;border-bottom:1px solid rgba(255,255,255,.08);font-weight:700}
.s-logo small{display:block;font-weight:400;opacity:.7;font-size:.7rem}
nav{flex:1;padding:12px 10px;display:flex;flex-direction:column;gap:4px}
.item{display:flex;align-items:center;gap:10px;color:rgba(255,255,255,.85);text-decoration:none;padding:10px 12px;border-radius:8px;font-size:.9rem;font-weight:600}
.item:hover{background:rgba(255,255,255,.08)}
.item.ativo{background:#c8960c;color:#13283e}
.s-user{padding:14px 18px;border-top:1px solid rgba(255,255,255,.08);font-size:.74rem;opacity:.9;word-break:break-all}
.s-user a{color:#f0b429;text-decoration:none;font-weight:700}
main{flex:1;margin-left:225px;padding:26px 30px;max-width:calc(100% - 225px)}
h1{color:#1a3a5c;font-size:1.35rem;margin-bottom:2px}
h2{color:#1a3a5c;font-size:1rem;margin:20px 0 10px}
.sub{color:#5a6a7a;font-size:.86rem;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:13px;margin-bottom:14px}
.mc{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.06);padding:15px 17px}
.mc .lbl{font-size:.7rem;text-transform:uppercase;letter-spacing:.4px;color:#5a6a7a;margin-bottom:5px}
.mc .val{font-size:1.5rem;font-weight:700;color:#1a3a5c}
.mc .det{font-size:.72rem;color:#8a97a5;margin-top:3px}
.mc.verde{border-left:4px solid #1e7e34}.mc.verde .val{color:#1e7e34}
.mc.ouro{border-left:4px solid #c8960c}.mc.ouro .val{color:#9a6700}
.mc.rubi .val{color:#a3271a}
.painel{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.06);padding:16px}
.duas{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media(max-width:1000px){.duas{grid-template-columns:1fr}}
@media(max-width:820px){body{flex-direction:column}aside{position:static;width:100%;flex-direction:row;align-items:center}nav{flex-direction:row;overflow-x:auto;padding:8px}.s-user{display:none}main{margin-left:0;max-width:100%;padding:18px}}
table{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.06);border-radius:10px;overflow:hidden}
th{background:#1a3a5c;color:#fff;padding:10px 11px;text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.4px}
td{padding:10px 11px;border-bottom:1px solid #eef1f5;font-size:.87rem;vertical-align:top}
tr:hover td{background:#fafbfd}
td a{color:#1a3a5c;text-decoration:none;font-size:.8rem;font-weight:600} td a:hover{color:#c8960c}
small{color:#8a97a5;font-weight:400}
.badge{padding:3px 11px;border-radius:20px;font-size:.74rem;font-weight:700;white-space:nowrap}
.b-ativo{background:#e9f7ee;color:#1b5e20}.b-trial{background:#fff4e0;color:#9a6700}.b-inativo{background:#fdecea;color:#7a2218}
.busca{width:100%;max-width:340px;padding:10px 14px;border:1.5px solid #d0d7e2;border-radius:8px;font-size:.9rem;background:#fff;margin-bottom:10px}
.btn-x{background:#fdecea;color:#a3271a;border:none;border-radius:6px;padding:4px 10px;font-size:.76rem;font-weight:700;cursor:pointer}
.btn-x:hover{background:#a3271a;color:#fff}
.nota{font-size:.76rem;color:#8a97a5;margin-top:10px}
</style></head><body>
<aside>
  <div class="s-logo">{{LOGO}} <div>Repactua<small>Painel Master</small></div></div>
  <nav>{{MENU}}</nav>
  <div class="s-user">{{ADMIN_EMAIL}}<br><a href="/admin/logout">Sair ↪</a></div>
</aside>
<main>{{CONTEUDO}}</main>
{{JS}}</body></html>"""


def _admin_page(titulo, conteudo, ativo="dash", extra_js=""):
    itens = [
        ("dash", "/admin", "📊", "Dashboard"),
        ("assin", "/admin/assinantes", "👥", "Assinantes"),
        ("fin", "/admin/financeiro", "💰", "Financeiro"),
        ("logs", "/admin/logs", "📜", "Atividades"),
        ("sub", "/admin/subconta", "🏦", "Subconta"),
        ("wh", "/admin/configurar-webhook", "🔗", "Webhook"),
        ("calc", "/calculadora", "🧮", "Calculadora"),
    ]
    menu = "".join(
        f'<a class="item{" ativo" if k == ativo else ""}" href="{h}"><span>{ic}</span>{lb}</a>'
        for k, h, ic, lb in itens)
    email = session.get("admin_email") or (current_user.email if current_user.is_authenticated else "admin")
    html = (ADMIN_SHELL
            .replace("{{TITULO}}", titulo)
            .replace("{{MENU}}", menu)
            .replace("{{LOGO}}", logo_repactua(30))
            .replace("{{ADMIN_EMAIL}}", email)
            .replace("{{CONTEUDO}}", conteudo)
            .replace("{{JS}}", extra_js))
    return Response(html, mimetype="text/html")


def _serie_6_meses(por_mes):
    """(labels, valores) dos últimos 6 meses a partir de um dict {'AAAA-MM': valor}."""
    MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
                "jul", "ago", "set", "out", "nov", "dez"]
    hoje = date.today()
    ano, mes = hoje.year, hoje.month
    chaves = []
    for _ in range(6):
        chaves.append((ano, mes))
        mes -= 1
        if mes == 0:
            mes, ano = 12, ano - 1
    labels, valores = [], []
    for a, m in reversed(chaves):
        labels.append(f"{MESES_PT[m-1]}/{str(a)[2:]}")
        valores.append(round(por_mes.get(f"{a:04d}-{m:02d}", 0), 2))
    return labels, valores


@app.route("/admin")
def admin():
    """Dashboard: visão geral do negócio (receita, contas, uso de IA, casos)."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    mes_atual = datetime.utcnow().strftime("%Y-%m")
    orgs = Escritorio.query.all()
    ativos = sum(1 for o in orgs if o.status == "ativo")
    trials = sum(1 for o in orgs if o.status == "trial")
    inativos = sum(1 for o in orgs if o.status not in ("ativo", "trial"))
    pagantes = sum(1 for o in orgs if o.status == "ativo" and o.asaas_subscription_id)
    cortesias = ativos - pagantes
    mrr = sum(PLANOS.get(o.plano, {}).get("valor", 0)
              for o in orgs if o.status == "ativo" and o.asaas_subscription_id)
    uso_ia = db.session.query(db.func.sum(User.usage_contagem)).filter(
        User.usage_mes == mes_atual).scalar() or 0
    total_casos = Caso.query.count()
    total_users = User.query.count()
    conv_pct = round(pagantes * 100 / len(orgs)) if orgs else 0
    ja_ativas = pagantes + inativos  # aproximação: quem já esteve/está no jogo pago
    churn_pct = round(inativos * 100 / ja_ativas) if ja_ativas else 0

    # receita real (Asaas)
    pags = _pagamentos_repactua()
    RECEBIDO = ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH")
    receb_mes = receb_total = a_receber = 0.0
    receita_por_mes = {}
    for p in pags:
        v = float(p.get("value") or 0)
        st = p.get("status") or ""
        dt = (p.get("clientPaymentDate") or p.get("paymentDate")
              or p.get("confirmedDate") or p.get("dueDate") or "")[:7]
        if st in RECEBIDO:
            receb_total += v
            receita_por_mes[dt] = receita_por_mes.get(dt, 0) + v
            if dt == mes_atual:
                receb_mes += v
        elif st in ("PENDING", "OVERDUE"):
            a_receber += v

    # novos cadastros por mês (contas)
    cad_por_mes = {}
    for o in orgs:
        k = (o.criado_em or datetime.utcnow()).strftime("%Y-%m")
        cad_por_mes[k] = cad_por_mes.get(k, 0) + 1
    lab_r, val_r = _serie_6_meses(receita_por_mes)
    lab_c, val_c = _serie_6_meses(cad_por_mes)

    # últimos cadastros
    recentes = Escritorio.query.order_by(Escritorio.criado_em.desc()).limit(5).all()
    linhas_rec = ""
    for o in recentes:
        dono = next((u for u in (o.usuarios or []) if u.papel == "dono"), None) or \
               (o.usuarios[0] if o.usuarios else None)
        cls = {"ativo": "b-ativo", "trial": "b-trial"}.get(o.status, "b-inativo")
        sit = {"ativo": "Em dia", "trial": "Em teste"}.get(o.status, "Inativo")
        linhas_rec += f"""<tr>
          <td><b>{o.nome or '—'}</b><br><small>{dono.email if dono else '—'}</small></td>
          <td>{PLANOS.get(o.plano, {}).get('nome', o.plano or '—')}</td>
          <td><span class="badge {cls}">{sit}</span></td>
          <td>{(o.criado_em or datetime.utcnow()).strftime('%d/%m/%Y')}</td></tr>"""
    if not linhas_rec:
        linhas_rec = '<tr><td colspan="4" style="color:#8a97a5">Nenhuma conta ainda.</td></tr>'

    def moeda(v):
        return ("R$ %.2f" % v).replace(".", ",")

    conteudo = f"""
    <h1>Dashboard</h1>
    <div class="sub">Visão geral do Repactua · {datetime.utcnow().strftime('%d/%m/%Y')}</div>
    <div class="cards">
      <div class="mc verde"><div class="lbl">Recebido este mês</div><div class="val">{moeda(receb_mes)}</div></div>
      <div class="mc verde"><div class="lbl">Recebido (total)</div><div class="val">{moeda(receb_total)}</div></div>
      <div class="mc ouro"><div class="lbl">A receber</div><div class="val">{moeda(a_receber)}</div></div>
      <div class="mc"><div class="lbl">MRR</div><div class="val">{moeda(mrr)}</div><div class="det">{pagantes} pagante(s)</div></div>
    </div>
    <div class="cards">
      <div class="mc"><div class="lbl">Contas ativas</div><div class="val">{ativos}</div><div class="det">{pagantes} pagas · {cortesias} cortesia</div></div>
      <div class="mc ouro"><div class="lbl">Em teste</div><div class="val">{trials}</div></div>
      <div class="mc rubi"><div class="lbl">Inativas</div><div class="val">{inativos}</div></div>
      <div class="mc"><div class="lbl">Conversão p/ pago</div><div class="val">{conv_pct}%</div><div class="det">{pagantes} de {len(orgs)} conta(s)</div></div>
      <div class="mc rubi"><div class="lbl">Churn (estimado)</div><div class="val">{churn_pct}%</div><div class="det">inativas vs. já ativas</div></div>
      <div class="mc"><div class="lbl">Usuários (logins)</div><div class="val">{total_users}</div></div>
      <div class="mc"><div class="lbl">Consultas IA no mês</div><div class="val">{uso_ia}</div></div>
      <div class="mc"><div class="lbl">Casos salvos</div><div class="val">{total_casos}</div></div>
    </div>
    <div class="duas">
      <div><h2>📈 Receita recebida (6 meses)</h2>
        <div class="painel"><canvas id="grafReceita" height="200"></canvas></div></div>
      <div><h2>🆕 Novas contas (6 meses)</h2>
        <div class="painel"><canvas id="grafContas" height="200"></canvas></div></div>
    </div>
    <h2>🕘 Últimos cadastros</h2>
    <table><thead><tr><th>Conta</th><th>Plano</th><th>Situação</th><th>Cadastro</th></tr></thead>
    <tbody>{linhas_rec}</tbody></table>
    <p class="nota">Receita = pagamentos reais do Asaas com "Repactua" na descrição. MRR considera apenas assinaturas pagas ativas.</p>"""

    js = """
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
    <script>
    new Chart(document.getElementById('grafReceita'), { type: 'bar',
      data: { labels: %s, datasets: [{ data: %s, backgroundColor: '#1a3a5c',
        hoverBackgroundColor: '#c8960c', borderRadius: 6 }] },
      options: { plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { callback: function(v){ return 'R$ ' + v; } } } } } });
    new Chart(document.getElementById('grafContas'), { type: 'line',
      data: { labels: %s, datasets: [{ data: %s, borderColor: '#c8960c',
        backgroundColor: 'rgba(200,150,12,.15)', fill: true, tension: .35, pointRadius: 4,
        pointBackgroundColor: '#1a3a5c' }] },
      options: { plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } } });
    </script>""" % (json.dumps(lab_r), json.dumps(val_r), json.dumps(lab_c), json.dumps(val_c))

    return _admin_page("Dashboard", conteudo, "dash", js)


@app.route("/admin/assinantes")
def admin_assinantes():
    """Gestão de assinantes: status, plano, admins e exclusão."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    users = User.query.order_by(User.criado_em.desc()).all()
    linhas = ""
    for u in users:
        org = u.org
        plano = (org.plano if org else "—")
        papel = u.papel or "dono"
        if org and plano == "escritorio":
            extra = (f"<br><small>pool {org.creditos_total}: "
                     f"{org.cota_distribuida} distrib. · {org.cota_disponivel} livre · "
                     f"{org.total_membros}/{org.max_membros} acessos</small>")
        else:
            extra = ""
        uso_mes = (u.usage_contagem or 0) if u.usage_mes == datetime.utcnow().strftime('%Y-%m') else 0
        selo_admin = ' <span style="background:#1a3a5c;color:#f0b429;padding:1px 7px;border-radius:10px;font-size:.68rem;font-weight:700">ADMIN</span>' if u.is_admin else ''
        protegido = (u.email == ADMIN_EMAIL) or (u.email == session.get("admin_email"))
        if u.is_admin:
            link_admin = '<span style="color:#aaa;font-size:.8rem">admin protegido</span>' if protegido else f'<a href="/admin/admin/{u.id}/0">remover admin</a>'
        else:
            link_admin = f'<a href="/admin/admin/{u.id}/1">tornar admin</a>'
        if protegido:
            botao_excluir = ""
        else:
            alvo = "a CONTA INTEIRA (escritório, membros e casos)" if papel == "dono" else "este login"
            botao_excluir = (f'<form method="post" action="/admin/excluir/{u.id}" style="display:inline" '
                             f"onsubmit=\"return confirm('EXCLUIR {u.email}? Isso apaga {alvo}. Não pode ser desfeito.')\">"
                             f'<button class="btn-x">🗑 excluir</button></form>')
        nome_cel = f'<a href="/admin/org/{u.org_id}"><b>{u.nome or "—"}</b></a>' if u.org_id else f'<b>{u.nome or "—"}</b>'
        linhas += f"""<tr>
          <td>{nome_cel}{selo_admin}<br><small>{u.email}</small></td>
          <td>{u.escritorio or '—'}<br><small>{plano} · {papel}</small>{extra}</td>
          <td><b>{u.status_efetivo}</b></td>
          <td>{uso_mes}/{u.limite_mensal}<br><small>cota pessoal</small></td>
          <td>
            <a href="/admin/status/{u.id}/ativo">ativar</a> ·
            <a href="/admin/status/{u.id}/inativo">inativar</a> ·
            <a href="/admin/status/{u.id}/trial">trial</a><br>
            <a href="/admin/plano/{u.id}/escritorio">→ escritório (cortesia)</a> ·
            <a href="/admin/plano/{u.id}/individual">→ individual</a><br>
            {link_admin} &nbsp; {botao_excluir}
          </td></tr>"""
    conteudo = f"""
    <h1>Assinantes</h1>
    <div class="sub">{len(users)} login(s) cadastrado(s) · clique no nome para abrir a ficha completa</div>
    <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
      <input class="busca" id="busca" onkeyup="filtrarTabela()" placeholder="🔍 Buscar por nome, e-mail, plano...">
      <a href="/admin/export/assinantes.csv" style="background:#1a3a5c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-size:.82rem;font-weight:700">⬇ Exportar CSV</a>
    </div>
    <table><thead><tr><th>Advogado</th><th>Escritório / plano</th><th>Status</th><th>Uso/mês</th><th>Ações</th></tr></thead>
    <tbody id="tbAssinantes">{linhas}</tbody></table>
    <p class="nota">Excluir um <b>dono</b> apaga o escritório inteiro (membros e casos). Excluir um <b>membro</b> apaga só aquele login. Admins protegidos não podem ser excluídos.</p>"""
    js = """<script>
    function filtrarTabela() {
      var q = document.getElementById('busca').value.toLowerCase();
      document.querySelectorAll('#tbAssinantes tr').forEach(function(tr) {
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
      });
    }
    </script>"""
    return _admin_page("Assinantes", conteudo, "assin", js)


@app.route("/admin/org/<int:oid>")
def admin_org(oid):
    """Ficha completa de uma conta: membros, cotas, pagamentos, ações de suporte."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    org = db.session.get(Escritorio, oid)
    if not org:
        return redirect(url_for("admin_assinantes"))
    senha_gerada = request.args.get("senha")
    aviso = ""
    if senha_gerada:
        aviso = (f'<div style="background:#e9f7ee;color:#1b5e20;border:1px solid #7ec891;'
                 f'border-radius:8px;padding:12px;margin-bottom:14px;font-size:.9rem">'
                 f'🔑 Senha temporária gerada: <b style="font-family:monospace;font-size:1.05rem">{senha_gerada}</b>'
                 f' — repasse ao cliente e oriente a trocar em "Minha conta".</div>')
    plano_nome = PLANOS.get(org.plano, {}).get("nome", org.plano or "—")
    cls = {"ativo": "b-ativo", "trial": "b-trial"}.get(org.status, "b-inativo")
    validade = org.acesso_ate.strftime("%d/%m/%Y") if org.acesso_ate else "sem expiração"
    n_casos = Caso.query.filter_by(org_id=org.id).count()
    mes = datetime.utcnow().strftime("%Y-%m")

    linhas_m = ""
    for m in sorted(org.usuarios or [], key=lambda x: (x.papel != "dono", x.email)):
        uso = (m.usage_contagem or 0) if m.usage_mes == mes else 0
        protegido = (m.email == ADMIN_EMAIL) or (m.email == session.get("admin_email"))
        acoes = f"""
          <form method="post" action="/admin/resetsenha/{m.id}" style="display:inline"
            onsubmit="return confirm('Gerar senha temporária para {m.email}?')">
            <button class="btn-mini">🔑 nova senha</button></form>
          <form method="post" action="/admin/resetuso/{m.id}" style="display:inline"
            onsubmit="return confirm('Zerar o uso do mês de {m.email}?')">
            <button class="btn-mini">♻️ zerar uso</button></form>
          <a class="btn-mini" style="text-decoration:none;display:inline-block"
             href="/admin/entrar-como/{m.id}"
             onclick="return confirm('Entrar como {m.email}? Você verá o sistema como este cliente.')">👁 entrar como</a>"""
        if protegido:
            acoes = '<span style="color:#aaa;font-size:.78rem">admin protegido</span>'
        linhas_m += f"""<tr>
          <td>{m.nome or '—'}<br><small>{m.email}</small></td>
          <td>{m.papel or 'dono'}</td>
          <td><form method="post" action="/admin/cota/{m.id}" style="display:flex;gap:6px">
            <input type="number" name="cota" value="{m.cota_mensal or 0}" min="0"
              style="width:76px;padding:5px 8px;border:1.5px solid #d0d7e2;border-radius:6px">
            <button class="btn-mini">salvar</button></form></td>
          <td>{uso}</td>
          <td>{acoes}</td></tr>"""

    # pagamentos deste cliente no Asaas
    linhas_p = ""
    if org.asaas_customer_id:
        try:
            r = asaas("GET", f"/payments?customer={org.asaas_customer_id}&limit=20")
            STATUS_PT = {"RECEIVED": ("Recebido", "b-ativo"), "CONFIRMED": ("Confirmado", "b-ativo"),
                         "PENDING": ("Aguardando", "b-trial"), "OVERDUE": ("Vencido", "b-inativo"),
                         "REFUNDED": ("Estornado", "b-inativo")}
            for p in (r.get("data") or []):
                dtp = (p.get("clientPaymentDate") or p.get("paymentDate") or p.get("dueDate") or "")
                dtf = f"{dtp[8:10]}/{dtp[5:7]}/{dtp[:4]}" if len(dtp) >= 10 else "—"
                sn, sc = STATUS_PT.get(p.get("status") or "", (p.get("status") or "—", "b-trial"))
                linhas_p += (f'<tr><td>{dtf}</td><td>R$ {("%.2f" % float(p.get("value") or 0)).replace(".", ",")}</td>'
                             f'<td>{p.get("billingType") or "—"}</td><td><span class="badge {sc}">{sn}</span></td></tr>')
        except Exception:
            pass
    if not linhas_p:
        linhas_p = '<tr><td colspan="4" style="color:#8a97a5">Nenhum pagamento encontrado.</td></tr>'

    ret = f"/admin/org/{org.id}"
    conteudo = f"""
    <a href="/admin/assinantes" style="color:#2c5f8a;text-decoration:none;font-size:.85rem">← Voltar aos assinantes</a>
    <h1 style="margin-top:8px">{org.nome or 'Conta'}</h1>
    <div class="sub">Ficha da conta · cadastro em {(org.criado_em or datetime.utcnow()).strftime('%d/%m/%Y')}</div>
    {aviso}
    <div class="cards">
      <div class="mc"><div class="lbl">Plano</div><div class="val" style="font-size:1.1rem">{plano_nome}</div>
        <div class="det"><a href="/admin/plano/{org.usuarios[0].id if org.usuarios else 0}/escritorio?next={ret}">→ escritório</a> · <a href="/admin/plano/{org.usuarios[0].id if org.usuarios else 0}/individual?next={ret}">→ individual</a></div></div>
      <div class="mc"><div class="lbl">Situação</div><div class="val" style="font-size:1.1rem"><span class="badge {cls}">{org.status}</span></div>
        <div class="det"><a href="/admin/status/{org.usuarios[0].id if org.usuarios else 0}/ativo?next={ret}">ativar</a> · <a href="/admin/status/{org.usuarios[0].id if org.usuarios else 0}/inativo?next={ret}">inativar</a> · <a href="/admin/status/{org.usuarios[0].id if org.usuarios else 0}/trial?next={ret}">trial</a></div></div>
      <div class="mc"><div class="lbl">Acesso válido até</div><div class="val" style="font-size:1.1rem">{validade}</div>
        <div class="det">{'assinatura ativa' if org.asaas_subscription_id else 'sem assinatura paga'}</div></div>
      <div class="mc"><div class="lbl">Pool de créditos</div><div class="val" style="font-size:1.1rem">{org.cota_distribuida}/{org.creditos_total}</div>
        <div class="det">{org.total_membros}/{org.max_membros} acessos · {n_casos} caso(s)</div></div>
    </div>
    <h2>👥 Membros</h2>
    <table><thead><tr><th>Membro</th><th>Papel</th><th>Cota/mês</th><th>Usou</th><th>Suporte</th></tr></thead>
    <tbody>{linhas_m}</tbody></table>
    <h2>💸 Pagamentos desta conta</h2>
    <table><thead><tr><th>Data</th><th>Valor</th><th>Forma</th><th>Status</th></tr></thead>
    <tbody>{linhas_p}</tbody></table>
    <p class="nota">"Entrar como" abre o sistema logado como o cliente (sua sessão de admin continua valendo — volte pelo /admin). A senha temporária aparece uma única vez.</p>
    <style>.btn-mini{{background:#eef4fb;color:#2c5f8a;border:none;border-radius:6px;padding:4px 9px;font-size:.74rem;font-weight:700;cursor:pointer;margin:2px 2px 2px 0}}.btn-mini:hover{{background:#1a3a5c;color:#fff}}</style>"""
    return _admin_page(org.nome or "Conta", conteudo, "assin")


@app.route("/admin/cota/<int:uid>", methods=["POST"])
def admin_cota(uid):
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if u:
        try:
            u.cota_mensal = max(int(request.form.get("cota") or 0), 0)
            db.session.commit()
            _log_admin(f"ajustou cota para {u.cota_mensal}", u.email)
        except ValueError:
            pass
    return redirect(f"/admin/org/{u.org_id}" if u and u.org_id else url_for("admin_assinantes"))


@app.route("/admin/resetuso/<int:uid>", methods=["POST"])
def admin_resetuso(uid):
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if u:
        u.usage_mes = datetime.utcnow().strftime("%Y-%m")
        u.usage_contagem = 0
        db.session.commit()
        _log_admin("zerou o uso do mês", u.email)
    return redirect(f"/admin/org/{u.org_id}" if u and u.org_id else url_for("admin_assinantes"))


@app.route("/admin/resetsenha/<int:uid>", methods=["POST"])
def admin_resetsenha(uid):
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if not u or u.email == ADMIN_EMAIL or u.email == session.get("admin_email"):
        return redirect(url_for("admin_assinantes"))
    nova = secrets.token_hex(4)  # 8 caracteres
    u.set_senha(nova)
    db.session.commit()
    _log_admin("gerou senha temporária", u.email)
    return redirect(f"/admin/org/{u.org_id}?senha={nova}" if u.org_id else url_for("admin_assinantes"))


@app.route("/admin/entrar-como/<int:uid>")
def admin_entrar_como(uid):
    """Loga como o cliente para dar suporte (a sessão de admin continua ativa)."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if not u:
        return redirect(url_for("admin_assinantes"))
    _log_admin("entrou como cliente", u.email)
    login_user(u, remember=False)
    return redirect(url_for("index"))


@app.route("/admin/excluir/<int:uid>", methods=["POST"])
def admin_excluir(uid):
    """Exclui um login (membro) ou a conta inteira (dono: escritório+membros+casos)."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if not u or u.email == ADMIN_EMAIL or u.email == session.get("admin_email"):
        return redirect(url_for("admin_assinantes"))
    org = u.org
    if (u.papel or "dono") == "dono" and org:
        Caso.query.filter_by(org_id=org.id).delete()
        for m in list(org.usuarios or []):
            db.session.delete(m)
        db.session.delete(org)
    else:
        Caso.query.filter_by(user_id=u.id).update({"user_id": None})
        db.session.delete(u)
    db.session.commit()
    _log_admin("excluiu conta/login", u.email)
    return redirect(url_for("admin_assinantes"))


def _admin_testar_email_impl():
    """Tenta enviar um e-mail de teste e devolve o erro real (sem engolir exceção)."""
    para = request.args.get("para") or ALERTA_EMAIL or ADMIN_EMAIL
    provedor = "brevo-api" if BREVO_API_KEY else ("smtp:" + SMTP_HOST if SMTP_HOST else "nenhum")
    try:
        _enviar_email_impl("Repactua — teste de e-mail",
                           "Teste de envio do Repactua — se você recebeu, o e-mail está funcionando! 🎉",
                           para)
        return {"ok": True, "enviado_para": para, "provedor": provedor,
                "remetente": SMTP_FROM or ALERTA_EMAIL or ADMIN_EMAIL}
    except Exception as e:
        return {"ok": False, "erro": (type(e).__name__ + ": " + str(e))[:600],
                "provedor": provedor}


@app.route("/admin/testar-email")
def admin_testar_email():
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    return jsonify(_admin_testar_email_impl())


@app.route("/admin/logs")
def admin_logs():
    """Histórico das ações administrativas (auditoria)."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    logs = LogAdmin.query.order_by(LogAdmin.quando.desc()).limit(200).all()
    linhas = "".join(
        f"<tr><td>{(l.quando or datetime.utcnow()).strftime('%d/%m/%Y %H:%M')}</td>"
        f"<td>{l.admin_email or '—'}</td><td>{l.acao or '—'}</td><td>{l.alvo or '—'}</td></tr>"
        for l in logs) or '<tr><td colspan="4" style="color:#8a97a5">Nenhuma atividade registrada ainda.</td></tr>'
    conteudo = f"""
    <h1>Atividades</h1>
    <div class="sub">Últimas 200 ações administrativas (auditoria)</div>
    <table><thead><tr><th>Quando</th><th>Admin</th><th>Ação</th><th>Alvo</th></tr></thead>
    <tbody>{linhas}</tbody></table>"""
    return _admin_page("Atividades", conteudo, "logs")


def _csv_response(nome, cabecalho, linhas):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cabecalho)
    w.writerows(linhas)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={nome}"})


@app.route("/admin/export/assinantes.csv")
def export_assinantes():
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    linhas = []
    for u in User.query.order_by(User.criado_em.desc()).all():
        org = u.org
        linhas.append([
            u.nome or "", u.email, u.escritorio or "",
            (org.plano if org else ""), u.papel or "dono", u.status_efetivo,
            u.cota_mensal or 0,
            (u.usage_contagem or 0) if u.usage_mes == datetime.utcnow().strftime("%Y-%m") else 0,
            (org.telefone if org else "") or "", (org.cidade if org else "") or "",
            (org.uf if org else "") or "",
            (u.criado_em or datetime.utcnow()).strftime("%d/%m/%Y"),
        ])
    _log_admin("exportou CSV de assinantes")
    return _csv_response("assinantes-repactua.csv",
                         ["Nome", "Email", "Escritorio", "Plano", "Papel", "Status",
                          "Cota", "Uso no mes", "Telefone", "Cidade", "UF", "Cadastro"], linhas)


@app.route("/admin/export/pagamentos.csv")
def export_pagamentos():
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    nomes = _mapa_clientes_asaas()
    linhas = []
    for p in _pagamentos_repactua():
        dt = (p.get("clientPaymentDate") or p.get("paymentDate") or p.get("dueDate") or "")
        linhas.append([
            dt[:10], nomes.get(p.get("customer"), p.get("customer") or ""),
            str(p.get("value") or 0).replace(".", ","),
            p.get("billingType") or "", p.get("status") or "",
            p.get("description") or "",
        ])
    _log_admin("exportou CSV de pagamentos")
    return _csv_response("pagamentos-repactua.csv",
                         ["Data", "Cliente", "Valor", "Forma", "Status", "Descricao"], linhas)


@app.route("/admin/status/<int:uid>/<novo>")
def admin_status(uid, novo):
    if not _admin_logado() or novo not in ("ativo", "inativo", "trial"):
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if u:
        u.status = novo            # legado
        if u.org:
            u.org.status = novo    # fonte de verdade
        db.session.commit()
        _log_admin(f"mudou status para {novo}", u.email)
    nxt = request.args.get("next") or ""
    return redirect(nxt if nxt.startswith("/admin") else url_for("admin_assinantes"))


@app.route("/admin/admin/<int:uid>/<int:val>")
def admin_set_admin(uid, val):
    """Promove (val=1) ou remove (val=0) um usuário como admin do painel."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if u:
        if val == 1:
            u.is_admin = True
        else:
            # não permite rebaixar o admin principal nem o admin logado
            if u.email != ADMIN_EMAIL and u.email != session.get("admin_email"):
                u.is_admin = False
        db.session.commit()
        _log_admin("promoveu a admin" if val == 1 else "removeu admin", u.email)
    return redirect(url_for("admin_assinantes"))


@app.route("/admin/plano/<int:uid>/<plano>")
def admin_plano(uid, plano):
    """Define o plano do escritório do usuário como cortesia (ativa sem cobrança)."""
    if not _admin_logado() or plano not in PLANOS:
        return redirect(url_for("admin_login"))
    u = db.session.get(User, uid)
    if u and u.org:
        u.org.plano = plano
        u.org.max_membros = PLANOS[plano]["max_membros"]
        u.org.creditos_total = PLANOS[plano]["pool"]
        u.org.status = "ativo"     # cortesia: ativa sem pagamento
        u.status = "ativo"
        # dono recebe o pool inteiro menos o que já está com outros membros
        if plano == "escritorio":
            outros = sum((m.cota_mensal or 0) for m in u.org.usuarios if m.id != u.id)
            u.cota_mensal = max(PLANOS[plano]["pool"] - outros, 0)
        else:
            u.cota_mensal = 50
        db.session.commit()
        _log_admin(f"mudou plano para {plano} (cortesia)", u.email)
    nxt = request.args.get("next") or ""
    return redirect(nxt if nxt.startswith("/admin") else url_for("admin_assinantes"))


@app.route("/admin/subconta", methods=["GET", "POST"])
def admin_subconta():
    """Cria uma subconta no Asaas (POST /accounts). Mostra apiKey/walletId uma única vez."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    msg = ""
    if request.method == "POST":
        payload = {
            "name": (request.form.get("name") or "").strip(),
            "email": (request.form.get("email") or "").strip().lower(),
            "cpfCnpj": "".join(filter(str.isdigit, request.form.get("cpfCnpj") or "")),
            "companyType": request.form.get("companyType") or "LIMITED",
            "phone": "".join(filter(str.isdigit, request.form.get("phone") or "")),
            "mobilePhone": "".join(filter(str.isdigit, request.form.get("mobilePhone") or "")),
            "address": (request.form.get("address") or "").strip(),
            "addressNumber": (request.form.get("addressNumber") or "").strip(),
            "complement": (request.form.get("complement") or "").strip(),
            "province": (request.form.get("province") or "").strip(),
            "postalCode": "".join(filter(str.isdigit, request.form.get("postalCode") or "")),
        }
        try:
            payload["incomeValue"] = float((request.form.get("incomeValue") or "0").replace(",", "."))
        except ValueError:
            payload["incomeValue"] = 0
        try:
            r = asaas("POST", "/accounts", payload)
            api_key = r.get("apiKey", "")
            wallet = r.get("walletId", "")
            acc_id = r.get("id", "")
            msg = f"""<div class="ok"><b>Subconta criada com sucesso!</b> 🎉<br>
              Copie agora (a chave aparece só uma vez):</div>
              <table style="margin-top:8px"><tbody>
              <tr><td><b>API Key</b></td><td style="font-family:monospace;word-break:break-all">{api_key}</td></tr>
              <tr><td><b>Wallet ID</b></td><td style="font-family:monospace">{wallet}</td></tr>
              <tr><td><b>Account ID</b></td><td style="font-family:monospace">{acc_id}</td></tr>
              </tbody></table>
              <div class="sub" style="margin-top:10px">⚠️ Guarde a <b>API Key</b> em local seguro. Próximo passo: trocar a variável
              <code>ASAAS_API_KEY</code> no Railway por esta chave, e configurar a NF e o webhook na subconta.</div>"""
        except Exception as e:
            msg = f'<div class="erro">Erro ao criar subconta: {str(e)[:500]}</div>'

    # valores pré-preenchidos com os dados da Sorvezene (editáveis)
    d = {
        "name": "Repactua", "email": "", "cpfCnpj": "67.028.638/0001-01",
        "phone": "(51) 9019-2409", "mobilePhone": "(51) 9019-2409",
        "address": "Avenida Taquara", "addressNumber": "193", "complement": "",
        "province": "Petrópolis", "postalCode": "90460-210",
    }
    html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Criar subconta · Repactua</title><style>
    *{{box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',system-ui,sans-serif}}
    body{{background:#f4f6f9;color:#1c2b3a}} .wrap{{max-width:620px;margin:0 auto;padding:24px}}
    h1{{color:#1a3a5c;font-size:1.3rem;margin-bottom:4px}} .sub{{color:#5a6a7a;font-size:.85rem;margin-bottom:16px}}
    .card{{background:#fff;border-radius:12px;box-shadow:0 2px 14px rgba(0,0,0,.07);padding:22px}}
    label{{display:block;font-size:.74rem;text-transform:uppercase;color:#5a6a7a;font-weight:600;margin:12px 0 4px}}
    input,select{{width:100%;padding:10px 12px;border:1.5px solid #d0d7e2;border-radius:8px;font-size:.92rem;background:#fafbfd}}
    .row{{display:flex;gap:10px}} .row>div{{flex:1}}
    .btn{{margin-top:18px;background:#c8960c;color:#fff;border:none;border-radius:8px;padding:12px 20px;font-weight:700;cursor:pointer;font-size:.95rem}}
    .ok{{background:#e9f7ee;color:#1b5e20;border:1px solid #7ec891;border-radius:8px;padding:12px 14px;font-size:.88rem;margin-bottom:14px}}
    .erro{{background:#fdecea;color:#7a2218;border:1px solid #e8a49a;border-radius:8px;padding:12px 14px;font-size:.85rem;margin-bottom:14px}}
    table td{{padding:6px 8px;border-bottom:1px solid #eef1f5;font-size:.85rem}}
    a{{color:#2c5f8a;text-decoration:none}} code{{background:#eef1f5;padding:1px 5px;border-radius:4px;font-size:.85rem}}</style></head>
    <body><div class="wrap">
    <a href="/admin">← Voltar ao painel</a>
    <h1 style="margin-top:10px">Criar subconta no Asaas</h1>
    <div class="sub">Cria uma subconta (POST /accounts) para separar o recebimento do Repactua. Confira os dados e informe um <b>e-mail diferente</b> do da conta-mãe.</div>
    {msg}
    <div class="card"><form method="post">
      <label>Nome da subconta</label><input name="name" value="{d['name']}" required>
      <label>E-mail (precisa ser diferente do e-mail da conta-mãe)</label><input name="email" type="email" placeholder="repactua@sorvezenetechnology.com.br" required>
      <label>Faturamento mensal médio (R$)</label><input name="incomeValue" type="number" step="0.01" value="5000" required>
      <div class="row">
        <div><label>CNPJ</label><input name="cpfCnpj" value="{d['cpfCnpj']}" required></div>
        <div><label>Tipo</label><select name="companyType">
          <option value="LIMITED" selected>LIMITED (Ltda)</option>
          <option value="MEI">MEI</option>
          <option value="INDIVIDUAL">INDIVIDUAL</option>
          <option value="ASSOCIATION">ASSOCIATION</option>
        </select></div>
      </div>
      <div class="row">
        <div><label>Telefone</label><input name="phone" value="{d['phone']}"></div>
        <div><label>Celular</label><input name="mobilePhone" value="{d['mobilePhone']}" required></div>
      </div>
      <label>CEP</label><input name="postalCode" value="{d['postalCode']}" required>
      <div class="row">
        <div style="flex:3"><label>Endereço</label><input name="address" value="{d['address']}" required></div>
        <div><label>Número</label><input name="addressNumber" value="{d['addressNumber']}" required></div>
      </div>
      <label>Complemento</label><input name="complement" value="{d['complement']}">
      <label>Bairro</label><input name="province" value="{d['province']}" required>
      <button class="btn" type="submit" onclick="return confirm('Criar a subconta no Asaas com estes dados?')">Criar subconta</button>
    </form></div></div></body></html>"""
    return Response(html, mimetype="text/html")


def _pagamentos_repactua():
    """Busca no Asaas os pagamentos do Repactua (descrição contém 'repactua')."""
    pags = []
    try:
        offset = 0
        while offset <= 200:  # até 300 pagamentos
            r = asaas("GET", f"/payments?limit=100&offset={offset}")
            data = r.get("data") or []
            pags.extend(data)
            if len(data) < 100:
                break
            offset += 100
    except Exception:
        return []
    return [p for p in pags if "repactua" in (p.get("description") or "").lower()]


def _mapa_clientes_asaas():
    """id -> nome dos clientes no Asaas (para exibir nos pagamentos)."""
    mapa = {}
    try:
        for offset in (0, 100):
            r = asaas("GET", f"/customers?limit=100&offset={offset}")
            data = r.get("data") or []
            for c in data:
                mapa[c.get("id")] = c.get("name") or c.get("email") or "—"
            if len(data) < 100:
                break
    except Exception:
        pass
    return mapa


@app.route("/admin/financeiro")
def admin_financeiro():
    """Painel financeiro: receita real (Asaas), MRR, gráfico e assinantes."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    orgs = Escritorio.query.order_by(Escritorio.criado_em.desc()).all()
    mrr = ativos = trials = inativos = 0
    linhas = ""
    pagantes = 0
    for o in orgs:
        valor = PLANOS.get(o.plano, {}).get("valor", 0)
        pago = bool(o.asaas_subscription_id)  # tem assinatura de verdade (não cortesia)
        if o.status == "ativo":
            ativos += 1
            if pago:
                mrr += valor
                pagantes += 1
        elif o.status == "trial":
            trials += 1
        else:
            inativos += 1
        dono = next((u for u in (o.usuarios or []) if u.papel == "dono"), None) or \
               (o.usuarios[0] if o.usuarios else None)
        nome = o.nome or (dono.nome if dono else "—")
        email = dono.email if dono else "—"
        plano_nome = PLANOS.get(o.plano, {}).get("nome", o.plano or "—")
        valor_fmt = ("R$ %.2f/mês" % valor).replace(".", ",")
        cadastro = (o.criado_em or datetime.utcnow()).strftime("%d/%m/%Y")
        cls = {"ativo": "b-ativo", "trial": "b-trial", "inativo": "b-inativo"}.get(o.status, "b-trial")
        sit = {"ativo": "Em dia", "trial": "Em teste", "inativo": "Inativo"}.get(o.status, o.status)
        tag_cortesia = '<br><small style="color:#c8960c">cortesia</small>' if (o.status == "ativo" and not pago) else ""
        contato = o.telefone or "—"
        cidade = (o.cidade + ("/" + o.uf if o.uf else "")) if o.cidade else "—"
        linhas += f"""<tr>
          <td><b>{nome}</b><br><small>{email}</small></td>
          <td><span class="badge {cls}">{sit}</span>{tag_cortesia}</td>
          <td>{valor_fmt}</td>
          <td>{plano_nome}</td>
          <td>{o.total_membros}/{o.max_membros}</td>
          <td>{contato}</td>
          <td>{cidade}</td>
          <td>{cadastro}</td></tr>"""
    mrr_fmt = ("R$ %.2f" % mrr).replace(".", ",")

    # ---- Receita REAL (pagamentos do Asaas com "Repactua" na descrição) ----
    def fmt_moeda(v):
        return ("R$ %.2f" % v).replace(".", ",")

    def _data_pag(p):
        return (p.get("clientPaymentDate") or p.get("paymentDate")
                or p.get("confirmedDate") or p.get("dueDate") or "")

    pags = _pagamentos_repactua()
    RECEBIDO = ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH")
    mes_atual = datetime.utcnow().strftime("%Y-%m")
    receb_mes = receb_total = a_receber = 0.0
    por_mes = {}
    for p in pags:
        v = float(p.get("value") or 0)
        st = p.get("status") or ""
        if st in RECEBIDO:
            receb_total += v
            mkey = _data_pag(p)[:7]
            por_mes[mkey] = por_mes.get(mkey, 0) + v
            if mkey == mes_atual:
                receb_mes += v
        elif st in ("PENDING", "OVERDUE"):
            a_receber += v

    # últimos 6 meses para o gráfico
    MESES_PT = ["jan", "fev", "mar", "abr", "mai", "jun",
                "jul", "ago", "set", "out", "nov", "dez"]
    hoje = date.today()
    labels, valores = [], []
    ano, mes = hoje.year, hoje.month
    chaves = []
    for _ in range(6):
        chaves.append((ano, mes))
        mes -= 1
        if mes == 0:
            mes, ano = 12, ano - 1
    for a, m in reversed(chaves):
        labels.append(f"{MESES_PT[m-1]}/{str(a)[2:]}")
        valores.append(round(por_mes.get(f"{a:04d}-{m:02d}", 0), 2))

    # pagamentos recentes (últimos 10)
    nomes = _mapa_clientes_asaas() if pags else {}
    STATUS_PT = {"RECEIVED": ("Recebido", "b-ativo"), "CONFIRMED": ("Confirmado", "b-ativo"),
                 "RECEIVED_IN_CASH": ("Recebido", "b-ativo"), "PENDING": ("Aguardando", "b-trial"),
                 "OVERDUE": ("Vencido", "b-inativo"), "REFUNDED": ("Estornado", "b-inativo")}
    FORMA_PT = {"PIX": "Pix", "BOLETO": "Boleto", "CREDIT_CARD": "Cartão", "UNDEFINED": "—"}
    pags_ord = sorted(pags, key=_data_pag, reverse=True)[:10]
    linhas_pag = ""
    for p in pags_ord:
        dt = _data_pag(p)
        dt_fmt = f"{dt[8:10]}/{dt[5:7]}/{dt[:4]}" if len(dt) >= 10 else "—"
        st_nome, st_cls = STATUS_PT.get(p.get("status") or "", (p.get("status") or "—", "b-trial"))
        linhas_pag += f"""<tr>
          <td>{dt_fmt}</td>
          <td>{nomes.get(p.get("customer"), "—")}</td>
          <td>{fmt_moeda(float(p.get("value") or 0))}</td>
          <td>{FORMA_PT.get(p.get("billingType") or "", p.get("billingType") or "—")}</td>
          <td><span class="badge {st_cls}">{st_nome}</span></td></tr>"""
    if not linhas_pag:
        linhas_pag = '<tr><td colspan="5" style="color:#8a97a5">Nenhum pagamento do Repactua encontrado no Asaas.</td></tr>'

    grafico_js = """
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
    <script>
    new Chart(document.getElementById('grafReceita'), {
      type: 'bar',
      data: { labels: %s, datasets: [{ label: 'Recebido (R$)', data: %s,
        backgroundColor: '#1a3a5c', hoverBackgroundColor: '#c8960c', borderRadius: 6 }] },
      options: { plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { callback: function(v){ return 'R$ ' + v; } } } } }
    });
    function filtrarTabela() {
      var q = document.getElementById('busca').value.toLowerCase();
      document.querySelectorAll('#tbAssinantes tr').forEach(function(tr) {
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
      });
    }
    </script>""" % (json.dumps(labels), json.dumps(valores))

    conteudo = f"""
      <h1>Financeiro</h1>
      <div class="sub">Receita real (Asaas) e situação das assinaturas</div>
      <div class="cards">
        <div class="mc verde"><div class="lbl">Recebido este mês</div><div class="val">{fmt_moeda(receb_mes)}</div></div>
        <div class="mc verde"><div class="lbl">Recebido (total)</div><div class="val">{fmt_moeda(receb_total)}</div></div>
        <div class="mc ouro"><div class="lbl">A receber</div><div class="val">{fmt_moeda(a_receber)}</div></div>
        <div class="mc"><div class="lbl">MRR (planos pagos)</div><div class="val">{mrr_fmt}</div></div>
      </div>
      <div class="cards">
        <div class="mc"><div class="lbl">Pagantes</div><div class="val">{pagantes}</div></div>
        <div class="mc"><div class="lbl">Em dia</div><div class="val">{ativos}</div></div>
        <div class="mc ouro"><div class="lbl">Em teste</div><div class="val">{trials}</div></div>
        <div class="mc rubi"><div class="lbl">Inativos</div><div class="val">{inativos}</div></div>
      </div>

      <div class="duas">
        <div>
          <h2>📈 Receita recebida por mês</h2>
          <div class="painel"><canvas id="grafReceita" height="210"></canvas></div>
        </div>
        <div>
          <h2>💸 Últimos pagamentos</h2>
          <table><thead><tr><th>Data</th><th>Cliente</th><th>Valor</th><th>Forma</th><th>Status</th></tr></thead>
          <tbody>{linhas_pag}</tbody></table>
        </div>
      </div>

      <h2>👥 Assinantes</h2>
      <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <input class="busca" id="busca" onkeyup="filtrarTabela()" placeholder="🔍 Buscar por nome, e-mail, cidade...">
        <a href="/admin/export/pagamentos.csv" style="background:#1a3a5c;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-size:.82rem;font-weight:700">⬇ Pagamentos (CSV)</a>
      </div>
      <table><thead><tr><th>Assinante</th><th>Situação</th><th>Valor</th><th>Plano</th><th>Membros</th><th>Contato</th><th>Cidade</th><th>Cadastro</th></tr></thead>
      <tbody id="tbAssinantes">{linhas}</tbody></table>
      <p class="nota">Recebido/A receber = pagamentos reais do Asaas com "Repactua" na descrição. MRR = soma dos planos <b>pagos</b> ativos (cortesias não entram). "Em dia" conta todos os ativos, inclusive cortesias.</p>"""
    return _admin_page("Financeiro", conteudo, "fin", grafico_js)


@app.route("/admin/configurar-webhook", methods=["GET", "POST"])
def admin_config_webhook():
    """Confirma a conta Asaas ativa e cria/lista o webhook (usa o ASAAS_API_KEY atual)."""
    if not _admin_logado():
        return redirect(url_for("admin_login"))
    msg = ""
    if request.method == "POST":
        try:
            try:
                _em = asaas("GET", "/myAccount").get("email") or ADMIN_EMAIL
            except Exception:
                _em = ADMIN_EMAIL
            r = asaas("POST", "/webhooks", {
                "name": "Repactua",
                "url": "https://repactua.com.br/api/asaas-webhook",
                "email": _em,
                "enabled": True,
                "interrupted": False,
                "authToken": ASAAS_WEBHOOK_TOKEN,
                "sendType": "SEQUENTIALLY",
                "events": ["PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_OVERDUE",
                           "PAYMENT_DELETED", "PAYMENT_REFUNDED"],
            })
            msg = f'<div class="ok"><b>Webhook configurado!</b> id: {r.get("id","")}</div>'
        except Exception as e:
            msg = f'<div class="erro">Erro ao criar webhook: {str(e)[:400]}</div>'

    # confirma a conta ativa e lista webhooks existentes
    try:
        conta = asaas("GET", "/myAccount")
    except Exception:
        conta = {}
    nome = conta.get("name") or conta.get("companyName") or "?"
    email = conta.get("email") or "?"
    wallet = conta.get("walletId") or "?"
    try:
        whs = asaas("GET", "/webhooks").get("data") or []
    except Exception:
        whs = []
    lista_wh = "".join(f'<li>{w.get("url")} · {"ativo" if w.get("enabled") else "inativo"}</li>' for w in whs) or "<li>(nenhum)</li>"

    html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Webhook · Repactua</title><style>
    body{{font-family:'Segoe UI',sans-serif;background:#f4f6f9;color:#1c2b3a;padding:24px}}
    .wrap{{max-width:620px;margin:0 auto}} h1{{color:#1a3a5c;font-size:1.3rem}}
    .card{{background:#fff;border-radius:12px;box-shadow:0 2px 14px rgba(0,0,0,.07);padding:22px;margin-top:14px}}
    .ok{{background:#e9f7ee;color:#1b5e20;border:1px solid #7ec891;border-radius:8px;padding:12px;margin-bottom:12px;font-size:.88rem}}
    .erro{{background:#fdecea;color:#7a2218;border:1px solid #e8a49a;border-radius:8px;padding:12px;margin-bottom:12px;font-size:.85rem}}
    .btn{{background:#c8960c;color:#fff;border:none;border-radius:8px;padding:12px 20px;font-weight:700;cursor:pointer;margin-top:8px}}
    a{{color:#2c5f8a;text-decoration:none}} table td{{padding:5px 8px;font-size:.9rem}} li{{font-size:.85rem;margin:3px 0}}</style></head>
    <body><div class="wrap"><a href="/admin">← Voltar ao painel</a>
    <h1 style="margin-top:8px">Configurar webhook da conta Asaas</h1>
    {msg}
    <div class="card">
      <b>Conta Asaas atual (pela ASAAS_API_KEY):</b>
      <table><tr><td>Nome</td><td><b>{nome}</b></td></tr>
      <tr><td>E-mail</td><td>{email}</td></tr>
      <tr><td>Wallet</td><td style="font-family:monospace;font-size:.82rem">{wallet}</td></tr></table>
      <p style="font-size:.82rem;color:#5a6a7a;margin-top:8px">Confirme que é a subconta do Repactua (não a conta-mãe).</p>
    </div>
    <div class="card">
      <b>Webhooks cadastrados nesta conta:</b><ul>{lista_wh}</ul>
      <form method="post"><button class="btn" type="submit">Criar/registrar webhook do Repactua</button></form>
      <p style="font-size:.8rem;color:#5a6a7a;margin-top:8px">Aponta para https://repactua.com.br/api/asaas-webhook com o token já configurado.</p>
    </div></div></body></html>"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n⚠️  ANTHROPIC_API_KEY não definida. A leitura por IA não vai funcionar.\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
