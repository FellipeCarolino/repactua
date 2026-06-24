# Calculadora de Superendividamento — com leitura inteligente de documentos

Calculadora jurídico-financeira (Lei 14.181/2021) com **leitura inteligente por IA**
de holerites/contracheques e contratos de dívida.

## Como funciona

- **Frontend** (`index.html`): a calculadora — funciona sozinha no navegador.
- **Backend** (`server.py`): servidor Python que serve a calculadora e expõe a leitura
  por IA. A chave de API fica **só no servidor**, nunca no navegador.

A IA usada é a **Claude (Anthropic)**, com visão — lê PDF, foto e documento escaneado.

## Pré-requisitos

- Python 3.9+ instalado.
- Uma chave de API da Anthropic: https://console.anthropic.com/ → **API Keys**.

## Passo a passo

```bash
# 1. Instalar as dependências
pip install -r requirements.txt

# 2. Definir a chave de API (no terminal da sessão)
export ANTHROPIC_API_KEY="sua-chave-aqui"
#   Windows (PowerShell):  $env:ANTHROPIC_API_KEY="sua-chave-aqui"
#   Windows (cmd):         set ANTHROPIC_API_KEY=sua-chave-aqui

# 3. Iniciar o servidor
python server.py

# 4. Abrir no navegador
#    http://localhost:5000
```

## Usando a leitura por IA

- **Holerite**: no card "Renda", clique em **"📎 Ler holerite com IA"** e selecione o
  PDF/foto do contracheque. Os campos de renda, descontos e consignados são preenchidos
  automaticamente.
- **Contrato de dívida**: em cada dívida, clique em **"📎 Ler contrato com IA"** e selecione
  o contrato. Credor, tipo, saldo, parcela e número de parcelas são preenchidos.

> Sem o servidor rodando (abrindo o `index.html` direto), a calculadora funciona normalmente,
> mas a leitura por IA fica indisponível — é só preencher os campos à mão.

## Custos

Cada documento lido custa alguns centavos de uso da API da Anthropic
(modelo `claude-opus-4-8`). O custo é cobrado na sua conta da Anthropic.

## Privacidade

Os documentos são enviados ao backend e processados pela API da Anthropic apenas para a
extração dos campos. Não são armazenados pelo servidor. Para uso comercial com dados de
clientes, revise a política de privacidade e os termos de uso aplicáveis.
