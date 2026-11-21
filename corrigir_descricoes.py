"""
Corrige descricoes ja criadas na Nuvemshop que ficaram em 1a pessoa
('nosso/nossa/meu/minha' referindo a MARCA) -> 3a pessoa.

- A Stivali REVENDE: a marca nao e nossa. Mas o SELO da Stivali
  ('verificado pela nossa curadoria') e da propria Stivali e deve ser
  PRESERVADO. Por isso so limpamos a parte ANTES do <hr> (a descricao do
  produto); o selo (depois do <hr>) fica intacto.
- Varre todos os produtos, corrige so os que precisam.
- Sem --aplicar: modo TESTE (so conta e mostra exemplos, nao altera nada).

Uso:  python corrigir_descricoes.py            (teste)
      python corrigir_descricoes.py --aplicar   (altera de verdade)
"""

import argparse
import os
import sys

from import_acne import Nuvemshop, _limpar_primeira_pessoa, STORE_ID, CONTATO


def corrige_descricao(desc_html):
    """Limpa a 1a pessoa so na parte antes do <hr> (preserva o selo)."""
    if not desc_html:
        return desc_html, False
    import re
    m = re.search(r"<hr\s*/?>", desc_html, re.I)
    if m:
        corpo, selo = desc_html[:m.start()], desc_html[m.start():]
    else:
        corpo, selo = desc_html, ""
    novo_corpo = _limpar_primeira_pessoa(corpo)
    novo = novo_corpo + selo
    return novo, (novo != desc_html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true",
                    help="altera de verdade (sem isso, so conta)")
    args = ap.parse_args()

    token = os.environ.get("NUVEMSHOP_ACCESS_TOKEN", "")
    if not token:
        sys.exit("Sem NUVEMSHOP_ACCESS_TOKEN: configure o segredo no repositorio.")
    ns = Nuvemshop(STORE_ID, token, CONTATO)

    print(f"== Corretor de descricoes | modo: {'APLICAR' if args.aplicar else 'TESTE (nao altera)'} ==\n")
    total = precisam = corrigidos = falhas = 0
    exemplos = 0
    page = 1
    while True:
        r = ns._req("GET", "/products", params={"per_page": 200, "page": page,
                    "fields": "id,name,description"}, timeout=60)
        if r.status_code == 404:
            break
        r.raise_for_status()
        prods = r.json()
        if not prods:
            break
        for p in prods:
            total += 1
            desc = p.get("description") or {}
            if not isinstance(desc, dict):
                continue
            mudou_algum = {}
            tem_mudanca = False
            for lang, html in desc.items():
                novo, mudou = corrige_descricao(html)
                mudou_algum[lang] = novo
                if mudou:
                    tem_mudanca = True
            if not tem_mudanca:
                continue
            precisam += 1
            nome = next(iter((p.get("name") or {}).values()), p["id"])
            if exemplos < 10:
                exemplos += 1
                print(f"  [{precisam}] {nome}")
            if args.aplicar:
                up = ns._req("PUT", f"/products/{p['id']}",
                             json={"description": mudou_algum}, timeout=60)
                if up.status_code < 400:
                    corrigidos += 1
                else:
                    falhas += 1
                    print(f"      ! falha {up.status_code} no id {p['id']}")
        page += 1

    print(f"\nResumo: {total} produto(s) varrido(s) | {precisam} precisavam de correcao"
          f" | {corrigidos} corrigido(s) | {falhas} falha(s)")
    if not args.aplicar and precisam:
        print("(modo TESTE - rode com 'Aplicar de verdade' marcado para corrigir)")


if __name__ == "__main__":
    main()
