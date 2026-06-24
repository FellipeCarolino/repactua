# Como colocar o backend no ar (deploy no Render — grátis)

O frontend (`index.html`) pode ficar em qualquer lugar, mas a **leitura por IA** precisa
do `server.py` rodando num host que execute Python. O **Render** faz isso de graça e
guarda sua chave de API com segurança. Leva ~10 minutos.

O projeto já está preparado (`render.yaml`, `Procfile`, `requirements.txt`, `.gitignore`).

## Passo 1 — Criar contas (se ainda não tiver)

1. **GitHub**: https://github.com/signup (grátis) — vai guardar o código.
2. **Render**: https://render.com → "Get Started" → entre **com o GitHub** (mais fácil).

## Passo 2 — Subir o código para o GitHub

No terminal, dentro da pasta `Calculadora Web`:

```bash
git init
git add .
git commit -m "Calculadora de superendividamento com leitura por IA"
git branch -M main
```

Crie um repositório novo (vazio) em https://github.com/new — dê um nome como
`calculadora-superendividamento` e **NÃO** marque "Add a README". Depois:

```bash
git remote add origin https://github.com/SEU_USUARIO/calculadora-superendividamento.git
git push -u origin main
```

> O `.gitignore` já impede que a sua chave de API ou documentos de teste sejam enviados.

## Passo 3 — Criar o serviço no Render

1. No painel do Render: **New +** → **Blueprint**.
2. Selecione o repositório que você acabou de subir.
3. O Render lê o `render.yaml` e mostra o serviço **calculadora-superendividamento**. Clique **Apply**.

## Passo 4 — Colocar a chave de API

1. Quando pedir as variáveis de ambiente (ou em **Environment** depois), preencha:
   - **ANTHROPIC_API_KEY** = sua chave (pegue em https://console.anthropic.com/ → API Keys)
2. Salve. O Render faz o build e o deploy automaticamente.

## Passo 5 — Pronto

O Render te dá um link fixo, tipo:

```
https://calculadora-superendividamento.onrender.com
```

Esse é o link que você manda para qualquer pessoa testar — abre direto, com a leitura por IA
funcionando. Não expira.

---

## Observações

- **Plano grátis "dorme":** após ~15 min sem uso, o serviço hiberna e a **primeira** visita
  depois disso demora ~30–50s para "acordar". As visitas seguintes são rápidas. Para uso
  comercial, o plano pago (US$ 7/mês) mantém sempre ligado.
- **Atualizações:** sempre que você rodar `git push`, o Render redeploya sozinho.
- **Custo da IA:** cada documento lido consome alguns centavos da sua conta da Anthropic
  (à parte do Render).
- **Domínio próprio:** dá para apontar um domínio (ex.: `calculadora.seudominio.com.br`)
  nas configurações do serviço no Render.

## Alternativa: Railway

Se preferir, o **Railway** (https://railway.app) funciona igual: conecte o mesmo repositório,
ele detecta o `Procfile`, e você adiciona a variável `ANTHROPIC_API_KEY` em *Variables*.
