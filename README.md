# 📊 Carteira • Rebalanceamento Dinâmico

App web para acompanhar e rebalancear investimentos, com cotação e Valor
Patrimonial em tempo real (Yahoo Finance + Fundamentus), múltiplas carteiras,
login e modo somente-leitura para carteiras de outras pessoas.

---

## ▶️ Rodar no seu Mac (local)

Dê dois cliques em **`Abrir Carteira.command`** (ou no Terminal):

```bash
./venv/bin/python app.py
```

Acesse <http://127.0.0.1:5001>. Crie sua conta, crie uma carteira e adicione
seus ativos pela linha "+" no rodapé da tabela.

---

## ☁️ Publicar na nuvem (Render) — acesso de qualquer lugar

Você só precisa de uma conta gratuita no GitHub e no Render.

### 1. Subir o código para o GitHub
```bash
cd "Carteira Investimentos"
git init
git add .
git commit -m "Carteira de investimentos"
```
Crie um repositório novo (vazio) em <https://github.com/new>, depois:
```bash
git remote add origin https://github.com/SEU_USUARIO/SEU_REPO.git
git branch -M main
git push -u origin main
```

### 2. Criar o serviço no Render
1. Acesse <https://dashboard.render.com> → **New** → **Blueprint**.
2. Conecte sua conta do GitHub e selecione o repositório.
3. O Render lê o arquivo **`render.yaml`** e já cria:
   - o **site** (Web Service), e
   - o **banco PostgreSQL** (`carteira-db`), ligados automaticamente.
4. Clique em **Apply**. Em alguns minutos o site sobe num endereço como
   `https://carteira-investimentos.onrender.com`.

### 3. Usar
- Abra o endereço, **crie sua conta** e sua carteira.
- Compartilhe o mesmo endereço com as outras pessoas — cada uma cria a sua conta.
- Todos veem o rebalanceamento de todos; só o dono edita a própria carteira.

> **Observação:** o plano gratuito do Render "hiberna" após ~15 min sem uso
> (o primeiro acesso depois disso demora alguns segundos) e o PostgreSQL
> gratuito tem validade limitada — dá para subir de plano quando quiser.

---

## Estrutura
- `app.py` — backend Flask (login, carteiras, cotações, API).
- `templates/` — `index.html`, `login.html`, `register.html`.
- `render.yaml` — configuração de deploy do Render.
