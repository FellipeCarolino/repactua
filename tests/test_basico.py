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


# ---------- Minha conta: senha, sessões, LGPD e upload ----------
import io  # noqa: E402

SENHA_OK = "senhaforte1"


def _usuario_teste(admin=False):
    suf = uuid.uuid4().hex[:10]
    with server.app.app_context():
        org = server.Escritorio(nome="C" + suf, plano="individual", status="ativo")
        server.db.session.add(org)
        server.db.session.flush()
        u = server.User(email=f"u{suf}@example.com", nome="Teste", org_id=org.id, papel="dono",
                        is_admin=admin, email_confirmado=True)
        u.set_senha(SENHA_OK)
        server.db.session.add(u)
        server.db.session.flush()
        server.db.session.add(server.Caso(org_id=org.id, user_id=u.id, nome="Caso " + suf,
                                          payload='{"dados": {"nome": "Cliente"}}'))
        server.db.session.commit()
        return u.id, u.email, org.id


def _logado(uid):
    """Cliente de teste já autenticado (sem passar pelo /login e seu rate-limit)."""
    c = _client()
    with server.app.app_context():
        gid = server.db.session.get(server.User, uid).get_id()
    with c.session_transaction() as s:
        s["_user_id"] = gid
        s["_fresh"] = True
    return c


def _senha_confere(uid, senha):
    with server.app.app_context():
        return server.db.session.get(server.User, uid).conferir_senha(senha)


def test_senha_minima_8():
    uid, _, _ = _usuario_teste()
    r = _logado(uid).post("/conta/senha", data={"senha": "curta12"})  # 7 caracteres
    assert "mínimo 8" in r.get_data(as_text=True)
    assert _senha_confere(uid, SENHA_OK)  # não trocou


def test_trocar_senha_derruba_outras_sessoes():
    uid, _, _ = _usuario_teste()
    aqui, outro_aparelho = _logado(uid), _logado(uid)
    assert aqui.post("/conta/senha", data={"senha": "novasenha99"}).status_code == 200
    assert outro_aparelho.get("/conta").status_code == 302  # caiu → vai pro login
    assert aqui.get("/conta").status_code == 200             # quem trocou continua logado
    assert _senha_confere(uid, "novasenha99")


def test_exportar_meus_dados():
    uid, email, _ = _usuario_teste()
    r = _logado(uid).get("/conta/exportar")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")
    dados = r.get_json(force=True)
    assert dados["titular"]["email"] == email
    assert len(dados["casos"]) == 1


def test_excluir_minha_conta():
    uid, _, oid = _usuario_teste()
    c = _logado(uid)
    c.post("/conta/excluir", data={"senha": SENHA_OK, "confirmacao": "nao"})  # sem confirmar
    c.post("/conta/excluir", data={"senha": "errada", "confirmacao": "EXCLUIR"})  # senha errada
    with server.app.app_context():
        assert server.db.session.get(server.User, uid) is not None
    r = c.post("/conta/excluir", data={"senha": SENHA_OK, "confirmacao": "EXCLUIR"})
    assert "Conta excluída" in r.get_data(as_text=True)
    with server.app.app_context():
        assert server.db.session.get(server.User, uid) is None
        assert server.db.session.get(server.Escritorio, oid) is None
        assert server.Caso.query.filter_by(org_id=oid).count() == 0


def test_admin_nao_se_exclui_pela_conta():
    uid, _, _ = _usuario_teste(admin=True)
    _logado(uid).post("/conta/excluir", data={"senha": SENHA_OK, "confirmacao": "EXCLUIR"})
    with server.app.app_context():
        assert server.db.session.get(server.User, uid) is not None


def test_upload_acima_do_limite_da_413_amigavel():
    uid, _, _ = _usuario_teste()
    grande = io.BytesIO(b"0" * (26 * 1024 * 1024))
    r = _logado(uid).post("/api/extract-holerite", data={"file": (grande, "grande.pdf")},
                          content_type="multipart/form-data")
    assert r.status_code == 413
    assert "muito grande" in r.get_json()["erro"]
