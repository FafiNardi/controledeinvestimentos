"""
Carteira de Investimentos - Rebalanceamento Dinâmico (multi-usuário)

- Login / cadastro (cada pessoa cria sua conta)
- Cada usuário cria várias carteiras; carteiras são privadas — cada um só vê
  (e edita) as próprias. Antes de 2026-08-31 todo mundo via a carteira de
  todo mundo (só a edição era travada); mudou a pedido do Rafael.
- Cotação e Valor Patrimonial em tempo real (Yahoo Finance + Fundamentus)
- Roda com SQLite local OU PostgreSQL na nuvem (Render) via DATABASE_URL
"""
import json
import os
import re
from datetime import datetime

import requests
import yfinance as yf
from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, flash, abort)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")

# Banco: PostgreSQL na nuvem (DATABASE_URL) ou SQLite local
db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("postgres://"):           # Render/Neon usam esse prefixo antigo
    db_url = db_url.replace("postgres://", "postgresql://", 1)
# Força o driver psycopg2 (é o que está no requirements.txt) — "postgresql://" sozinho,
# sem versão travada nas dependências, corre o risco de pegar uma versão nova do
# SQLAlchemy que tenta usar o driver "psycopg" (v3) por padrão, que a gente não instala.
# Foi exatamente isso que derrubou o deploy de 26/09/2026 com "ModuleNotFoundError:
# No module named 'psycopg'" — o app nem chegava a subir.
if db_url.startswith("postgresql://") and "+" not in db_url.split("://", 1)[0]:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url or "sqlite:///" + os.path.join(BASE_DIR, "carteira.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# O Render (plano grátis) hiberna o site por inatividade, e o Neon (também grátis)
# suspende a conexão com o banco depois de um tempo parado. Quando o site acorda,
# o SQLAlchemy tentava reaproveitar uma conexão que já tinha morrido do lado do
# banco, e isso derrubava a página com "SSL connection has been closed
# unexpectedly" (500). pool_pre_ping testa a conexão antes de cada uso e reconecta
# sozinho se precisar; pool_recycle descarta conexões paradas há mais de 4 min,
# antes do Neon suspendê-las por conta própria.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 240}

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


# --------------------------------------------------------------------------- #
# Modelos
# --------------------------------------------------------------------------- #

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    carteiras = db.relationship("Carteira", backref="dono", cascade="all, delete-orphan")

    def set_senha(self, s):
        self.senha_hash = generate_password_hash(s)

    def check_senha(self, s):
        return check_password_hash(self.senha_hash, s)


class Carteira(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    carteira_ideal = db.Column(db.Float, default=0)
    renda_ref = db.Column(db.Float, default=3457)   # legado: valor padrão p/ anos sem valor próprio
    renda_ref_json = db.Column(db.Text, default="")  # {"2017": 1000, "2018": 1200, ...} - por ano
    anos_json = db.Column(db.Text, default="")      # anos extras criados manualmente (ex.: "2017,2018")
    moeda = db.Column(db.String(3), default="BRL")  # BRL ou USD (só formatação de exibição)
    assets = db.relationship("Asset", backref="carteira", cascade="all, delete-orphan")
    # Faltava isso: sem essa relação, apagar uma carteira só limpava os ativos do
    # Rebalanceamento (assets acima) — os da Rentabilidade (RentAtivo/RentMov) ficavam
    # pra trás, presos numa carteira_id que não existe mais, e o banco recusava o DELETE
    # por violar a chave estrangeira. Só não tinha aparecido antes porque nenhuma carteira
    # com dado de Rentabilidade tinha sido excluída ainda.
    rentativos = db.relationship("RentAtivo", cascade="all, delete-orphan")
    opcoes = db.relationship("OpcaoOp", cascade="all, delete-orphan")
    darfs = db.relationship("DarfPagamento", cascade="all, delete-orphan")


class Asset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    carteira_id = db.Column(db.Integer, db.ForeignKey("carteira.id"), nullable=False)
    ticker = db.Column(db.String(40), nullable=False)
    classe = db.Column(db.String(40), default="")
    segmento = db.Column(db.String(60), default="")
    num_acoes = db.Column(db.Float, default=0)
    preco_medio = db.Column(db.Float, default=0)
    pct_ideal = db.Column(db.Float, default=0)
    preco = db.Column(db.Float, default=0)
    vpa = db.Column(db.Float, default=0)
    pvp = db.Column(db.Float, default=0)
    dy = db.Column(db.Float, default=0)
    manual = db.Column(db.Integer, default=0)
    vpa_manual = db.Column(db.Integer, default=0)
    ordem = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.String(40), default="")
    # esconde o ativo só da lista da aba Preço Teto (ele continua existindo normalmente no
    # Rebalanceamento) — pra ativos como títulos públicos e fundos de investimento, que não
    # fazem sentido nessa conta
    oculto_preco_teto = db.Column(db.Integer, default=0)
    premissas_teto = db.relationship("PrecoTetoPremissa", cascade="all, delete-orphan")


class PrecoTetoPremissa(db.Model):
    """Premissas que o usuário informa pra calcular o preço teto de um ativo por um
    método específico (Bazin, Barsi, e outros que vierem depois). Os campos que cada
    método precisa mudam (Bazin usa dividendo de 12 meses, Barsi usa soma de 60 meses),
    então ficam guardados livres num JSON em vez de uma coluna fixa por campo — assim dá
    pra "encaixar" um método novo sem alterar o schema do banco."""
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("asset.id"), nullable=False)
    metodo = db.Column(db.String(20), nullable=False)  # bazin, barsi, ...
    dados_json = db.Column(db.Text, default="{}")
    __table_args__ = (db.UniqueConstraint("asset_id", "metodo"),)


class RentAtivo(db.Model):
    """Ativo/fundo acompanhado no módulo de rentabilidade mensal."""
    id = db.Column(db.Integer, primary_key=True)
    carteira_id = db.Column(db.Integer, db.ForeignKey("carteira.id"), nullable=False)
    nome = db.Column(db.String(80), nullable=False)
    ordem = db.Column(db.Integer, default=0)
    is_caixa = db.Column(db.Integer, default=0)   # legado; substituído por classe == "Caixa"
    classe = db.Column(db.String(30), default="")  # Caixa, Renda Fixa, Tesouro Direto, Ações, FII, FII Infra
    inicio_ano = db.Column(db.Integer)   # ano/mês em que o ativo foi criado; None = sem restrição (legado)
    inicio_mes = db.Column(db.Integer)   # não aparece em períodos anteriores a isso
    movs = db.relationship("RentMov", backref="ativo", cascade="all, delete-orphan")


class RentMov(db.Model):
    """Lançamento mensal de um ativo (aporte, resgate, proventos, valor final)."""
    id = db.Column(db.Integer, primary_key=True)
    ativo_id = db.Column(db.Integer, db.ForeignKey("rent_ativo.id"), nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    valor_base = db.Column(db.Float, default=0)   # usado só quando não há mês anterior
    aporte = db.Column(db.Float, default=0)
    resgate = db.Column(db.Float, default=0)
    proventos = db.Column(db.Float, default=0)
    # SEM default=0 de propósito: precisa dar pra distinguir "usuário lançou 0
    # porque a posição zerou de verdade" de "usuário ainda não fechou o mês e
    # não mexeu nesse campo". Com default=0, um mês com só aporte/proventos
    # lançados (valor final ainda em branco) calculava a rentabilidade como se
    # o saldo tivesse ido a zero — rentabilidade de quase -100%. Deixando None
    # até a pessoa preencher de verdade, rent_compute() usa o saldo anterior
    # como estimativa enquanto o mês não fecha (ver `if final is None` abaixo).
    valor_final = db.Column(db.Float)
    __table_args__ = (db.UniqueConstraint("ativo_id", "ano", "mes"),)


class OpcaoOp(db.Model):
    """Venda coberta de put/call (geração de renda com opções). `premio` e
    `custo_recompra` guardam o valor POR OPÇÃO (como é cotado na B3, geralmente
    centavos) — o total em dinheiro é esse valor × `quantidade`, calculado no
    front-end. O campo `status` só registra o que aconteceu no fim: virou pó
    (expirou sem exercício), foi exercida, ou foi recomprada antes do
    vencimento (aí sim há um custo pra fechar, que abate do prêmio)."""
    id = db.Column(db.Integer, primary_key=True)
    carteira_id = db.Column(db.Integer, db.ForeignKey("carteira.id"), nullable=False)
    ativo = db.Column(db.String(40), nullable=False, default="")
    tipo = db.Column(db.String(4), nullable=False, default="PUT")   # PUT ou CALL
    data_abertura = db.Column(db.String(10), default="")     # YYYY-MM-DD
    data_vencimento = db.Column(db.String(10), default="")   # YYYY-MM-DD
    strike = db.Column(db.Float, default=0)
    quantidade = db.Column(db.Float, default=0)   # nº de ações cobertas (não "contratos")
    premio = db.Column(db.Float, default=0)       # valor por opção, recebido na abertura
    status = db.Column(db.String(12), default="aberta")  # aberta, po, exercida, recomprada
    data_fechamento = db.Column(db.String(10), default="")
    custo_recompra = db.Column(db.Float, default=0)   # valor por opção, pago pra fechar antes do vencimento
    # preço médio que o usuário já tinha na ação ANTES dessa call ser exercida — só faz
    # sentido pra CALL exercida (aí ele vendeu a ação pelo strike): dá pra calcular o
    # lucro da venda da ação (strike - preco_medio) além do prêmio da opção em si
    preco_medio = db.Column(db.Float, default=0)
    # custo de corretagem cobrado no exercício — valor TOTAL já calculado pela corretora
    # (varia por operação, cada corretora tem sua própria tabela; o usuário digita o
    # valor final, não dá pra recalcular aqui)
    custo_exercicio = db.Column(db.Float, default=0)
    obs = db.Column(db.Text, default="")
    ordem = db.Column(db.Integer, default=0)


class DarfPagamento(db.Model):
    """Registro manual de 'já paguei essa DARF' — mês+regime da tabela de Imposto de
    Renda da aba Opções. Não guarda o valor calculado (isso é sempre recalculado a
    partir das operações); só a confirmação de pagamento, pra virar um check visual."""
    id = db.Column(db.Integer, primary_key=True)
    carteira_id = db.Column(db.Integer, db.ForeignKey("carteira.id"), nullable=False)
    mes = db.Column(db.String(7), nullable=False)     # YYYY-MM
    regime = db.Column(db.String(6), nullable=False)  # comum ou day
    data_pagamento = db.Column(db.String(10), default="")
    valor_pago = db.Column(db.Float, default=0)
    __table_args__ = (db.UniqueConstraint("carteira_id", "mes", "regime"),)


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# --------------------------------------------------------------------------- #
# Cotações (Yahoo Finance + Fundamentus)
# --------------------------------------------------------------------------- #

def yahoo_symbol(ticker: str, moeda: str = "BRL") -> str:
    t = ticker.strip().upper()
    if "." in t or "-" in t:
        return t
    if moeda == "USD":
        return t  # ativos dos EUA (NYSE/NASDAQ) não usam sufixo no Yahoo Finance
    return t + ".SA"


def fetch_fundamentus_vp(ticker: str):
    t = ticker.strip().upper()
    try:
        html = requests.get(
            "https://www.fundamentus.com.br/detalhes.php?papel=" + t,
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        ).content.decode("latin-1")
        m = re.search(r"VP/Cota.*?>\s*([\d\.,]+)\s*</span>", html, re.S)
        if m:
            return float(m.group(1).replace(".", "").replace(",", "."))
    except Exception:  # noqa: BLE001
        pass
    return None


def parse_num_br(s: str):
    """'1.234,56' -> 1234.56 (número no formato brasileiro: ponto de milhar, vírgula decimal)."""
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


INVESTIDOR10_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def fetch_investidor10(ticker: str) -> dict:
    """Extrai cotação e indicadores fundamentalistas do investidor10.com.br.
    Tenta primeiro como ação, depois como FII — os dois têm páginas com HTML
    bem diferentes entre si, então cada indicador tem seu próprio jeito de achar:
    - Preço atual: <span class='livePrice'>R$ X</span> (existe nas duas páginas)
    - LPA/ROE/Payout/VPA/P-VP/DY (ações): atributos data-indicator="X" data-current-value="Y"
    - P/VP e DY (FIIs): dentro de blocos <div class="_card vp/dy">...<span>Y</span>
    - Dividendos dos últimos 12 meses (ações e FIIs): frase fixa do texto de perguntas
      frequentes da própria página ("Nos últimos 12 meses ... pagou um total de R$ X")
    """
    t = ticker.strip().lower()
    out = {"preco": None, "vpa": None, "pvp": None, "dy": None,
           "lpa": None, "roe": None, "payout": None, "dividendos_12m": None, "erro": None}
    for tipo in ("acoes", "fiis"):
        try:
            r = requests.get(f"https://investidor10.com.br/{tipo}/{t}/",
                              headers=INVESTIDOR10_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            html = r.text
            m = re.search(r"class=['\"]livePrice['\"]>\s*R\$\s*([\d\.,]+)", html)
            if not m:
                continue  # não é esse tipo de ativo — tenta o outro (acoes <-> fiis)
            out["preco"] = parse_num_br(m.group(1))
            for campo, nome in (("lpa", "LPA"), ("roe", "ROE"), ("payout", "Payout"),
                                ("vpa", "VPA")):
                mi = re.search(rf'data-indicator="{nome}"\s+data-current-value="([\-\d.]+)"', html)
                if mi:
                    out[campo] = float(mi.group(1))
            mp = re.search(r'data-indicator="P/VP"\s+data-current-value="([\-\d.]+)"', html)
            if mp:
                out["pvp"] = float(mp.group(1))
            md = re.search(r'data-indicator="Dividend Yield"\s+data-current-value="([\-\d.]+)"', html)
            if md:
                out["dy"] = float(md.group(1))
            if out["pvp"] is None:  # layout de FII
                mpvp = re.search(r'"_card vp".*?<span>([\d.,]+)</span>', html, re.S)
                if mpvp:
                    out["pvp"] = parse_num_br(mpvp.group(1))
            if out["dy"] is None:
                mdy = re.search(r'"_card dy".*?<span>([\d.,]+)%</span>', html, re.S)
                if mdy:
                    out["dy"] = parse_num_br(mdy.group(1))
            mdiv = re.search(r"Nos últimos 12 meses,.*?pagou um total de R\$\s*([\d.,]+) em dividendos", html)
            if mdiv:
                out["dividendos_12m"] = parse_num_br(mdiv.group(1))
            return out
        except Exception as e:  # noqa: BLE001
            out["erro"] = str(e)
    if not out["preco"]:
        out["erro"] = out["erro"] or "ativo não encontrado no Investidor10"
    return out


def fetch_investidor10_historico(ticker: str) -> dict:
    """Histórico de 5 anos de ROE, LPA e Dividend Yield pro método 'Crescimento Histórico'
    de preço teto. Precisa de duas idas ao site: a primeira só pra achar o ID interno do
    ativo (aparece no link de 'seguir ativo' da própria página, não é o ticker), a segunda
    pra pegar o histórico de verdade num endpoint interno que a página usa pros gráficos."""
    t = ticker.strip().lower()
    out = {"crescimento_lucro": None, "crescimento_roe": None,
           "dividendos_consistentes": None, "erro": None}
    for tipo in ("acoes", "fiis"):
        try:
            r = requests.get(f"https://investidor10.com.br/{tipo}/{t}/",
                              headers=INVESTIDOR10_HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            m = re.search(r"api/seguir-ativo/(\d+)/", r.text)
            if not m:
                continue
            ativo_id = m.group(1)
            rh = requests.get(
                f"https://investidor10.com.br/api/historico-indicadores/{ativo_id}/5/?v=2",
                headers={**INVESTIDOR10_HEADERS, "X-Requested-With": "XMLHttpRequest",
                         "Referer": f"https://investidor10.com.br/{tipo}/{t}/"}, timeout=15)
            if rh.status_code != 200:
                continue
            data = rh.json()

            def serie(nome):
                # "Atual" é o ano corrente ainda em andamento — só entram anos fechados.
                # Vem do mais recente pro mais antigo.
                itens = data.get(nome) or []
                return [float(x["value"]) for x in itens
                        if x.get("year") != "Atual" and x.get("value") is not None]

            def media_crescimento(vals):
                if len(vals) < 2:
                    return None
                taxas = []
                for i in range(len(vals) - 1):
                    anterior = vals[i + 1]
                    if anterior:
                        taxas.append((vals[i] - anterior) / anterior * 100)
                return sum(taxas) / len(taxas) if taxas else None

            lpa, roe, dy = serie("LPA"), serie("ROE"), serie("Dividend Yield")
            out["crescimento_lucro"] = media_crescimento(lpa)
            out["crescimento_roe"] = media_crescimento(roe)
            out["dividendos_consistentes"] = bool(dy) and len(dy) >= 5 and all(v > 0 for v in dy)
            return out
        except Exception as e:  # noqa: BLE001
            out["erro"] = str(e)
    if out["crescimento_lucro"] is None and out["crescimento_roe"] is None:
        out["erro"] = out["erro"] or "histórico não encontrado no Investidor10"
    return out


def fetch_quote(ticker: str, moeda: str = "BRL") -> dict:
    symbol = yahoo_symbol(ticker, moeda)
    out = {"preco": None, "vpa": None, "pvp": None, "dy": None, "erro": None}
    # Investidor10 é a fonte principal pra ativos em R$ (pedido do Rafael) — cobre ações e
    # FIIs numa tacada só, sem precisar do Yahoo Finance + Fundamentus juntos como antes.
    # USD continua no Yahoo Finance, já que o Investidor10 é focado no mercado brasileiro.
    if moeda != "USD":
        d10 = fetch_investidor10(ticker)
        if d10["preco"]:
            out.update(preco=d10["preco"], vpa=d10["vpa"], pvp=d10["pvp"], dy=d10["dy"])
            return out
    try:
        tk = yf.Ticker(symbol)
        preco = None
        # fast_info.last_price é a forma correta no yfinance >= 0.2
        try:
            preco = tk.fast_info.last_price
        except Exception:
            preco = None
        # fallback: histórico do dia
        if not preco:
            try:
                hist = tk.history(period="1d")
                if not hist.empty:
                    preco = float(hist["Close"].iloc[-1])
            except Exception:
                pass
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        if not preco:
            preco = info.get("currentPrice") or info.get("regularMarketPrice")
        vpa = info.get("bookValue")
        pvp = info.get("priceToBook")
        if not vpa and moeda != "USD" and ticker.strip().upper().endswith("11"):
            vpa = fetch_fundamentus_vp(ticker)
        if preco and vpa:
            pvp = preco / vpa
        dy = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
        if dy is not None and dy < 1:
            dy = dy * 100
        out.update(preco=preco, vpa=vpa, pvp=pvp, dy=dy)
        if not preco:
            out["erro"] = "sem cotação"
    except Exception as e:  # noqa: BLE001
        out["erro"] = str(e)
    return out


# --------------------------------------------------------------------------- #
# Cálculos
# --------------------------------------------------------------------------- #

def asset_dict(a: Asset) -> dict:
    return {c.name: getattr(a, c.name) for c in Asset.__table__.columns}


def compute_rows(assets_list, carteira_ideal):
    assets = [asset_dict(a) for a in assets_list]
    total_tenho = sum((a["num_acoes"] or 0) * (a["preco"] or 0) for a in assets)
    for a in assets:
        preco = a["preco"] or 0
        tenho = (a["num_acoes"] or 0) * preco
        quero = (a["pct_ideal"] or 0) / 100.0 * carteira_ideal
        falta = quero - tenho
        pct_tenho = (tenho / total_tenho * 100) if total_tenho else 0
        rent = 0
        if a["preco_medio"]:
            rent = (preco - a["preco_medio"]) / a["preco_medio"] * 100
        decisao = "Comprar" if falta > 0.005 * max(quero, 1) else "Aguardar"
        qtd_cotas = int(falta // preco) if preco and falta > 0 else 0
        if preco and a.get("vpa"):
            a["pvp"] = preco / a["vpa"]
        a.update(tenho=tenho, quero=quero, falta=falta, pct_tenho=pct_tenho,
                 rentabilidade=rent, decisao=decisao, qtd_cotas=qtd_cotas)
    return assets, total_tenho


# --------------------------------------------------------------------------- #
# Autenticação
# --------------------------------------------------------------------------- #

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        senha = request.form.get("senha") or ""
        if not nome or not senha:
            flash("Preencha nome e senha.")
        elif User.query.filter_by(nome=nome).first():
            flash("Esse nome já existe. Escolha outro.")
        else:
            u = User(nome=nome)
            u.set_senha(senha)
            db.session.add(u)
            db.session.commit()
            login_user(u)
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        senha = request.form.get("senha") or ""
        u = User.query.filter_by(nome=nome).first()
        if u and u.check_senha(senha):
            login_user(u)
            return redirect(url_for("index"))
        flash("Nome ou senha incorretos.")
    return render_template("login.html")


@app.route("/recuperar", methods=["GET", "POST"])
def recuperar():
    """Redefine a senha usando a chave-mestra (env RESET_SECRET)."""
    reset_secret = os.environ.get("RESET_SECRET", "")
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        chave = request.form.get("chave") or ""
        nova = request.form.get("senha") or ""
        if not reset_secret:
            flash("Recuperação indisponível: a chave-mestra não foi configurada.")
        elif chave != reset_secret:
            flash("Chave-mestra incorreta.")
        elif not nome or not nova:
            flash("Preencha o nome de usuário e a nova senha.")
        else:
            u = User.query.filter_by(nome=nome).first()
            if not u:
                flash("Usuário não encontrado.")
            else:
                u.set_senha(nova)
                db.session.commit()
                flash("Senha redefinida! Faça login com a nova senha.")
                return redirect(url_for("login"))
    return render_template("recuperar.html", habilitado=bool(reset_secret))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

@app.route("/")
@login_required
def index():
    return render_template("index.html", usuario=current_user.nome)


@app.route("/api/carteiras")
@login_required
def api_carteiras():
    """Só as carteiras do usuário logado (privado desde 2026-08-31 — antes
    todo mundo via a carteira de todo mundo, só não podia editar a alheia)."""
    out = []
    for c in Carteira.query.filter_by(user_id=current_user.id).order_by(Carteira.id).all():
        out.append({"id": c.id, "nome": c.nome, "dono": c.dono.nome,
                    "user_id": c.user_id, "minha": True})
    return jsonify({"carteiras": out, "user_id": current_user.id})


@app.route("/api/carteiras", methods=["POST"])
@login_required
def api_nova_carteira():
    data = request.get_json(force=True)
    nome = (data.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "informe um nome"}), 400
    c = Carteira(user_id=current_user.id, nome=nome,
                 carteira_ideal=float(data.get("carteira_ideal") or 0))
    db.session.add(c)
    db.session.commit()
    return jsonify({"ok": True, "id": c.id})


def get_carteira_or_404(cid):
    c = db.session.get(Carteira, cid)
    if not c:
        abort(404)
    return c


def require_owner(c):
    if c.user_id != current_user.id:
        abort(403)


@app.route("/api/carteira/<int:cid>/state")
@login_required
def api_state(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)  # carteira agora é privada: só o dono enxerga
    assets = Asset.query.filter_by(carteira_id=cid).order_by(Asset.ordem, Asset.id).all()
    computed, total = compute_rows(assets, c.carteira_ideal or 0)
    return jsonify({
        "carteira": {"id": c.id, "nome": c.nome, "dono": c.dono.nome,
                     "carteira_ideal": c.carteira_ideal or 0, "moeda": c.moeda or "BRL",
                     "editavel": c.user_id == current_user.id},
        "assets": computed,
        "total_tenho": total,
        "pct_ideal_total": sum(a["pct_ideal"] or 0 for a in computed),
    })


@app.route("/api/carteira/<int:cid>", methods=["DELETE"])
@login_required
def api_delete_carteira(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    # Antes isso era só "db.session.delete(c)", deixando o SQLAlchemy cascatear via ORM: carrega
    # CADA RentAtivo e CADA RentMov como objeto Python antes de apagar um por um. Numa carteira
    # pequena não dá pra notar, mas numa com anos de histórico isso empilha milhares de objetos na
    # memória de uma vez — no plano free do Render (512 MB) isso derrubou o worker (OOM/SIGKILL) na
    # hora de excluir a carteira "Teste - Rafael". Trocado por DELETE em lote (uma consulta só por
    # tabela, sem carregar linha nenhuma como objeto Python).
    ativo_ids = [row[0] for row in RentAtivo.query.filter_by(carteira_id=cid)
                 .with_entities(RentAtivo.id).all()]
    if ativo_ids:
        RentMov.query.filter(RentMov.ativo_id.in_(ativo_ids)).delete(synchronize_session=False)
    RentAtivo.query.filter_by(carteira_id=cid).delete(synchronize_session=False)
    asset_ids = [row[0] for row in Asset.query.filter_by(carteira_id=cid)
                 .with_entities(Asset.id).all()]
    if asset_ids:
        PrecoTetoPremissa.query.filter(PrecoTetoPremissa.asset_id.in_(asset_ids)).delete(synchronize_session=False)
    Asset.query.filter_by(carteira_id=cid).delete(synchronize_session=False)
    OpcaoOp.query.filter_by(carteira_id=cid).delete(synchronize_session=False)
    DarfPagamento.query.filter_by(carteira_id=cid).delete(synchronize_session=False)
    db.session.delete(c)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/carteira/<int:cid>/config", methods=["POST"])
@login_required
def api_config(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    data = request.get_json(force=True)
    if "carteira_ideal" in data:
        c.carteira_ideal = float(data["carteira_ideal"] or 0)
    if "renda_ref" in data:
        if "ano" in data:
            mapa = {}
            if c.renda_ref_json:
                try:
                    mapa = json.loads(c.renda_ref_json)
                except (TypeError, ValueError):
                    mapa = {}
            mapa[str(int(data["ano"]))] = float(data["renda_ref"] or 0)
            c.renda_ref_json = json.dumps(mapa)
        else:
            c.renda_ref = float(data["renda_ref"] or 0)
    if data.get("moeda") in ("BRL", "USD"):
        c.moeda = data["moeda"]
    if data.get("nome"):
        c.nome = data["nome"].strip()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/carteira/<int:cid>/assets", methods=["POST"])
@login_required
def api_add_asset(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    data = request.get_json(force=True)
    ticker = (data.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"erro": "ticker obrigatório"}), 400
    maxord = db.session.query(db.func.coalesce(db.func.max(Asset.ordem), 0)) \
        .filter_by(carteira_id=cid).scalar()
    a = Asset(carteira_id=cid, ticker=ticker, classe=data.get("classe", ""),
              segmento=data.get("segmento", ""),
              num_acoes=float(data.get("num_acoes") or 0),
              preco_medio=float(data.get("preco_medio") or 0),
              pct_ideal=float(data.get("pct_ideal") or 0),
              manual=1 if data.get("manual") else 0, ordem=maxord + 1)
    db.session.add(a)
    db.session.commit()
    if not a.manual:
        q = fetch_quote(ticker, c.moeda or "BRL")
        if q["preco"]:
            a.preco, a.vpa, a.pvp, a.dy = q["preco"], q["vpa"] or 0, q["pvp"] or 0, q["dy"] or 0
            a.updated_at = datetime.now().isoformat(timespec="seconds")
        else:
            # não achou cotação de verdade (ex.: usuário digitou um nome tipo "Fundo DI" ou
            # "Tesouro Selic 2029", não um ticker de bolsa) — vira editável na hora, senão o
            # preço fica travado em R$0,00 pra sempre sem nenhum jeito de corrigir
            a.manual = 1
        db.session.commit()
    return jsonify({"ok": True, "id": a.id})


EDITABLE = {"ticker", "classe", "segmento", "num_acoes", "preco_medio",
            "pct_ideal", "preco", "manual", "vpa", "vpa_manual", "oculto_preco_teto"}
NUM_FIELDS = {"num_acoes", "preco_medio", "pct_ideal", "preco", "vpa"}


@app.route("/api/assets/<int:aid>", methods=["PUT"])
@login_required
def api_update_asset(aid):
    a = db.session.get(Asset, aid)
    if not a:
        abort(404)
    require_owner(a.carteira)
    data = request.get_json(force=True)
    for k, v in data.items():
        if k in EDITABLE:
            if k in NUM_FIELDS:
                v = float(v or 0)
            setattr(a, k, v)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/assets/<int:aid>", methods=["DELETE"])
@login_required
def api_delete_asset(aid):
    a = db.session.get(Asset, aid)
    if not a:
        abort(404)
    require_owner(a.carteira)
    db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/carteira/<int:cid>/refresh", methods=["POST"])
@login_required
def api_refresh(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)  # carteira agora é privada: só o dono mexe (antes qualquer logado podia)
    assets = Asset.query.filter_by(carteira_id=cid, manual=0).all()
    resultados = []
    for a in assets:
        q = fetch_quote(a.ticker, c.moeda or "BRL")
        if q["preco"]:
            a.preco = q["preco"]
            a.vpa = a.vpa if a.vpa_manual else (q["vpa"] or 0)
            a.pvp = q["pvp"] or 0
            a.dy = q["dy"] or 0
            a.updated_at = datetime.now().isoformat(timespec="seconds")
        resultados.append({"ticker": a.ticker, **q})
    db.session.commit()
    return jsonify({"ok": True, "resultados": resultados})


# --------------------------------------------------------------------------- #
# Rentabilidade mensal (réplica da planilha "Rent. Geral Acum.")
# --------------------------------------------------------------------------- #

def rent_compute(cid):
    """Calcula toda a série histórica de rentabilidade de uma carteira.

    Retorno do mês = (ValorFinal + Resgate + Proventos) / (Base + Aporte) - 1
    Isso trata resgate e proventos como dinheiro que saiu do fundo (positivo
    para o retorno quando há lucro), evitando o bug de mostrar -100% num
    resgate lucrativo que zera a posição.
    Acumulado = capitalização composta dos retornos mensais.
    Um ativo só aparece a partir do seu início efetivo: o mais antigo entre
    (a) o mês em que foi criado (inicio_ano/mes) e (b) o mês do seu primeiro
    lançamento real. Isso evita tanto "vazar" para antes da criação quanto
    "vazar" para antes do primeiro lançamento em ativos antigos que não têm
    inicio_ano preenchido (criados antes desse recurso existir).
    """
    # joinedload evita 1 consulta ao banco por ativo (N+1): sem isso, uma carteira
    # com 50 ativos e anos de histórico faz 50 idas ao banco extras TODA vez que
    # essa função roda — e ela roda a cada troca de mês, aba ou recarregamento.
    ativos = RentAtivo.query.filter_by(carteira_id=cid) \
        .options(db.joinedload(RentAtivo.movs)) \
        .order_by(RentAtivo.ordem, RentAtivo.id).all()
    caixa_ids = {a.id for a in ativos if (a.classe or "") == "Caixa" or a.is_caixa}
    movs = {}
    for a in ativos:
        for m in a.movs:
            movs[(a.id, m.ano, m.mes)] = m
    ativos_out = [{"id": a.id, "nome": a.nome, "classe": a.classe or ""}
                  for a in ativos]

    def inicio_efetivo(a):
        candidatos = []
        if a.inicio_ano:
            candidatos.append((a.inicio_ano, a.inicio_mes or 1))
        movs_ativo = [(m.ano, m.mes) for m in a.movs]
        if movs_ativo:
            candidatos.append(min(movs_ativo))
        return min(candidatos) if candidatos else None

    inicio_por_ativo = {a.id: inicio_efetivo(a) for a in ativos}
    if not movs:
        return {"ativos": ativos_out, "meses": {}, "anos": []}

    periodos = sorted({(m.ano, m.mes) for m in movs.values()})
    anos = sorted({p[0] for p in periodos})
    # série completa do primeiro ao último mês lançado
    a0, m0 = periodos[0]
    a1, m1 = periodos[-1]
    # estende o fim da série até hoje e até o último ano adicionado manualmente,
    # para os ativos continuarem "copiando" o saldo nos anos seguintes mesmo
    # sem nenhum lançamento novo em nenhum ativo da carteira
    c = db.session.get(Carteira, cid)
    hoje = datetime.now()
    candidatos_fim = [(a1, m1), (hoje.year, hoje.month)]
    if c and c.anos_json:
        anos_extra = [int(x) for x in c.anos_json.split(",") if x.strip().isdigit()]
        if anos_extra:
            candidatos_fim.append((max(anos_extra), 12))
    a1, m1 = max(candidatos_fim)
    serie = []
    y, mth = a0, m0
    while (y, mth) <= (a1, m1):
        serie.append((y, mth))
        mth += 1
        if mth > 12:
            mth = 1; y += 1

    ultimo_final = {a.id: None for a in ativos}   # carrega o saldo entre meses
    ja_comecou = {a.id: False for a in ativos}    # já teve algum lançamento?
    acum = 1.0
    out_meses = {}
    for (y, mth) in serie:
        linhas = []
        tot = {"base": 0.0, "aporte": 0.0, "resgate": 0.0,
               "proventos": 0.0, "final": 0.0}
        geracao_caixa = 0.0   # rendimento puro dos fundos caixa/DI no mês
        tem_dado = False
        for a in ativos:
            # ativo ainda não existia neste período (antes do início efetivo) -> ignora
            inicio = inicio_por_ativo[a.id]
            if inicio and (y, mth) < inicio:
                continue
            mv = movs.get((a.id, y, mth))
            base = ultimo_final[a.id]
            if base is None:
                base = (mv.valor_base if mv else 0) or 0
            aporte = (mv.aporte if mv else 0) or 0
            resgate = (mv.resgate if mv else 0) or 0
            prov = (mv.proventos if mv else 0) or 0
            final = (mv.valor_final if mv else None)
            if mv:
                tem_dado = True
            # "estimado": o mês ainda não foi fechado (ninguém preencheu valor final
            # de verdade) — usamos o saldo anterior só pra rentabilidade não desabar
            # pra -100%, mas isso NUNCA pode aparecer pro usuário como se fosse um
            # valor digitado, senão ele vê o número, estranha e apaga — o que grava
            # um zero de verdade e recria o mesmo bug (foi exatamente o que aconteceu
            # em 2026-08-30 com SGOV/Realty Income).
            estimado = final is None
            if final is None:
                final = base if base else 0
            rent = None
            denom = base + aporte
            if denom:
                rent = (final + resgate + prov) / denom - 1
            # decide se o ativo aparece na lista deste mês:
            #  - aparece se teve lançamento, se ainda tem saldo, ou se nunca começou
            #  - some se foi encerrado (saldo 0, sem lançamento, mas já operou antes)
            if mv or base > 0 or not ja_comecou[a.id]:
                mostrar = True
            else:
                mostrar = False
            linhas.append({"ativo_id": a.id, "nome": a.nome, "base": base,
                           "aporte": aporte, "resgate": resgate,
                           "proventos": prov, "final": final, "rent": rent,
                           "tem_mov": bool(mv), "mostrar": mostrar,
                           "final_estimado": estimado})
            if mv:
                ja_comecou[a.id] = True
            tot["base"] += base; tot["aporte"] += aporte
            tot["resgate"] += resgate; tot["proventos"] += prov
            tot["final"] += final
            # geração de caixa = rendimento do ativo (só p/ fundos caixa/DI)
            if a.id in caixa_ids and mv:
                geracao_caixa += final - base - aporte + resgate + prov
            ultimo_final[a.id] = final
        # retorno da carteira no mês
        rent_cart = None
        denom = tot["base"] + tot["aporte"]
        if denom:
            rent_cart = (tot["final"] + tot["resgate"] + tot["proventos"]) / denom - 1
        if rent_cart is not None and tem_dado:
            acum *= (1 + rent_cart)
        for ln in linhas:
            ln["peso"] = (ln["final"] / tot["final"]) if tot["final"] else 0
        out_meses[f"{y}-{mth:02d}"] = {
            "ano": y, "mes": mth, "linhas": linhas,
            "patrimonio": tot["final"], "aportes": tot["aporte"],
            "resgates": tot["resgate"], "dividendos": tot["proventos"],
            "geracao_caixa": geracao_caixa if tem_dado else 0,
            "geracao_total": (geracao_caixa + tot["proventos"]) if tem_dado else 0,
            "rent": rent_cart if tem_dado else None,
            "acumulado": (acum - 1) if tem_dado else None,
            "tem_dado": tem_dado,
        }
    return {"ativos": ativos_out, "meses": out_meses, "anos": anos}


@app.route("/rentabilidade")
@login_required
def rentabilidade():
    return render_template("rentabilidade.html", usuario=current_user.nome)


@app.route("/api/carteira/<int:cid>/rent")
@login_required
def api_rent(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)  # carteira agora é privada: só o dono enxerga
    data = rent_compute(cid)
    data["editavel"] = c.user_id == current_user.id
    anos_cfg = [int(x) for x in (c.anos_json or "").split(",") if x.strip().isdigit()]
    renda_ref_por_ano = {}
    if c.renda_ref_json:
        try:
            renda_ref_por_ano = json.loads(c.renda_ref_json)
        except (TypeError, ValueError):
            renda_ref_por_ano = {}
    data["carteira"] = {"id": c.id, "nome": c.nome, "dono": c.dono.nome,
                        "renda_ref": c.renda_ref or 3457, "renda_ref_por_ano": renda_ref_por_ano,
                        "moeda": c.moeda or "BRL"}
    data["anos_cfg"] = anos_cfg
    return jsonify(data)


@app.route("/api/carteira/<int:cid>/rent/anos", methods=["POST"])
@login_required
def api_rent_add_ano(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    d = request.get_json(force=True)
    atuais = {int(x) for x in (c.anos_json or "").split(",") if x.strip().isdigit()}
    # aceita um ano só {ano} ou uma lista {anos:[...]}
    novos = d.get("anos") or ([d.get("ano")] if d.get("ano") else [])
    for a in novos:
        try:
            atuais.add(int(a))
        except (TypeError, ValueError):
            pass
    c.anos_json = ",".join(str(x) for x in sorted(atuais))
    db.session.commit()
    return jsonify({"ok": True, "anos": sorted(atuais)})


@app.route("/api/carteira/<int:cid>/rent/ativos", methods=["POST"])
@login_required
def api_rent_add_ativo(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    d = request.get_json(force=True)
    nome = (d.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "informe um nome"}), 400
    # se já existe um ativo com esse nome nessa carteira (ex.: você fechou a posição e está
    # reabrindo agora), reaproveita o mesmo registro em vez de criar um histórico novo e
    # desconectado — assim o gráfico e as estatísticas continuam somando tudo junto
    existente = RentAtivo.query.filter_by(carteira_id=cid).filter(
        db.func.lower(RentAtivo.nome) == nome.lower()).first()
    if existente:
        # garante um lançamento (mesmo que zerado) no mês atual, senão o ativo reaproveitado
        # continuaria escondido da tabela por já ter saldo zerado desde que foi encerrado
        ano, mes = d.get("ano"), d.get("mes")
        if ano and mes:
            ano, mes = int(ano), int(mes)
            if not RentMov.query.filter_by(ativo_id=existente.id, ano=ano, mes=mes).first():
                db.session.add(RentMov(ativo_id=existente.id, ano=ano, mes=mes))
                db.session.commit()
        return jsonify({"ok": True, "id": existente.id, "reaproveitado": True})
    maxord = db.session.query(db.func.coalesce(db.func.max(RentAtivo.ordem), 0)) \
        .filter_by(carteira_id=cid).scalar()
    # ativo só aparece a partir do mês/ano em que foi criado (não "vaza" para anos anteriores)
    ano = d.get("ano")
    mes = d.get("mes")
    a = RentAtivo(carteira_id=cid, nome=nome, ordem=maxord + 1,
                  inicio_ano=int(ano) if ano else None,
                  inicio_mes=int(mes) if mes else None)
    db.session.add(a)
    db.session.commit()
    return jsonify({"ok": True, "id": a.id})


@app.route("/api/carteira/<int:cid>/rent/reordenar", methods=["POST"])
@login_required
def api_rent_reordenar(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    ids = request.get_json(force=True).get("ordem") or []
    ativos = {a.id: a for a in RentAtivo.query.filter_by(carteira_id=cid).all()}
    for i, aid in enumerate(ids):
        a = ativos.get(int(aid))
        if a:
            a.ordem = i
    db.session.commit()
    return jsonify({"ok": True})


# Mapas de tradução de classe ao trocar de moeda — espelham CLASSE_BRL_TO_USD /
# CLASSE_USD_TO_BRL do rentabilidade.html. Ficam duplicados de propósito (front só
# usa o dele pra popular o <select> na hora, sem depender de round-trip nenhum).
CLASSE_BRL_TO_USD = {"Renda Fixa": "Bond", "FII": "REIT", "Ações": "Stocks", "ETF": "ETF", "Caixa": "Cash"}
CLASSE_USD_TO_BRL = {"Bond": "Renda Fixa", "REIT": "FII", "Stocks": "Ações", "ETF": "ETF", "Cash": "Caixa"}


@app.route("/api/carteira/<int:cid>/rent/moeda", methods=["POST"])
@login_required
def api_rent_moeda(cid):
    """Troca a moeda da carteira E traduz a classe de todos os ativos NUMA TACADA SÓ.
    Antes o front fazia um PUT por ativo (às vezes dezenas, uma carteira grande = uma
    fila de requisições) — lento e, pro usuário, parecia que o botão nem tinha reagido."""
    c = get_carteira_or_404(cid)
    require_owner(c)
    nova = request.get_json(force=True).get("moeda")
    if nova not in ("BRL", "USD"):
        return jsonify({"erro": "moeda inválida"}), 400
    mapa = CLASSE_BRL_TO_USD if nova == "USD" else CLASSE_USD_TO_BRL
    for a in RentAtivo.query.filter_by(carteira_id=cid).all():
        if a.classe in mapa:
            a.classe = mapa[a.classe]
    c.moeda = nova
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/rent/ativos/<int:aid>", methods=["PUT", "DELETE"])
@login_required
def api_rent_ativo(aid):
    a = db.session.get(RentAtivo, aid)
    if not a:
        abort(404)
    require_owner(db.session.get(Carteira, a.carteira_id))
    if request.method == "DELETE":
        db.session.delete(a)
    else:
        d = request.get_json(force=True)
        nome = (d.get("nome") or "").strip()
        if nome:
            a.nome = nome
        if "classe" in d:
            a.classe = (d["classe"] or "").strip()
    db.session.commit()
    return jsonify({"ok": True})


RENT_FIELDS = {"valor_base", "aporte", "resgate", "proventos", "valor_final"}


@app.route("/api/rent/mov", methods=["PUT"])
@login_required
def api_rent_mov():
    d = request.get_json(force=True)
    a = db.session.get(RentAtivo, int(d["ativo_id"]))
    if not a:
        abort(404)
    require_owner(db.session.get(Carteira, a.carteira_id))
    ano, mes = int(d["ano"]), int(d["mes"])
    mv = RentMov.query.filter_by(ativo_id=a.id, ano=ano, mes=mes).first()
    if not mv:
        mv = RentMov(ativo_id=a.id, ano=ano, mes=mes)
        db.session.add(mv)
    for k, v in d.items():
        if k in RENT_FIELDS:
            setattr(mv, k, float(v or 0))
    try:
        db.session.commit()
    except IntegrityError:
        # duas edições do mesmo ativo/mês chegaram quase juntas: a outra requisição já
        # criou a linha antes desta commitar. Refaz como atualização da linha existente.
        db.session.rollback()
        mv = RentMov.query.filter_by(ativo_id=a.id, ano=ano, mes=mes).first()
        for k, v in d.items():
            if k in RENT_FIELDS:
                setattr(mv, k, float(v or 0))
        db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Opções (venda coberta de put/call)
# --------------------------------------------------------------------------- #

@app.route("/opcoes")
@login_required
def opcoes():
    return render_template("opcoes.html", usuario=current_user.nome)


@app.route("/api/carteira/<int:cid>/opcoes")
@login_required
def api_opcoes(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    ops = OpcaoOp.query.filter_by(carteira_id=cid).order_by(OpcaoOp.data_abertura, OpcaoOp.id).all()
    out = [{
        "id": o.id, "ativo": o.ativo, "tipo": o.tipo,
        "data_abertura": o.data_abertura, "data_vencimento": o.data_vencimento,
        "strike": o.strike or 0, "quantidade": o.quantidade or 0, "premio": o.premio or 0,
        "status": o.status or "aberta", "data_fechamento": o.data_fechamento or "",
        "custo_recompra": o.custo_recompra or 0, "preco_medio": o.preco_medio or 0,
        "custo_exercicio": o.custo_exercicio or 0, "obs": o.obs or "",
    } for o in ops]
    darfs = DarfPagamento.query.filter_by(carteira_id=cid).all()
    darfs_out = [{"id": d.id, "mes": d.mes, "regime": d.regime,
                  "data_pagamento": d.data_pagamento or "", "valor_pago": d.valor_pago or 0} for d in darfs]
    return jsonify({
        "operacoes": out,
        "darfs": darfs_out,
        "carteira": {"id": c.id, "nome": c.nome, "dono": c.dono.nome, "moeda": c.moeda or "BRL"},
        "editavel": c.user_id == current_user.id,
    })


@app.route("/api/carteira/<int:cid>/opcoes", methods=["POST"])
@login_required
def api_opcoes_add(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    d = request.get_json(force=True) or {}
    hoje = datetime.now().strftime("%Y-%m-%d")
    o = OpcaoOp(carteira_id=cid, ativo=(d.get("ativo") or "").strip().upper(),
                tipo=d.get("tipo") if d.get("tipo") in ("PUT", "CALL") else "PUT",
                data_abertura=d.get("data_abertura") or hoje,
                data_vencimento=d.get("data_vencimento") or hoje,
                strike=float(d.get("strike") or 0), quantidade=float(d.get("quantidade") or 100),
                premio=float(d.get("premio") or 0), status="aberta")
    db.session.add(o)
    db.session.commit()
    return jsonify({"ok": True, "id": o.id})


OPCAO_FIELDS = {"ativo", "tipo", "data_abertura", "data_vencimento", "strike",
                "quantidade", "premio", "status", "data_fechamento", "custo_recompra",
                "preco_medio", "custo_exercicio", "obs"}
OPCAO_NUM_FIELDS = {"strike", "quantidade", "premio", "custo_recompra", "preco_medio", "custo_exercicio"}


@app.route("/api/opcoes/<int:oid>", methods=["PUT", "DELETE"])
@login_required
def api_opcao_editar(oid):
    o = db.session.get(OpcaoOp, oid)
    if not o:
        abort(404)
    require_owner(db.session.get(Carteira, o.carteira_id))
    if request.method == "DELETE":
        db.session.delete(o)
        db.session.commit()
        return jsonify({"ok": True})
    d = request.get_json(force=True) or {}
    for k, v in d.items():
        if k not in OPCAO_FIELDS:
            continue
        if k in OPCAO_NUM_FIELDS:
            setattr(o, k, float(v or 0))
        elif k == "ativo":
            o.ativo = (v or "").strip().upper()
        elif k == "tipo":
            if v in ("PUT", "CALL"):
                o.tipo = v
        elif k == "status":
            if v in ("aberta", "po", "exercida", "recomprada"):
                o.status = v
        else:
            setattr(o, k, v or "")
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/carteira/<int:cid>/darfs", methods=["POST"])
@login_required
def api_darf_marcar(cid):
    """Marca (ou atualiza) uma DARF mês+regime como paga."""
    c = get_carteira_or_404(cid)
    require_owner(c)
    d = request.get_json(force=True) or {}
    mes = (d.get("mes") or "").strip()
    regime = d.get("regime")
    if not mes or regime not in ("comum", "day"):
        return jsonify({"erro": "informe mês e regime válidos"}), 400
    reg = DarfPagamento.query.filter_by(carteira_id=cid, mes=mes, regime=regime).first()
    if not reg:
        reg = DarfPagamento(carteira_id=cid, mes=mes, regime=regime)
        db.session.add(reg)
    reg.data_pagamento = d.get("data_pagamento") or datetime.now().strftime("%Y-%m-%d")
    reg.valor_pago = float(d.get("valor_pago") or 0)
    db.session.commit()
    return jsonify({"ok": True, "id": reg.id})


@app.route("/api/darfs/<int:did>", methods=["DELETE"])
@login_required
def api_darf_desmarcar(did):
    reg = db.session.get(DarfPagamento, did)
    if not reg:
        abort(404)
    require_owner(db.session.get(Carteira, reg.carteira_id))
    db.session.delete(reg)
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Preço Teto (Bazin, Barsi, e outros métodos que vierem depois)
# --------------------------------------------------------------------------- #

METODOS_TETO = {"bazin", "barsi", "fluxo_descontado", "cresc5"}


@app.route("/preco-teto")
@login_required
def preco_teto():
    return render_template("preco_teto.html", usuario=current_user.nome)


@app.route("/api/carteira/<int:cid>/preco-teto")
@login_required
def api_preco_teto(cid):
    c = get_carteira_or_404(cid)
    require_owner(c)
    assets = Asset.query.filter_by(carteira_id=cid).order_by(Asset.ordem, Asset.id).all()
    asset_ids = [a.id for a in assets]
    premissas = PrecoTetoPremissa.query.filter(PrecoTetoPremissa.asset_id.in_(asset_ids)).all() \
        if asset_ids else []
    por_asset = {}
    for p in premissas:
        try:
            dados = json.loads(p.dados_json or "{}")
        except (TypeError, ValueError):
            dados = {}
        por_asset.setdefault(p.asset_id, {})[p.metodo] = dados
    ativos_out = [{
        "id": a.id, "ticker": a.ticker, "classe": a.classe or "", "preco": a.preco or 0,
        "num_acoes": a.num_acoes or 0, "premissas": por_asset.get(a.id, {}),
        "oculto": bool(a.oculto_preco_teto),
    } for a in assets]
    return jsonify({
        "ativos": ativos_out,
        "carteira": {"id": c.id, "nome": c.nome, "dono": c.dono.nome, "moeda": c.moeda or "BRL"},
        "editavel": c.user_id == current_user.id,
    })


@app.route("/api/carteira/<int:cid>/preco-teto/limpar", methods=["POST"])
@login_required
def api_preco_teto_limpar(cid):
    """'Limpar tabela': oculta TODOS os ativos da lista de preço teto numa vez (não apaga
    nada — os ativos continuam existindo no Rebalanceamento e dá pra reexibir um por um)."""
    c = get_carteira_or_404(cid)
    require_owner(c)
    Asset.query.filter_by(carteira_id=cid).update({"oculto_preco_teto": 1})
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/preco-teto", methods=["POST"])
@login_required
def api_preco_teto_salvar():
    d = request.get_json(force=True) or {}
    asset_id = d.get("asset_id")
    metodo = d.get("metodo")
    a = db.session.get(Asset, int(asset_id) if asset_id else 0)
    if not a:
        abort(404)
    require_owner(a.carteira)
    if metodo not in METODOS_TETO:
        return jsonify({"erro": "método inválido"}), 400
    dados = d.get("dados") or {}
    if not isinstance(dados, dict):
        return jsonify({"erro": "dados inválidos"}), 400
    premissa = PrecoTetoPremissa.query.filter_by(asset_id=a.id, metodo=metodo).first()
    if not premissa:
        premissa = PrecoTetoPremissa(asset_id=a.id, metodo=metodo)
        db.session.add(premissa)
    premissa.dados_json = json.dumps({k: float(v or 0) for k, v in dados.items()})
    db.session.commit()
    return jsonify({"ok": True, "id": premissa.id})


@app.route("/api/investidor10/<ticker>")
@login_required
def api_investidor10(ticker):
    """Busca dados do Investidor10 pra preencher as premissas de preço teto na hora
    (botão 'Buscar dados' da aba) — não salva nada sozinho, só devolve pro front."""
    return jsonify(fetch_investidor10(ticker))


@app.route("/api/investidor10-historico/<ticker>")
@login_required
def api_investidor10_historico(ticker):
    """Histórico de 5 anos (crescimento de lucro e ROE, consistência de dividendos)
    pro método 'Crescimento Histórico' da aba Preço Teto."""
    return jsonify(fetch_investidor10_historico(ticker))


def ensure_schema():
    """Adiciona colunas novas em tabelas já existentes (SQLite e PostgreSQL)."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)

    def add(tabela, coluna, ddl):
        if insp.has_table(tabela):
            cols = [c["name"] for c in insp.get_columns(tabela)]
            if coluna not in cols:
                db.session.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {ddl}"))
                db.session.commit()

    add("carteira", "renda_ref", "renda_ref FLOAT DEFAULT 3457")
    add("carteira", "renda_ref_json", "renda_ref_json TEXT")
    add("carteira", "anos_json", "anos_json TEXT")
    add("carteira", "moeda", "moeda VARCHAR(3) DEFAULT 'BRL'")
    add("rent_ativo", "is_caixa", "is_caixa INTEGER DEFAULT 0")
    add("rent_ativo", "classe", "classe VARCHAR(30) DEFAULT ''")
    add("rent_ativo", "inicio_ano", "inicio_ano INTEGER")
    add("rent_ativo", "inicio_mes", "inicio_mes INTEGER")
    add("asset", "oculto_preco_teto", "oculto_preco_teto INTEGER DEFAULT 0")
    add("opcao_op", "preco_medio", "preco_medio FLOAT DEFAULT 0")
    add("opcao_op", "custo_exercicio", "custo_exercicio FLOAT DEFAULT 0")
    if insp.has_table("rent_ativo"):
        cols = [c["name"] for c in insp.get_columns("rent_ativo")]
        if "classe" in cols and "is_caixa" in cols:
            db.session.execute(text(
                "UPDATE rent_ativo SET classe='Caixa' WHERE is_caixa=1 "
                "AND (classe IS NULL OR classe='')"))
            db.session.commit()


with app.app_context():
    db.create_all()
    ensure_schema()


if __name__ == "__main__":
    print("\n  Carteira de Investimentos:  http://127.0.0.1:5001\n")
    app.run(host="0.0.0.0", port=5001, debug=False)
