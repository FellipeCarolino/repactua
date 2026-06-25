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
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, request, jsonify, send_from_directory, redirect, url_for, Response
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

# --- Nota fiscal automática (NFS-e via Asaas) ---
NF_AUTO = os.environ.get("NF_AUTO", "0") == "1"
NF_SERVICO_CODIGO = os.environ.get("NF_SERVICO_CODIGO", "")   # código do serviço municipal
NF_SERVICO_NOME = os.environ.get("NF_SERVICO_NOME", "")       # descrição do serviço
NF_ISS = float(os.environ.get("NF_ISS", "0") or 0)            # alíquota de ISS (%)
NF_RETER_ISS = os.environ.get("NF_RETER_ISS", "0") == "1"
NF_DEDUCOES = float(os.environ.get("NF_DEDUCOES", "0") or 0)
NF_OBSERVACOES = os.environ.get("NF_OBSERVACOES", "")
NF_QUANDO = os.environ.get("NF_QUANDO", "ON_PAYMENT_CONFIRMATION")

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
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    senha_hash = db.Column(db.String(255), nullable=False)
    nome = db.Column(db.String(255))
    escritorio = db.Column(db.String(255))
    oab = db.Column(db.String(60))
    status = db.Column(db.String(20), default="trial")  # trial | ativo | inativo
    is_admin = db.Column(db.Boolean, default=False)
    usage_mes = db.Column(db.String(7))   # "AAAA-MM"
    usage_contagem = db.Column(db.Integer, default=0)
    asaas_customer_id = db.Column(db.String(120))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha, method="pbkdf2:sha256")

    def conferir_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    @property
    def limite_mensal(self):
        return LIMITE_POR_STATUS.get(self.status, 0)

    def _mes_atual(self):
        return datetime.utcnow().strftime("%Y-%m")

    def consultas_restantes(self):
        if self.usage_mes != self._mes_atual():
            return self.limite_mensal
        return max(self.limite_mensal - (self.usage_contagem or 0), 0)

    def pode_consultar(self):
        return self.status in ("trial", "ativo") and self.consultas_restantes() > 0

    def registrar_consulta(self):
        mes = self._mes_atual()
        if self.usage_mes != mes:
            self.usage_mes = mes
            self.usage_contagem = 0
        self.usage_contagem = (self.usage_contagem or 0) + 1
        db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()


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
    "Você é um analista jurídico-financeiro especializado em folhas de pagamento brasileiras. "
    "Leia o documento e extraia os dados para análise de superendividamento. "
    "Separe proventos de descontos. 'renda_bruta' = soma dos proventos brutos. "
    "'inss' = INSS/PSS/previdência. 'outros_descontos' = descontos obrigatórios que não sejam INSS, IRRF, pensão ou consignados. "
    "'consignados' = empréstimos/cartões consignados em folha (liste credor e parcela). "
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
    if current_user.status == "inativo":
        return False, "Sua assinatura está inativa. Regularize o pagamento para usar a leitura por IA."
    if current_user.consultas_restantes() <= 0:
        return False, f"Você atingiu o limite de {current_user.limite_mensal} consultas neste mês."
    return True, None


# ============================================================
# Páginas (HTML simples, embutido)
# ============================================================
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
    <div class="link">Ainda não tem conta? <a href="/signup">Criar conta</a></div>"""
    return _pagina_auth("Entrar", corpo)


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
            user = User(email=email, nome=nome, escritorio=escritorio, status="trial")
            user.set_senha(senha)
            if email == ADMIN_EMAIL:
                user.is_admin = True
                user.status = "ativo"
            db.session.add(user)
            db.session.commit()
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
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/me")
@login_required
def api_me():
    return jsonify({
        "nome": current_user.nome, "email": current_user.email,
        "status": current_user.status, "is_admin": current_user.is_admin,
        "limite": current_user.limite_mensal,
        "restantes": current_user.consultas_restantes(),
    })


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
    if current_user.status == "ativo":
        return redirect(url_for("index"))
    corpo = f"""<h2>Assinar o Repactua</h2>
    <div class="sub">Plano Profissional · R$ {('%.2f' % PLANO_VALOR).replace('.', ',')}/mês · 50 consultas de IA por mês.</div>
    <form method="post">
      <label>Nome / Razão social</label><input name="nome" value="{(current_user.nome or '').replace('"','')}" required>
      <label>CPF ou CNPJ (do pagador)</label><input name="cpfCnpj" required placeholder="somente números">
      <label>E-mail</label><input type="email" value="{current_user.email}" disabled style="opacity:.7">
      <button class="btn" type="submit">Ir para o pagamento →</button>
    </form>
    <p style="font-size:.8rem;color:#5a6a7a;margin-top:14px">Você escolhe Pix, boleto ou cartão na próxima tela (Asaas). A conta é ativada automaticamente após a confirmação do pagamento.</p>
    <div class="link"><a href="/">← Voltar</a></div>"""
    return _pagina_auth("Assinar", corpo)


@app.route("/assinar", methods=["POST"])
@login_required
def assinar_post():
    nome = (request.form.get("nome") or current_user.nome or current_user.email).strip()
    cpf = "".join(filter(str.isalnum, request.form.get("cpfCnpj") or ""))
    try:
        if not current_user.asaas_customer_id:
            cliente = asaas("POST", "/customers",
                            {"name": nome, "email": current_user.email, "cpfCnpj": cpf})
            current_user.asaas_customer_id = cliente.get("id")
            if nome and not current_user.nome:
                current_user.nome = nome
            db.session.commit()
        assinatura = asaas("POST", "/subscriptions", {
            "customer": current_user.asaas_customer_id,
            "billingType": "UNDEFINED",
            "value": PLANO_VALOR,
            "nextDueDate": date.today().isoformat(),
            "cycle": "MONTHLY",
            "description": PLANO_DESC,
        })
        # Configura emissão automática de nota fiscal para a assinatura (se ativado)
        if NF_AUTO and (NF_SERVICO_CODIGO or NF_SERVICO_NOME):
            cfg_nf = {
                "deductions": NF_DEDUCOES,
                "effectiveDatePeriod": NF_QUANDO,
                "receivedOnly": True,
                "observations": NF_OBSERVACOES or PLANO_DESC,
                "taxes": {"retainIss": NF_RETER_ISS, "iss": NF_ISS,
                          "cofins": 0, "csll": 0, "inss": 0, "ir": 0, "pis": 0},
            }
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
    user = None
    if cust_id:
        user = User.query.filter_by(asaas_customer_id=cust_id).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()
    if user:
        if tipo in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
            user.status = "ativo"
            if cust_id and not user.asaas_customer_id:
                user.asaas_customer_id = cust_id
        elif tipo in ("PAYMENT_OVERDUE", "PAYMENT_DELETED", "PAYMENT_REFUNDED", "SUBSCRIPTION_DELETED"):
            user.status = "inativo"
        db.session.commit()
    return jsonify({"ok": True})


# ============================================================
# Admin (gestão simples de assinantes)
# ============================================================
@app.route("/admin")
@login_required
def admin():
    if not current_user.is_admin:
        return redirect(url_for("index"))
    users = User.query.order_by(User.criado_em.desc()).all()
    linhas = ""
    for u in users:
        linhas += f"""<tr>
          <td>{u.nome or '—'}<br><small>{u.email}</small></td>
          <td>{u.escritorio or '—'}</td>
          <td><b>{u.status}</b></td>
          <td>{(u.usage_contagem or 0) if u.usage_mes == datetime.utcnow().strftime('%Y-%m') else 0}/{u.limite_mensal}</td>
          <td>
            <a href="/admin/status/{u.id}/ativo">ativar</a> ·
            <a href="/admin/status/{u.id}/inativo">inativar</a> ·
            <a href="/admin/status/{u.id}/trial">trial</a>
          </td></tr>"""
    html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
    <title>Admin · Assinantes</title><style>
    body{{font-family:'Segoe UI',sans-serif;background:#f4f6f9;padding:24px;color:#1c2b3a}}
    h1{{color:#1a3a5c}} table{{width:100%;border-collapse:collapse;background:#fff;margin-top:16px;box-shadow:0 2px 12px rgba(0,0,0,.08);border-radius:8px;overflow:hidden}}
    th{{background:#1a3a5c;color:#fff;padding:10px;text-align:left;font-size:.8rem}} td{{padding:10px;border-bottom:1px solid #eee;font-size:.9rem}}
    a{{color:#2c5f8a;text-decoration:none;font-size:.82rem}} small{{color:#888}}
    .voltar{{display:inline-block;margin-bottom:12px;color:#2c5f8a;text-decoration:none}}</style></head><body>
    <a class="voltar" href="/">← Voltar à calculadora</a>
    <h1>Assinantes ({len(users)})</h1>
    <table><thead><tr><th>Advogado</th><th>Escritório</th><th>Status</th><th>Uso/mês</th><th>Ações</th></tr></thead>
    <tbody>{linhas}</tbody></table></body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/admin/status/<int:uid>/<novo>")
@login_required
def admin_status(uid, novo):
    if not current_user.is_admin or novo not in ("ativo", "inativo", "trial"):
        return redirect(url_for("index"))
    u = db.session.get(User, uid)
    if u:
        u.status = novo
        db.session.commit()
    return redirect(url_for("admin"))


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n⚠️  ANTHROPIC_API_KEY não definida. A leitura por IA não vai funcionar.\n")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
