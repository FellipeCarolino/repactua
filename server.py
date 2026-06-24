"""
Backend de leitura inteligente de documentos para a Calculadora de Superendividamento.

- Serve a calculadora (index.html) e seus arquivos estáticos.
- Expõe dois endpoints que usam a IA de visão da Claude para ler documentos
  (holerite/contracheque e contratos de dívida) e devolver os campos já
  estruturados, prontos para preencher a calculadora.

A chave de API fica somente aqui no servidor (variável de ambiente
ANTHROPIC_API_KEY) e nunca é exposta ao navegador.

Como rodar:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="sua-chave-aqui"   # no Windows: set ANTHROPIC_API_KEY=...
    python server.py
    # abra http://localhost:5000
"""

import base64
import json
import os

from flask import Flask, request, jsonify, send_from_directory
import anthropic

MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente

# Tipos de dívida aceitos pela calculadora (mantém em sincronia com TIPOS_DIVIDA no index.html)
TIPOS_DIVIDA = [
    "cartao", "cheque", "emprestimo", "consignado",
    "financiamento_imovel", "financiamento_veiculo", "aluguel", "alimentos",
    "condominio", "fiscal", "energia", "saude", "educacao", "outro",
]

# ----------------------------------------------------------------------------
# Esquemas de saída estruturada (JSON Schema)
# ----------------------------------------------------------------------------
SCHEMA_HOLERITE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nome": {"type": ["string", "null"], "description": "Nome do servidor/empregado"},
        "renda_bruta": {"type": ["number", "null"], "description": "Total de proventos/vencimentos brutos, em reais"},
        "inss": {"type": ["number", "null"], "description": "Desconto de INSS/PSS/previdência, em reais"},
        "irrf": {"type": ["number", "null"], "description": "Imposto de renda retido na fonte, em reais"},
        "pensao": {"type": ["number", "null"], "description": "Pensão alimentícia descontada em folha, em reais"},
        "outros_descontos": {"type": ["number", "null"], "description": "Soma dos demais descontos obrigatórios (exceto INSS, IRRF, pensão e consignados), em reais"},
        "consignados_total": {"type": ["number", "null"], "description": "Soma de todas as parcelas de empréstimos/cartões consignados descontadas em folha, em reais"},
        "consignados": {
            "type": "array",
            "description": "Lista de cada empréstimo/cartão consignado identificado na folha",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "credor": {"type": ["string", "null"]},
                    "parcela": {"type": ["number", "null"], "description": "Valor da parcela mensal em reais"},
                },
                "required": ["credor", "parcela"],
            },
        },
        "observacoes": {"type": ["string", "null"], "description": "Observações relevantes (ex.: rubricas que não foi possível classificar)"},
    },
    "required": [
        "nome", "renda_bruta", "inss", "irrf", "pensao",
        "outros_descontos", "consignados_total", "consignados", "observacoes",
    ],
}

SCHEMA_CONTRATO = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "credor": {"type": ["string", "null"], "description": "Nome do credor/instituição financeira"},
        "tipo": {"type": "string", "enum": TIPOS_DIVIDA, "description": "Tipo da dívida que melhor descreve o contrato"},
        "saldo_devedor": {"type": ["number", "null"], "description": "Saldo devedor atual/total em aberto, em reais"},
        "parcela_mensal": {"type": ["number", "null"], "description": "Valor da parcela mensal, em reais"},
        "parcelas_contratadas": {"type": ["integer", "null"], "description": "Número total de parcelas contratadas"},
        "parcelas_pagas": {"type": ["integer", "null"], "description": "Número de parcelas já pagas/quitadas"},
        "em_folha": {"type": ["boolean", "null"], "description": "true se for desconto consignado em folha de pagamento"},
        "taxa_juros": {"type": ["string", "null"], "description": "Taxa de juros do contrato, se informada (ex.: '2,5% a.m.')"},
        "observacoes": {"type": ["string", "null"], "description": "Cláusulas relevantes (juros abusivos, seguros embutidos, tarifas, garantias)"},
    },
    "required": [
        "credor", "tipo", "saldo_devedor", "parcela_mensal",
        "parcelas_contratadas", "parcelas_pagas", "em_folha", "taxa_juros", "observacoes",
    ],
}

PROMPT_HOLERITE = (
    "Você é um analista jurídico-financeiro especializado em folhas de pagamento brasileiras "
    "(contracheques de servidores públicos — SIAPE/estaduais/municipais — e da iniciativa privada). "
    "Leia o documento anexado e extraia os dados para análise de superendividamento.\n\n"
    "Regras:\n"
    "- Separe PROVENTOS (vencimentos, gratificações, auxílios, adicionais) dos DESCONTOS.\n"
    "- 'renda_bruta' = soma de todos os proventos brutos.\n"
    "- 'inss' = contribuição previdenciária (INSS, PSS, RPPS).\n"
    "- 'outros_descontos' = descontos obrigatórios que NÃO sejam INSS, IRRF, pensão alimentícia ou consignados.\n"
    "- 'consignados' = empréstimos e cartões consignados descontados em folha; liste cada um com credor e parcela.\n"
    "- Valores monetários SEMPRE em reais como número decimal (ex.: 5800.50), sem 'R$' nem separador de milhar.\n"
    "- Se um campo não existir no documento, retorne null.\n"
    "- Não invente valores: extraia apenas o que está no documento."
)

PROMPT_CONTRATO = (
    "Você é um analista jurídico-financeiro especializado em contratos de crédito brasileiros "
    "(empréstimo pessoal, consignado, financiamento de veículo/imóvel, cartão de crédito, cédula de crédito bancário). "
    "Leia o contrato anexado e extraia os dados da dívida para análise de superendividamento.\n\n"
    "Regras:\n"
    "- 'tipo' deve ser o valor da lista que melhor descreve o contrato.\n"
    "- 'saldo_devedor' = total em aberto / saldo devedor atual, em reais.\n"
    "- 'parcela_mensal' = valor da prestação mensal, em reais.\n"
    "- 'parcelas_contratadas' e 'parcelas_pagas' = números inteiros, se informados.\n"
    "- 'em_folha' = true se for consignado descontado em folha.\n"
    "- Valores monetários em reais como número decimal (ex.: 28000.00), sem 'R$' nem separador de milhar.\n"
    "- Em 'observacoes', registre indícios de abusividade (juros muito acima do mercado, venda casada de seguro, tarifas não pactuadas).\n"
    "- Se um campo não existir, retorne null. Não invente dados."
)


def _content_block_for_upload(file_storage):
    """Monta o bloco de conteúdo (documento PDF ou imagem) para a API a partir do upload."""
    raw = file_storage.read()
    if not raw:
        raise ValueError("Arquivo vazio.")
    filename = (file_storage.filename or "").lower()
    mimetype = (file_storage.mimetype or "").lower()
    data = base64.standard_b64encode(raw).decode("utf-8")

    is_pdf = filename.endswith(".pdf") or "pdf" in mimetype
    if is_pdf:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        }

    # Imagem
    if filename.endswith(".png") or "png" in mimetype:
        media = "image/png"
    elif filename.endswith(".webp") or "webp" in mimetype:
        media = "image/webp"
    elif filename.endswith(".gif") or "gif" in mimetype:
        media = "image/gif"
    else:
        media = "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": data},
    }


def _extrair(prompt, schema, file_storage):
    """Envia o documento à Claude e devolve o JSON estruturado."""
    bloco = _content_block_for_upload(file_storage)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": [bloco, {"type": "text", "text": prompt}]}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("A análise foi recusada por política de segurança.")
    texto = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(texto)


# ----------------------------------------------------------------------------
# Rotas
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/extract-holerite", methods=["POST"])
def extract_holerite():
    if "file" not in request.files:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    try:
        dados = _extrair(PROMPT_HOLERITE, SCHEMA_HOLERITE, request.files["file"])
        return jsonify({"ok": True, "dados": dados})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/extract-contrato", methods=["POST"])
def extract_contrato():
    if "file" not in request.files:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    try:
        dados = _extrair(PROMPT_CONTRATO, SCHEMA_CONTRATO, request.files["file"])
        return jsonify({"ok": True, "dados": dados})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "model": MODEL, "tem_chave": bool(os.environ.get("ANTHROPIC_API_KEY"))})


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n⚠️  ANTHROPIC_API_KEY não definida. A leitura por IA não vai funcionar.")
        print("    Defina com:  export ANTHROPIC_API_KEY=\"sua-chave\"\n")
    # PORT é injetada pelo host (Render/Railway); localmente cai em 5000.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
