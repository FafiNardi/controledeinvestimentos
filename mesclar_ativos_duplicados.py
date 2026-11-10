"""Junta ativos duplicados (mesmo nome, mesma carteira) que ficaram com históricos
separados por terem sido fechados e reabertos antes da correção que reaproveita
o registro existente automaticamente.

Uso:
  python3 mesclar_ativos_duplicados.py            # mostra o que seria feito, não altera nada
  python3 mesclar_ativos_duplicados.py --aplicar   # aplica de verdade
"""
import sys
from collections import defaultdict

from sqlalchemy import text

from app import app, db, RentAtivo, RentMov

APLICAR = "--aplicar" in sys.argv

with app.app_context():
    ativos = RentAtivo.query.order_by(RentAtivo.carteira_id, RentAtivo.id).all()
    grupos = defaultdict(list)
    for a in ativos:
        grupos[(a.carteira_id, a.nome.strip().lower())].append(a)

    total_grupos = 0
    for (cid, nome_lower), grupo in grupos.items():
        if len(grupo) < 2:
            continue
        total_grupos += 1
        grupo.sort(key=lambda a: a.id)
        primario = grupo[0]
        duplicados = grupo[1:]
        print(f"\nCarteira {cid} — '{primario.nome}': ativo principal id={primario.id}, "
              f"duplicados={[d.id for d in duplicados]}")
        for dup in duplicados:
            for mv in list(RentMov.query.filter_by(ativo_id=dup.id)):
                conflito = RentMov.query.filter_by(
                    ativo_id=primario.id, ano=mv.ano, mes=mv.mes).first()
                if conflito:
                    print(f"  CONFLITO em {mv.ano}-{mv.mes:02d}: já existe lançamento no "
                          f"ativo principal (id {primario.id}); mantendo o do principal e "
                          f"IGNORANDO o do duplicado (id {dup.id}) — revisar manualmente.")
                    continue
                print(f"  move lançamento {mv.ano}-{mv.mes:02d} do ativo {dup.id} -> {primario.id}")
                if APLICAR:
                    # update direto via SQL, sem passar pelas coleções do SQLAlchemy: como
                    # RentAtivo.movs usa cascade="all, delete-orphan", mexer nas coleções em
                    # memória (remove/append) faz o SQLAlchemy tratar o lançamento como órfão
                    # e apagá-lo por engano, em vez de só mover pra outro ativo
                    db.session.execute(
                        text("UPDATE rent_mov SET ativo_id=:novo WHERE id=:id"),
                        {"novo": primario.id, "id": mv.id})
            if not primario.classe and dup.classe:
                if APLICAR:
                    primario.classe = dup.classe
                print(f"  copia classe '{dup.classe}' do duplicado pro principal")
            if dup.inicio_ano and (not primario.inicio_ano or
                                    (dup.inicio_ano, dup.inicio_mes or 1) <
                                    (primario.inicio_ano, primario.inicio_mes or 1)):
                print(f"  ajusta início efetivo do principal para {dup.inicio_ano}-{dup.inicio_mes}")
                if APLICAR:
                    primario.inicio_ano, primario.inicio_mes = dup.inicio_ano, dup.inicio_mes
            if APLICAR:
                db.session.commit()
                db.session.refresh(dup)
            print(f"  remove ativo duplicado id={dup.id}"
                  + (f" (ainda tem {len(dup.movs)} lançamento(s) em conflito, "
                     "serão apagados junto)" if APLICAR and dup.movs else ""))
            if APLICAR:
                db.session.delete(dup)
                db.session.commit()

    if total_grupos == 0:
        print("Nenhum ativo duplicado encontrado.")
    elif not APLICAR:
        print(f"\n{total_grupos} grupo(s) de duplicados encontrados. "
              f"Rode com --aplicar para mesclar de verdade.")
    else:
        print(f"\n{total_grupos} grupo(s) mesclados com sucesso.")
