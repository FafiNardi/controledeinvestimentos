"""
Carteira de Investimentos - Rebalanceamento Dinâmico (multi-usuário)

- Login / cadastro (cada pessoa cria sua conta)
- Cada usuário cria várias carteiras; todos veem o rebalanceamento de todos
  (somente o dono edita a própria carteira)
- Cotação e Valor Patrimonial em tempo real (Yahoo Finance + Fundamentus)
- Roda com SQLite local OU PostgreSQL na nuvem (Render) via DATABASE_URL
"""
import os
import re
from datetime import datetime

import requests
import yfinance as yf
from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, flash, abort)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")

# Banco: PostgreSQL na nuvem (DATABASE_URL) ou SQLite local
db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("postgres://"):           # Render usa esse prefixo antigo
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url or "sqlite:///" + os.path.join(BASE_DIR, "carteira.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

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
    assets = db.relationship("Asset", backref="carteira", cascade="all, delete-orphan")


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


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# --------------------------------------------------------------------------- #
# Cotações (Yahoo Finance + Fundamentus)
# --------------------------------------------------------------------------- #

def yahoo_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    if "." in t or "-" in t:
        return t
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


def fetch_quote(ticker: str) -> dict:
    symbol = yahoo_symbol(ticker)
    out = {"preco": None, "vpa": None, "pvp": None, "dy": None, "erro": None}
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
        if not vpa and ticker.strip().upper().endswith("11"):
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
    """Todas as carteiras de todos (para o seletor)."""
    out = []
    for c in Carteira.query.order_by(Carteira.id).all():
        out.append({"id": c.id, "nome": c.nome, "dono": c.dono.nome,
                    "user_id": c.user_id, "minha": c.user_id == current_user.id})
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
    assets = Asset.query.filter_by(carteira_id=cid).order_by(Asset.ordem, Asset.id).all()
    computed, total = compute_rows(assets, c.carteira_ideal or 0)
    return jsonify({
        "carteira": {"id": c.id, "nome": c.nome, "dono": c.dono.nome,
                     "carteira_ideal": c.carteira_ideal or 0,
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
        q = fetch_quote(ticker)
        if q["preco"]:
            a.preco, a.vpa, a.pvp, a.dy = q["preco"], q["vpa"] or 0, q["pvp"] or 0, q["dy"] or 0
            a.updated_at = datetime.now().isoformat(timespec="seconds")
            db.session.commit()
    return jsonify({"ok": True, "id": a.id})


EDITABLE = {"ticker", "classe", "segmento", "num_acoes", "preco_medio",
            "pct_ideal", "preco", "manual", "vpa", "vpa_manual"}
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
    get_carteira_or_404(cid)  # qualquer logado pode atualizar cotações (dado público)
    assets = Asset.query.filter_by(carteira_id=cid, manual=0).all()
    resultados = []
    for a in assets:
        q = fetch_quote(a.ticker)
        if q["preco"]:
            a.preco = q["preco"]
            a.vpa = a.vpa if a.vpa_manual else (q["vpa"] or 0)
            a.pvp = q["pvp"] or 0
            a.dy = q["dy"] or 0
            a.updated_at = datetime.now().isoformat(timespec="seconds")
        resultados.append({"ticker": a.ticker, **q})
    db.session.commit()
    return jsonify({"ok": True, "resultados": resultados})


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    print("\n  Carteira de Investimentos:  http://127.0.0.1:5001\n")
    app.run(host="0.0.0.0", port=5001, debug=False)
