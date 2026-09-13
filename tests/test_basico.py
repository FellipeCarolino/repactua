# Smoke-tests do Repactua — protegem o essencial contra regressões.
# Rodar: pytest -q  (usa SQLite local; não toca no Postgres de produção)
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "chave-de-teste")
os.environ.setdefault("SECRET_KEY", "segredo-de-teste")

import server  # noqa: E402  (importa o app com SQLite)


# ---------- Validadores de cadastro ----------
def test_cpf_valido():
    assert server._cpf_valido("529.982.247-25")
    assert server._cpf_valido("11144477735")


def test_cpf_invalido():
    assert not server._cpf_valido("123.456.789-00")
    assert not server._cpf_valido("111.111.111-11")  # dígitos repetidos
    assert not server._cpf_valido("123")


def test_cnpj():
    assert server._cnpj_valido("11.222.333/0001-81")
    assert not server._cnpj_valido("11.222.333/0001-00")


def test_documento_valido():
    assert server._documento_valido("529.982.247-25")       # CPF
    assert server._documento_valido("11.222.333/0001-81")   # CNPJ
    assert not server._documento_valido("000")


def test_email():
    assert server._email_valido("a@b.com")
    assert not server._email_valido("sem-arroba")
    assert server._dominio_descartavel("x@mailinator.com")
    assert not server._dominio_descartavel("x@gmail.com")


def test_fmt_documento():
    assert "CPF" in server._fmt_documento("52998224725")
    assert "CNPJ" in server._fmt_documento("11222333000181")


# ---------- Regras de acesso (trial/ativo) ----------
def test_trial_expirado_vira_inativo():
    with server.app.app_context():
        org = server.Escritorio(nome="T", plano="individual", status="trial",
                                acesso_ate=date.today() - timedelta(days=1))
        u = server.User(email="t@t.com", senha_hash="x")
        u.org = org
        assert u.status_efetivo == "inativo"
        org.acesso_ate = date.today() + timedelta(days=3)
        assert u.status_efetivo == "trial"
        org.status = "ativo"
        org.acesso_ate = None  # cortesia vitalícia
        assert u.status_efetivo == "ativo"


# ---------- Endpoints críticos ----------
def _client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_health_ok():
    r = _client().get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_cabecalhos_de_seguranca():
    r = _client().get("/login")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Referrer-Policy" in r.headers


def test_admin_acao_sem_login_redireciona():
    # ação mutante sem login/token NUNCA pode executar nem dar 500
    r = _client().get("/admin/status/1/inativo")
    assert r.status_code == 302
    assert "/admin/login" in r.headers.get("Location", "")
    r2 = _client().post("/admin/excluir/1")
    assert r2.status_code == 302


def test_signup_rejeita_cpf_invalido():
    r = _client().post("/signup", data={
        "nome": "X", "documento": "123.456.789-00",
        "email": "novo@example.com", "senha": "123456", "aceite": "on",
    })
    assert "CPF ou CNPJ" in r.get_data(as_text=True)
    with server.app.app_context():
        assert server.User.query.filter_by(email="novo@example.com").first() is None


# ---------- Webhook do Asaas ----------
import uuid  # noqa: E402


def _org_teste(status, com_assinatura=True, acesso=None):
    suf = uuid.uuid4().hex[:10]
    sub = ("sub_" + suf) if com_assinatura else None
    with server.app.app_context():
        org = server.Escritorio(nome="W" + suf, plano="individual", status=status,
                                asaas_customer_id="cus_" + suf, asaas_subscription_id=sub,
                                acesso_ate=acesso)
        server.db.session.add(org)
        server.db.session.commit()
        return org.id, "cus_" + suf, sub


def _hook(tipo, cust, pay_id, sub):
    return _client().post("/api/asaas-webhook", json={"event": tipo, "payment": {
        "id": pay_id, "customer": cust, "subscription": sub, "value": 129.9}})


def _estado(oid):
    with server.app.app_context():
        o = server.db.session.get(server.Escritorio, oid)
        return o.status, o.acesso_ate


def test_webhook_pagamento_ativa_uma_vez_so():
    oid, cus, sub = _org_teste("trial")
    pay = "pay_" + uuid.uuid4().hex[:10]
    assert _hook("PAYMENT_CONFIRMED", cus, pay, sub).status_code == 200
    assert _estado(oid) == ("ativo", date.today() + timedelta(days=37))
    with server.app.app_context():  # simula o tempo passando
        server.db.session.get(server.Escritorio, oid).acesso_ate = date.today() + timedelta(days=5)
        server.db.session.commit()
    _hook("PAYMENT_RECEIVED", cus, pay, sub)  # mesmo pagamento, 32 dias depois (cartão)
    assert _estado(oid) == ("ativo", date.today() + timedelta(days=5))  # não renovou de novo


def test_webhook_exclusao_de_cobranca_nao_inativa():
    oid, cus, sub = _org_teste("ativo", acesso=date.today() + timedelta(days=20))
    _hook("PAYMENT_DELETED", cus, "pay_" + uuid.uuid4().hex[:10], sub)
    assert _estado(oid)[0] == "ativo"


def test_webhook_cortesia_blindada():
    oid, cus, _ = _org_teste("ativo", com_assinatura=False, acesso=None)
    for tipo in ("PAYMENT_OVERDUE", "PAYMENT_REFUNDED", "PAYMENT_DELETED", "PAYMENT_CONFIRMED"):
        _hook(tipo, cus, "pay_" + uuid.uuid4().hex[:10], "sub_antiga")
    assert _estado(oid) == ("ativo", None)  # continua vitalícia, sem data de expiração


def test_webhook_atraso_so_da_assinatura_vigente():
    oid, cus, sub = _org_teste("ativo", acesso=date.today() + timedelta(days=20))
    _hook("PAYMENT_OVERDUE", cus, "pay_" + uuid.uuid4().hex[:10], "sub_de_outra_epoca")
    assert _estado(oid)[0] == "ativo"
    _hook("PAYMENT_OVERDUE", cus, "pay_" + uuid.uuid4().hex[:10], sub)
    assert _estado(oid)[0] == "inativo"
