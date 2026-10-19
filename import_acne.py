# -*- coding: utf-8 -*-
"""
Importador Acne Studios -> Stivali (Nuvemshop), rodando no GitHub Actions.

Como funciona (econômico em token: a leitura cara foi feita antes; aqui o GitHub
faz o trabalho de graça):
  - Lê um arquivo de dados JSON (ex.: acne_saias.json) com os produtos prontos
    (nome, cor em INGLÊS, preço em R$, descrição, SEO, tamanhos, categoria e a
    URL da pagina do produto na Acne).
  - Para cada produto, baixa a pagina da Acne e extrai as fotos na ordem certa:
    1) PACKSHOT (peça sozinha, _FLAT) e 2) MODELO (_A), depois _B/_C.
  - Cria o produto na loja via API da Nuvemshop (cor sempre em inglês).
  - Pula produtos que já existem na loja (pelo nome), então pode rodar de novo
    sem duplicar.

Uso:
  python import_acne.py --arquivo acne_saias.json            (teste: não cria)
  python import_acne.py --arquivo acne_saias.json --live     (cria de verdade)

Precisa do segredo NUVEMSHOP_ACCESS_TOKEN no repositório.
"""
import argparse, base64, json, os, re, sys, time
import requests

STORE_ID = "3277373"
CONTATO = "contato@stivaliofficial.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def baixar_imagem(url):
    """Baixa a foto e devolve (base64, nome). Assim enviamos o ARQUIVO para a Nuvemshop
    em vez do link, driblando o filtro que bloqueia URLs com '/ad/'. Reduz p/ 1200px."""
    u = url.split("?")[0] + "?sw=1200"
    nome = url.split("?")[0].rsplit("/", 1)[-1]
    try:
        r = SESSION.get(u, timeout=45)
        if r.status_code == 200 and r.content and len(r.content) > 800:
            return base64.b64encode(r.content).decode(), nome
    except Exception:  # noqa
        pass
    return None, nome


# ----------------------------------------------------------------- fotos Acne
def extrair_fotos(acne_url, mpn, max_fotos=6):
    """Baixa a pagina da Acne e devolve as URLs das fotos do produto na ordem:
    PACKSHOT (_FLAT) primeiro, depois MODELO (_A), depois _B/_C/_D..."""
    try:
        r = SESSION.get(acne_url, timeout=40)
        r.raise_for_status()
        html = r.text
    except Exception as e:  # noqa
        print(f"    ! nao consegui abrir a pagina da Acne ({e})")
        return []
    # Pega o ENDERECO REAL de cada foto direto da pagina (sem montar o caminho na mao):
    # qualquer imagem /2000x/ cujo arquivo seja {MPN}_{vista}.jpg. Funciona para qualquer
    # estrutura de pasta e lida com sufixos unissex (UNISEX-WOMAN_A etc).
    pat = re.compile(
        r'(https://www\.acnestudios\.com/dw/image/v2/[^\s"\'<>]*?/2000x/' +
        re.escape(mpn) + r'_([A-Za-z0-9][A-Za-z0-9_\-]*?)\.jpg)', re.I)
    achadas = {}  # vista (A, FLAT, Y, G...) -> url limpa
    for m in pat.finditer(html):
        url = m.group(1).split("?")[0]
        view = m.group(2).upper().split("_")[-1]   # pega a vista real mesmo em UNISEX-WOMAN_A
        if view == "HOVER":
            continue
        if view not in achadas:
            achadas[view] = url
    # ordem: PECA SOZINHA primeiro (FLAT/W/X/Y/Z), depois MODELO (A/B/C/D/E/F/G)
    ordem = ["FLAT", "W", "X", "Y", "Z", "A", "B", "C", "D", "E", "F", "G"]
    urls = [achadas[v] for v in ordem if v in achadas]
    urls += [u for v, u in achadas.items() if v not in ordem]
    return urls[:max_fotos]


# ------------------------------------------------------------- Nuvemshop API
class Nuvemshop:
    def __init__(self, store_id, token, contato):
        self.base = f"https://api.nuvemshop.com.br/v1/{store_id}"
        self.h = {"Authentication": f"bearer {token}", "Content-Type": "application/json",
                  "User-Agent": f"STIVALI Acne Importer ({contato})"}

    def _req(self, method, path, tries=5, **kw):
        for n in range(tries):
            r = requests.request(method, f"{self.base}{path}", headers=self.h, **kw)
            if r.status_code in (429, 502, 503, 504) and n < tries - 1:
                espera = float(r.headers.get("Retry-After", 0) or 0) or min(60, 4 * 2 ** n)
                print(f"    (Nuvemshop respondeu {r.status_code}; aguardando {espera:.0f}s)")
                time.sleep(espera)
                continue
            return r
        return r

    def exists(self, name):
        r = self._req("GET", "/products", params={"q": name, "per_page": 10, "fields": "id,name"}, timeout=30)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        for p in r.json():
            n = p.get("name", {})
            if any(str(v).strip().upper() == name.strip().upper() for v in n.values()):
                return True
        return False

    def find_by_name(self, name):
        r = self._req("GET", "/products", params={"q": name, "per_page": 10, "fields": "id,name"}, timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        ids = []
        for p in r.json():
            n = p.get("name", {})
            if any(str(v).strip().upper() == name.strip().upper() for v in n.values()):
                ids.append(p["id"])
        return ids

    def delete(self, pid):
        r = self._req("DELETE", f"/products/{pid}", timeout=60)
        return r.status_code in (200, 204, 404)

    def create(self, payload):
        r = self._req("POST", "/products", json=payload, timeout=300)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
        return r.json()


SELO = ("<p><br></p><hr><p><strong>\u2713 Produto 100% original, verificado pela nossa "
        "curadoria antes do envio.<br>\u2713 Compra 100% segura \u00b7 Em at\u00e9 12x no "
        "cart\u00e3o \u00b7 Envio para todo o Brasil</strong></p>")


def monta_descricao(p):
    """Monta o HTML no formato padrao: ACNE STUDIOS / NOME / DESCRICAO / CAIMENTO & MATERIAL / selo."""
    nome = p["name"]
    desc = p["descricao"]
    bullets = "".join(f"\u2022 {b}<br>" for b in p["caimento_material"]).rstrip("<br>")
    return (f"<p><strong>ACNE STUDIOS</strong></p><p><strong>{nome}</strong></p><p><br></p>"
            f"<h2><strong>DESCRI\u00c7\u00c3O</strong></h2><p>{desc}</p><p><br></p>"
            f"<h2><strong>CAIMENTO &amp; MATERIAL</strong></h2><p>{bullets}</p>" + SELO)


def _montar_imagens(imgs):
    out = []
    for i, u in enumerate(imgs):
        b64, nome = baixar_imagem(u)
        if b64:
            out.append({"attachment": b64, "filename": nome, "position": i + 1})
        else:
            out.append({"src": u, "position": i + 1})
    return out


def monta_payload(p, imgs):
    cor = p["color"]
    variants = []
    for t in p["sizes"]:
        variants.append({"price": f"{p['price_brl']:.2f}", "stock": p.get("stock", 1),
                         "values": [{"pt": cor}, {"pt": t}],
                         "weight": p.get("weight", 0.6), "width": 28, "height": 6, "depth": 30})
    return {
        "name": {"pt": p["name"]},
        "description": {"pt": monta_descricao(p)},
        "brand": "Acne Studios",
        "published": True,
        "requires_shipping": True,
        "attributes": [{"pt": "Cor"}, {"pt": "Tamanho"}],
        "categories": [p["category_id"]],
        "seo_title": {"pt": p["seo_title"]},
        "seo_description": {"pt": p["seo_description"]},
        "tags": ", ".join(p.get("tags", [])),
        "variants": variants,
        "images": _montar_imagens(imgs),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arquivo", required=True, help="JSON de produtos, ex.: acne_saias.json")
    ap.add_argument("--live", action="store_true", help="criar de verdade (sem isso, so testa)")
    ap.add_argument("--substituir", action="store_true", help="se o produto ja existe, apaga e cria de novo (corrige ordem de fotos/preco)")
    args = ap.parse_args()

    produtos = json.load(open(args.arquivo, encoding="utf-8-sig"))
    token = os.environ.get("NUVEMSHOP_ACCESS_TOKEN", "")
    if args.live and not token:
        sys.exit("Sem NUVEMSHOP_ACCESS_TOKEN: nao da para criar. Configure o segredo no repositorio.")
    ns = Nuvemshop(STORE_ID, token, CONTATO) if token else None

    print(f"== Acne importer | {len(produtos)} produto(s) | modo: {'LIVE (cria na loja)' if args.live else 'TESTE (nao cria)'} ==\n")
    criados = pulados = falhas = 0
    for i, p in enumerate(produtos, 1):
        nome = p["name"]
        mpn = p["mpn"]
        print(f"[{i}/{len(produtos)}] {nome}  ->  R$ {p['price_brl']:.0f}  ({p['color']})")
        imgs = p.get("images") or extrair_fotos(p["acne_url"], mpn)
        if imgs:
            suf1 = imgs[0].rsplit('_',1)[-1].replace('.jpg','').upper()
            eh_modelo = suf1 in ('A','B','C','D','E')
            print(f"    fotos: {len(imgs)} (1a = {'MODELO' if eh_modelo else 'PECA SOZINHA'})")
        else:
            print("    fotos: NENHUMA encontrada (confira a URL/MPN)")
        if not args.live:
            continue
        try:
            existentes = ns.find_by_name(nome)
            if existentes and not args.substituir:
                print("    ja existe na loja -> pulando"); pulados += 1; continue
            if existentes and args.substituir:
                for pid in existentes:
                    ns.delete(pid)
                print(f"    (apaguei {len(existentes)} versao(oes) antiga(s) para recriar)")
            ns.create(monta_payload(p, imgs))
            print("    + criado"); criados += 1
            time.sleep(1)
        except Exception as e:  # noqa
            print(f"    ! falhou: {e}"); falhas += 1
    print(f"\nResumo: {criados} criado(s), {pulados} pulado(s) (ja existiam), {falhas} falha(s).")


if __name__ == "__main__":
    main()
