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
import html as _htmlmod
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
    base = url.split("?")[0]
    nome = base.rsplit("/", 1)[-1]
    u = base + "?sw=1200" if "acnestudios.com" in url else base
    for _tentativa in range(2):
        try:
            r = SESSION.get(u, timeout=45)
            if r.status_code == 200 and r.content and len(r.content) > 800:
                return base64.b64encode(r.content).decode(), nome
            if r.status_code == 404:
                break
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


# ------------------------------------------------- descricao oficial + traducao
def _html_para_texto(html):
    """Converte o body_html do produto (descricao oficial da marca) em texto limpo,
    preservando quebras de linha e marcadores de lista."""
    if not html:
        return ""
    t = re.sub(r"(?i)<\s*br\s*/?>", "\n", html)
    t = re.sub(r"(?i)<\s*li[^>]*>", "• ", t)
    t = re.sub(r"(?i)</\s*(p|li|div|h[1-6]|ul|ol)\s*>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _htmlmod.unescape(t)
    linhas = [l.strip() for l in t.split("\n")]
    return "\n".join(l for l in linhas if l)


_TRAD_CACHE = {}


def traduzir(texto, sl="en", tl="pt"):
    """Traduz EN->PT usando o endpoint publico gratuito do Google (sem chave).
    Em caso de erro, devolve o texto original (nunca inventa)."""
    texto = (texto or "").strip()
    if not texto:
        return ""
    if texto in _TRAD_CACHE:
        return _TRAD_CACHE[texto]
    out = texto
    try:
        r = SESSION.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": texto[:4800]},
            timeout=25)
        if r.status_code == 200:
            data = r.json()
            juntado = "".join(seg[0] for seg in data[0] if seg and seg[0])
            if juntado.strip():
                out = juntado
    except Exception:  # noqa
        out = texto
    _TRAD_CACHE[texto] = out
    return out


# ------------------------------------------------- traducao de tamanhos (BR)
_TAM_LETRA = {
    "xxs": "PP", "xs": "PP", "s": "P", "m": "M", "l": "G", "xl": "GG",
    "xxl": "2GG", "2xl": "2GG", "xxxl": "3GG", "3xl": "3GG",
    "xxxxl": "4GG", "4xl": "4GG",
}


# conversao polegada (cintura) -> numeracao BR de calcas (BR usa so pares)
_JEANS_BR = {
    24: "34", 25: "34", 26: "36", 27: "36", 28: "38", 29: "38",
    30: "40", 31: "40", 32: "42", 33: "42", 34: "44", 35: "44",
    36: "46", 37: "46", 38: "48", 39: "48", 40: "50", 41: "50", 42: "52",
}


def _letra_br(s):
    """Traduz tamanho em letra para BR, lidando com combinados tipo 'XS/S' -> 'PP/P'."""
    partes = [p.strip() for p in s.split("/")] if "/" in s else [s]
    return "/".join(_TAM_LETRA.get(p.lower().replace(" ", ""), p.upper()) for p in partes)


def _traduz_tamanho(raw, mode):
    """Traduz o tamanho do site para o padrao BR conforme o modo:
      apparel    -> S/M/L/XL/XXL = P/M/G/GG/2GG
      shoe_eu    -> BR = EU - 2  (37 EU -> 35 BR)
      jeans_waist-> polegada -> BR por faixa (25->34, 27->36, 28->38);
                    se vier letra (jogger), cai no mapa apparel.
      none       -> mantem como esta."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("default title", "title"):
        return "Único"
    if mode == "apparel":
        return _letra_br(s)
    if mode == "shoe_eu":
        m = re.search(r"\d+(?:[.,]5)?", s)
        if m:
            val = float(m.group().replace(",", ".")) - 2
            return str(int(val)) if val == int(val) else str(val).replace(".0", "")
        return s
    if mode == "jeans_waist":
        m = re.search(r"\d+", s)
        if m:
            n = int(m.group())
            return _JEANS_BR.get(n, str(n + 9))  # fora da tabela: +9 como aproximacao
        return _letra_br(s)  # jogger S/M/L (e combinados)
    return s


def _size_pos(prod):
    """Descobre em qual opcao (1,2,3) esta o tamanho. Se nao houver opcao chamada
    'Size' mas houver varias variantes numa unica opcao, assume a opcao 1."""
    opts = prod.get("options", [])
    for idx, o in enumerate(opts, start=1):
        nome = (o.get("name") if isinstance(o, dict) else str(o)) or ""
        if nome.strip().lower() in ("size", "talla", "taille", "größe",
                                    "grosse", "tamanho", "tamanhos", "size eu", "us size"):
            return idx
    if len(opts) == 1 and len(prod.get("variants", [])) > 1:
        return 1
    return None


# vocabulario de cores p/ extrair a cor do handle/tags quando o titulo so traz o modelo
_CORES = {
    "black": "Black", "brown": "Brown", "dark": "Dark", "red": "Red", "beige": "Beige",
    "white": "White", "grey": "Grey", "gray": "Gray", "green": "Green", "blue": "Blue",
    "navy": "Navy", "tan": "Tan", "camel": "Camel", "cream": "Cream", "pink": "Pink",
    "silver": "Silver", "gold": "Gold", "khaki": "Khaki", "burgundy": "Burgundy",
    "ecru": "Ecru", "chocolate": "Chocolate", "taupe": "Taupe", "ivory": "Ivory",
    "orange": "Orange", "yellow": "Yellow", "purple": "Purple", "sand": "Sand",
    "olive": "Olive", "nude": "Nude", "mocha": "Mocha", "bordeaux": "Bordeaux",
    "charcoal": "Charcoal", "stone": "Stone", "sage": "Sage", "rust": "Rust",
    "wine": "Wine", "teal": "Teal", "maroon": "Maroon", "aqua": "Aqua",
    "mint": "Mint", "coral": "Coral", "lilac": "Lilac", "mustard": "Mustard",
    "denim": "Denim", "indigo": "Indigo", "plum": "Plum", "peach": "Peach",
    "turquoise": "Turquoise", "emerald": "Emerald", "bronze": "Bronze",
    "ecru": "Ecru", "offwhite": "Off White",
}


def _cor_do_produto(prod):
    """Descobre a cor de um produto Shopify quando o titulo so traz o modelo.
    Prioridade: opcao 'Color' -> tag de cor -> cor no handle. Devolve '' se nada."""
    # 1) opcao chamada Color/Colour/Cor
    for opt in prod.get("options", []):
        if (opt.get("name") or "").strip().lower() in ("color", "colour", "cor"):
            vals = opt.get("values") or []
            if vals and str(vals[0]).strip().lower() not in ("default title", "title", ""):
                return str(vals[0]).strip().title()
    # 2) tag de cor (ex 'color:Black', 'Brown Dark')
    for tag in prod.get("tags", []) if isinstance(prod.get("tags"), list) else str(prod.get("tags", "")).split(","):
        t = str(tag).strip()
        base = t.split(":")[-1].strip()
        toks = [w for w in re.split(r"[\s\-]+", base.lower()) if w in _CORES]
        if toks:
            return " ".join(_CORES[w] for w in toks)
    # 3) handle (ex copba01405-calf-leather-black-dark)
    toks = [w for w in (prod.get("handle") or "").lower().split("-") if w in _CORES]
    if toks:
        return " ".join(_CORES[w] for w in toks)
    # 4) titulo (ex 'Austin Grime Blue Bomber')
    toks = [w for w in re.split(r"[\s\-]+", (prod.get("title") or "").lower()) if w in _CORES]
    if toks:
        return " ".join(_CORES[w] for w in toks)
    # 5) nome do arquivo das imagens (ex .../261U139-BLACK-1.jpg)
    for im in prod.get("images", []) or []:
        src = (im.get("src") if isinstance(im, dict) else str(im)) or ""
        toks = [w for w in re.split(r"[\s/\-_.]+", src.lower()) if w in _CORES]
        if toks:
            return " ".join(dict.fromkeys(_CORES[w] for w in toks))
    return ""


def _so_cores_conhecidas(texto):
    """Devolve a cor so se o texto contiver palavra(s) de cor conhecida(s).
    Assim descartamos lixo de tema tipo 'defaultNegativePercentage'."""
    toks = [w for w in re.split(r"[\s/\-_]+", (texto or "").lower()) if w]
    cor = [t for t in toks if t in _CORES]
    return " ".join(_CORES[t] for t in cor) if cor else ""


_DBG_COR = {"n": 0}


def _cor_da_pagina(url_produto, debug=False):
    """Ultimo recurso: abre a pagina do produto e tenta achar a cor (metafield
    que nao vem no products.json). So aceita nomes de cor REAIS (via _CORES),
    descartando variaveis de tema. Devolve '' se nao achar (nunca inventa)."""
    try:
        html = SESSION.get(url_produto, timeout=30).text
    except Exception:  # noqa
        return ""
    cands = []
    cands += re.findall(r'Colou?r\s*[:\-]\s*([A-Za-z][A-Za-z /]{1,24})', html)
    cands += re.findall(r'"colou?r"\s*:\s*"([^"]{2,40})"', html, re.I)
    cands += re.findall(r'"colou?r"[^{}]{0,60}?"value"\s*:\s*"([^"]{2,40})"', html, re.I)
    cands += re.findall(r'data-colou?r=["\']([A-Za-z][A-Za-z /]{1,24})', html, re.I)
    cands += re.findall(r'aria-label=["\']\s*Colou?r:?\s*([A-Za-z][A-Za-z /]{1,24})', html, re.I)
    cands += re.findall(r'(?i)\b(?:colou?r|cor)\b[^<>{}\"]{0,20}?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', html)
    for c in cands:
        cor = _so_cores_conhecidas(c)
        if cor:
            return cor
    if debug and _DBG_COR["n"] < 3:
        _DBG_COR["n"] += 1
        low = html.lower()
        i = low.find("colo")
        snip = html[max(0, i - 25):i + 110].replace("\n", " ") if i >= 0 else "(sem 'colo' na pagina)"
        print(f"      [debug cor] {url_produto.rsplit('/', 1)[-1]}: {snip!r}")
    return ""


def _norm_img(src):
    src = (src or "").split("?")[0]
    if src.startswith("//"):
        src = "https:" + src
    return src


def _shopify_products_robusto(colecao_url):
    """Devolve a lista de produtos de uma colecao Shopify, resistente a lojas que
    desativaram /products.json (ex: Coperni). Estrategia:
      1) tenta <colecao>/products.json paginado;
      2) se vier vazio, raspa os handles do HTML da colecao e puxa cada produto
         pelo endpoint /products/<handle>.js (que quase nunca e desativado).
    Normaliza tudo para o mesmo formato (title, handle, options, tags, variants
    com price em EUR string, images com src https)."""
    base = colecao_url.split("?")[0]
    # pagina HTML da colecao = tira o /products.json do fim, se houver
    col_html = re.sub(r"/products\.json$", "", base).rstrip("/")
    prods_json_url = col_html + "/products.json"

    # ---- 1) products.json ----
    achados = []
    for page in range(1, 8):
        try:
            r = SESSION.get(f"{prods_json_url}?limit=250&page={page}", timeout=40)
            if page == 1:
                print(f"    (products.json: HTTP {r.status_code}, {len(r.content)} bytes)")
            ps = r.json().get("products", []) if r.status_code == 200 else []
        except Exception as e:  # noqa
            if page == 1:
                print(f"    (products.json falhou: {e})")
            ps = []
        if not ps:
            break
        achados.extend(ps)
    if achados:
        return achados

    # ---- 2) fallback: handles do HTML + /products/<handle>.js ----
    handles = []
    vistos = set()
    for page in range(1, 10):
        try:
            html = SESSION.get(f"{col_html}?page={page}", timeout=40).text
        except Exception:  # noqa
            break
        novos = 0
        for h in re.findall(r"/products/([a-z0-9][a-z0-9\-]+)", html):
            if h not in vistos:
                vistos.add(h); handles.append(h); novos += 1
        if page == 1:
            print(f"    (HTML da colecao: {len(handles)} handle(s) na 1a pagina)")
        if novos == 0:
            break
    dominio = "https://" + col_html.split("/")[2]
    out = []
    for h in handles:
        try:
            j = SESSION.get(f"{dominio}/products/{h}.js", timeout=30).json()
        except Exception:  # noqa
            continue
        if not j.get("title"):
            continue
        cents = j.get("price")
        if cents is None:
            vs = j.get("variants") or [{}]
            cents = vs[0].get("price", 0)
        try:
            eur = float(cents) / 100.0
        except Exception:  # noqa
            eur = 0
        opts = []
        for o in j.get("options", []):
            if isinstance(o, dict):
                opts.append({"name": o.get("name", ""), "values": o.get("values", [])})
            else:
                opts.append({"name": str(o), "values": []})
        variants = []
        for v in j.get("variants", []):
            try:
                eurv = float(v.get("price", 0)) / 100.0
            except Exception:  # noqa
                eurv = eur
            try:
                cheio = float(v.get("compare_at_price") or 0) / 100.0
            except Exception:  # noqa
                cheio = 0
            vo = v.get("options") or []
            variants.append({
                "price": f"{eurv:.2f}", "available": v.get("available", True),
                "compare_at_price": (f"{cheio:.2f}" if cheio else None),
                "title": v.get("title"),
                "option1": vo[0] if len(vo) > 0 else v.get("option1"),
                "option2": vo[1] if len(vo) > 1 else v.get("option2"),
                "option3": vo[2] if len(vo) > 2 else v.get("option3"),
            })
        if not variants:
            variants = [{"price": f"{eur:.2f}", "available": j.get("available", True)}]
        out.append({
            "title": j.get("title", ""), "handle": j.get("handle", h),
            "options": opts, "tags": j.get("tags", []),
            "body_html": j.get("description", ""),
            "variants": variants,
            "images": [{"src": _norm_img(s)} for s in j.get("images", [])],
        })
    print(f"    (fallback .js: {len(out)} produto(s) montado(s) de {len(handles)} handle(s))")
    return out


def expandir_colecao(entry):
    """Le uma colecao inteira do Shopify (todos os produtos/cores) e devolve os itens
    prontos. Pula esgotados e kits/sets (por palavra-chave).

    Dois modos:
      - Legado (Rhode): sem 'name_prefix' -> comportamento antigo.
      - Generico (Coperni etc): com 'name_prefix'. Usa o preco do proprio JSON do
        Shopify (eur = preco da variante) quando 'use_json_price' = true, aplica a
        formula eur*eur_mult+eur_add, monta nome 'PREFIXO MODELO COR' e SEO forte."""
    url = entry["shopify_collection"].split("?")[0]
    skip = [k.lower() for k in entry.get("skip_keywords", [])]
    incl = [k.lower() for k in entry.get("include_keywords", [])]
    prefixo = entry.get("name_prefix")  # None -> modo legado Rhode

    # ---- modo generico (Coperni e futuras marcas Shopify) ----
    if prefixo:
        mult = entry.get("eur_mult", 6.5); add = entry.get("eur_add", 0)
        usa_json = entry.get("use_json_price", True)
        pmap = {k.lower(): v for k, v in entry.get("price_eur_map", {}).items()}
        default = entry.get("price_eur_default", 0)
        sizes = entry.get("sizes", ["\u00danico"])
        size_mode = entry.get("size_mode", "none")  # apparel|shoe_eu|jeans_waist|none
        brand = entry.get("brand", prefixo.title())
        header = entry.get("marca_header", prefixo.upper())
        secao = entry.get("secao", "DETALHES")
        tags = entry.get("tags", [prefixo.lower()])
        desc_tpl = entry.get("descricao_tpl",
            "{model}{cor_sp} da {brand} \u2014 pe\u00e7a original importada, pronta entrega.")
        seo_t_tpl = entry.get("seo_title_tpl", "{brand} {model}{cor_sp}")
        seo_d_tpl = entry.get("seo_description_tpl",
            "{model}{cor_sp} da {brand}. Original, importada, em at\u00e9 12x no cart\u00e3o. Compre na Stivali.")
        caimento = entry.get("caimento_material",
            ["Produto 100% original {brand}", "Importado \u00b7 pronta entrega"])
        vistos = set()
        out = []
        ignora_esg = entry.get("ignore_sold_out", False)
        limite = entry.get("limit")              # traz no maximo N pecas (lote)
        pular_n = entry.get("skip_primeiros", 0)  # pula as N primeiras (proximo lote)
        _tot = _disp = _elig = 0
        for prod in _shopify_products_robusto(url):
            if limite and len(out) >= limite:
                break
            title = (prod.get("title") or "").strip()
            tl = title.lower()
            if not title or any(k in tl for k in skip):
                continue
            if incl and not any(k in tl for k in incl):
                continue
            inc_h = [k.lower() for k in entry.get("include_handles", [])]
            if inc_h and not any(k in (prod.get("handle") or "").lower() for k in inc_h):
                continue
            _tot += 1
            vars_ = prod.get("variants", [])
            disp = (not vars_) or any(v.get("available", True) for v in vars_)
            if disp:
                _disp += 1
            if not disp and not ignora_esg:
                continue  # esgotado -> pula (a menos que ignore_sold_out)
            # preco em euro: usa o preco CHEIO (compare_at_price) quando houver
            # desconto; senao o preco normal. Pega a 1a variante disponivel.
            if usa_json and vars_:
                v0 = next((v for v in vars_ if v.get("available", True)), vars_[0])
                try:
                    preco = float(v0.get("price") or 0)
                    cheio = float(v0.get("compare_at_price") or 0)
                    eur = cheio if cheio > preco else preco
                except Exception:  # noqa
                    eur = default
            else:
                eur = default
                for k, v in pmap.items():
                    if k in tl:
                        eur = v; break
            if not eur:
                continue
            # tamanhos: monta a partir das variantes, traduz e pula numeracao esgotada
            if size_mode != "none":
                spos = _size_pos(prod)
                if spos:
                    sizes_out = []
                    for v in vars_:
                        if not ignora_esg and not v.get("available", True):
                            continue  # numeracao esgotada -> fora
                        tb = _traduz_tamanho(v.get(f"option{spos}"), size_mode)
                        if tb and tb not in sizes_out:
                            sizes_out.append(tb)
                    if not sizes_out:
                        continue  # todas as numeracoes esgotadas -> pula a peca
                else:
                    sizes_out = sizes
            else:
                sizes_out = sizes
            # controle de lote: pula as N primeiras pecas elegiveis (proximo lote)
            _elig += 1
            if _elig <= pular_n:
                continue
            # cor: override manual (por codigo OU nome da peca) tem prioridade; depois deteccao
            cor = ""
            h_low = (prod.get("handle") or "").lower()
            alvo_ov = h_low + " " + tl  # casa por codigo do handle ou por palavra do nome
            for chave, val in entry.get("color_overrides", {}).items():
                if chave.lower() in alvo_ov:
                    cor = val; break
            if not cor:
                cor = _cor_do_produto(prod)
            if not cor and entry.get("cor_da_pagina") and prod.get("handle"):
                dominio = "https://" + url.split("/")[2]
                cor = _cor_da_pagina(f"{dominio}/products/{prod['handle']}")
            if not cor and entry.get("debug_cor") and _DBG_COR["n"] < 4:
                _DBG_COR["n"] += 1
                _ims = prod.get("images") or []
                _im0 = (_ims[0].get("src") if _ims and isinstance(_ims[0], dict) else "")
                print(f"      [debug cor] {prod.get('handle')} | type={prod.get('product_type')!r}"
                      f" | tags={prod.get('tags')!r} | img={_im0}")
            if not cor:
                cor = entry.get("cor_default", "")  # cor padrao quando nao da p/ detectar
            model = title
            cor_sp = (" " + cor) if cor else ""
            # nao repete o prefixo se o titulo da peca ja comeca com a marca
            if tl.startswith(prefixo.lower()):
                nome = f"{model}{cor_sp}".upper()
            else:
                nome = f"{prefixo} {model}{cor_sp}".upper()
            if nome in vistos:
                continue  # mesma modelo+cor ja adicionada (outra variante/material)
            vistos.add(nome)
            imgs = [i["src"].split("?")[0] for i in prod.get("images", [])][:5]
            ctx = {"model": model, "brand": brand, "cor": cor or "", "cor_sp": cor_sp}
            # descricao: ou a traducao da descricao OFICIAL da marca, ou o template
            if entry.get("descricao_from_source"):
                bruto = _html_para_texto(prod.get("body_html") or "")
                txt = traduzir(bruto) if (bruto and entry.get("traduzir", True)) else bruto
                descricao = txt.replace("\n", "<br>") if txt else desc_tpl.format(**ctx)
            else:
                descricao = desc_tpl.format(**ctx)
            out.append({
                "name": nome, "brand": brand, "marca_header": header, "secao": secao,
                "images": imgs, "color": cor or "\u00danico",
                "price_brl": round(eur * mult + add, 2),
                "descricao": descricao,
                "caimento_material": [b.format(**ctx) for b in caimento],
                "seo_title": seo_t_tpl.format(**ctx)[:70],
                "seo_description": seo_d_tpl.format(**ctx)[:160],
                "tags": tags + ([cor.lower()] if cor else []),
                "sizes": sizes_out, "category_id": entry["category_id"],
                "weight": entry.get("weight", 0.6), "stock": entry.get("stock", 1),
                "add_category_if_exists": entry.get("add_category_if_exists", False),
                "imagens_por_url": entry.get("imagens_por_url", False),
            })
        print(f"    (disponibilidade: {_disp} em estoque de {_tot} no feed | "
              f"ignore_sold_out={ignora_esg})")
        return out

    # ---- modo legado (Rhode) ----
    pmap = {k.lower(): v for k, v in entry.get("price_eur_map", {}).items()}
    default = entry.get("price_eur_default", 30)
    out = []
    for prod in _shopify_products_robusto(url):
        if True:
            title = (prod.get("title") or "").strip()
            tl = title.lower()
            if not title or any(k in tl for k in skip):
                continue
            if incl and not any(k in tl for k in incl):
                continue
            eur = default
            for k, v in pmap.items():
                if k in tl:
                    eur = v
                    break
            vars_ = prod.get("variants", [])
            if vars_ and not any(v.get("available", True) for v in vars_):
                continue  # esgotado -> pula
            imgs = [i["src"].split("?")[0] for i in prod.get("images", [])][:5]
            out.append({
                "name": (title.upper() if tl.startswith("rhode") else f"RHODE {title}".upper()), "brand": entry.get("brand", "Rhode"),
                "marca_header": entry.get("marca_header", "RHODE"), "secao": entry.get("secao", "BENEF\u00cdCIOS"),
                "images": imgs, "color": "\u00danico", "price_brl": round(eur * 6.5 + 320, 2),
                "descricao": f"{title} da Rhode, de Hailey Bieber. Produto de l\u00e1bios e bochechas da marca.",
                "caimento_material": ["Produto 100% original Rhode (Hailey Bieber)", "Pronta entrega / importado"],
                "seo_title": f"Rhode {title.title()}"[:70],
                "seo_description": f"{title.title()} da Rhode (Hailey Bieber). Original, importado, em at\u00e9 12x no cart\u00e3o.",
                "tags": entry.get("tags", ["rhode"]), "sizes": ["\u00danico"],
                "category_id": entry["category_id"], "weight": entry.get("weight", 0.1),
                "stock": entry.get("stock", 2),
                "add_category_if_exists": entry.get("add_category_if_exists", False),
            })
    return out


def shopify_disponivel(url_produto):
    """True se o produto Shopify tem alguma variante disponivel (nao esgotado).
    Em caso de duvida/erro, retorna True (nao bloqueia)."""
    base = url_produto.split("?")[0].rstrip("/")
    try:
        r = SESSION.get(base + ".json", timeout=30)
        if r.status_code == 200:
            vars_ = r.json().get("product", {}).get("variants", [])
            if vars_:
                return any(v.get("available", True) for v in vars_)
    except Exception:  # noqa
        pass
    return True


def expandir_tnf(entry):
    """Le a listagem da The North Face (es-es) pagina a pagina e devolve os produtos
    prontos (nome EN do modelo, 4 fotos HERO/ALT1/ALT2/ALT3, preco pela formula).
    Pula os codigos que existem no BR (br_excluir) e os repetidos."""
    import re
    base = entry["tnf_listing"].split("?")[0]
    paginas = int(entry.get("paginas", 4))
    excluir = set(c.upper()[:8] for c in entry.get("br_excluir", []))
    mult = entry.get("eur_mult", 6.5); add = entry.get("eur_add", 1500)
    prefixo = entry.get("nome_prefixo", "THE NORTH FACE")
    IMG = ("https://assets.thenorthface.eu/images/t_img/f_auto,q_auto:best,w_1200/"
           "v{ver}/{code}-{suf}/{file}-{suf}.jpg")
    hero_re = re.compile(r"/v(\d+)/(NF0A[A-Z0-9]+)-HERO/([A-Za-z0-9\-]+?)-HERO\.jpg")
    alt_re = re.compile(r"/v(\d+)/(NF0A[A-Z0-9]+)-(ALT\d+)/[A-Za-z0-9\-]+?-\3\.jpg")
    price_re = re.compile(r"([\d.]{2,6}),(\d\d)\s*(?:&nbsp;|\s)*(?:\u20ac|&euro;|EUR)")
    out = []; vistos = set()
    for page in range(1, paginas + 1):
        try:
            html = SESSION.get(f"{base}?page={page}", timeout=40).text
        except Exception:  # noqa
            continue
        heros = list(hero_re.finditer(html))
        print(f"    (pagina {page}: {len(heros)} fotos HERO, {len(list(price_re.finditer(html)))} precos; html {len(html)} chars)")
        alts = {}
        for m in alt_re.finditer(html):
            alts.setdefault(m.group(2), {})[m.group(3)] = m.group(1)
        prices = list(price_re.finditer(html))
        for i, h in enumerate(heros):
            code = h.group(2)               # ex NF0A8H3DBI4 (estilo+cor)
            file = h.group(3)
            estilo = code[:8]
            if code in vistos or estilo in excluir:
                continue
            vistos.add(code)
            # preco: primeiro "Precio:" depois deste hero e antes do proximo
            fim = heros[i + 1].start() if i + 1 < len(heros) else len(html)
            eur = None
            for p in prices:
                if h.end() <= p.start() < fim:
                    eur = float(p.group(1).replace(".", "")); break
            if not eur:
                continue
            vh = h.group(1)
            v1 = alts.get(code, {}).get("ALT1", vh)
            v2 = alts.get(code, {}).get("ALT2", v1)
            v3 = str(int(v2) + 1)
            imgs = [IMG.format(ver=vh, code=code, suf="HERO", file=file),
                    IMG.format(ver=v1, code=code, suf="ALT1", file=file),
                    IMG.format(ver=v2, code=code, suf="ALT2", file=file),
                    IMG.format(ver=v3, code=code, suf="ALT3", file=file)]
            sem_mens = file[5:] if file.startswith("Mens-") else file
            modelo = sem_mens.split("-TNF-")[0].replace("-", " ").strip()
            if any(k in modelo.lower() for k in entry.get("skip_palavras", [])):
                continue
            nome = f"{prefixo} {modelo}".upper()
            out.append({
                "name": nome, "brand": "The North Face", "marca_header": "THE NORTH FACE",
                "secao": "DETALHES", "images": imgs, "color": estilo,
                "price_brl": round(eur * mult + add, 2),
                "descricao": f"{modelo} da The North Face \u2014 pe\u00e7a importada da Europa, n\u00e3o dispon\u00edvel no site BR.",
                "caimento_material": [f"Modelo: {modelo}", f"C\u00f3digo global TNF: {estilo}",
                                      "Importado da Europa \u2014 n\u00e3o dispon\u00edvel no site BR",
                                      "Produto 100% original The North Face"],
                "seo_title": f"The North Face {modelo}"[:70],
                "seo_description": f"The North Face {modelo}. Importado da Europa, original, em at\u00e9 12x no cart\u00e3o.",
                "tags": ["the north face", "tnf", "masculino", estilo.lower()],
                "sizes": entry.get("sizes", ["P", "M", "G", "GG", "GGG"]),
                "category_id": entry["category_id"], "weight": 1.2, "stock": 1,
            })
    return out


def shopify_images(url_produto, max_fotos=5):
    """Pega as fotos de um produto Shopify (ex: Rhode) pelo endpoint .json."""
    base = url_produto.split("?")[0].rstrip("/")
    try:
        r = SESSION.get(base + ".json", timeout=30)
        if r.status_code == 200:
            imgs = r.json().get("product", {}).get("images", [])
            return [i["src"].split("?")[0] for i in imgs][:max_fotos]
    except Exception:  # noqa
        pass
    return []


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

    def add_category(self, pid, cat_id):
        r = self._req("GET", f"/products/{pid}", params={"fields": "id,categories"}, timeout=30)
        if r.status_code >= 400:
            return f"erro-get {r.status_code}"
        cats = []
        for c in r.json().get("categories", []):
            cats.append(c["id"] if isinstance(c, dict) else c)
        if cat_id in cats:
            return "ja-tinha"
        cats.append(cat_id)
        r = self._req("PUT", f"/products/{pid}", json={"categories": cats}, timeout=60)
        return "ok" if r.status_code < 400 else f"erro {r.status_code}"

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
    header = p.get("marca_header", "ACNE STUDIOS")
    secao = p.get("secao", "CAIMENTO &amp; MATERIAL")
    return (f"<p><strong>{header}</strong></p><p><strong>{nome}</strong></p><p><br></p>"
            f"<h2><strong>DESCRI\u00c7\u00c3O</strong></h2><p>{desc}</p><p><br></p>"
            f"<h2><strong>{secao}</strong></h2><p>{bullets}</p>" + SELO)


def _montar_imagens(imgs, por_url=False):
    """Monta as imagens do produto. por_url=True envia o LINK (a Nuvemshop baixa do
    CDN) -> pedido pequeno, evita 413. por_url=False embute o arquivo em base64
    (necessario so quando a URL e bloqueada por filtro, ex: Acne '/ad/')."""
    out = []
    for u in imgs:
        if por_url:
            out.append({"src": u, "position": len(out) + 1})
            continue
        b64, nome = baixar_imagem(u)
        if b64:
            out.append({"attachment": b64, "filename": nome, "position": len(out) + 1})
        # se nao baixou (ex: 404), pula essa foto e segue com as demais
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
        "brand": p.get("brand", "Acne Studios"),
        "published": True,
        "requires_shipping": True,
        "attributes": [{"pt": "Cor"}, {"pt": "Tamanho"}],
        "categories": [p["category_id"]],
        "seo_title": {"pt": p["seo_title"]},
        "seo_description": {"pt": p["seo_description"]},
        "tags": ", ".join(p.get("tags", [])),
        "variants": variants,
        "images": _montar_imagens(imgs, p.get("imagens_por_url", False)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arquivo", required=True, help="JSON de produtos, ex.: acne_saias.json")
    ap.add_argument("--live", action="store_true", help="criar de verdade (sem isso, so testa)")
    ap.add_argument("--substituir", action="store_true", help="se o produto ja existe, apaga e cria de novo (corrige ordem de fotos/preco)")
    args = ap.parse_args()

    produtos = json.load(open(args.arquivo, encoding="utf-8-sig"))
    # expande entradas de colecao Shopify (todos os produtos/cores) em itens individuais
    expandido = []
    for it in produtos:
        if it.get("tnf_listing"):
            ach = expandir_tnf(it)
            print(f"  (TNF: {len(ach)} jaqueta(s) exclusivas encontradas)")
            expandido.extend(ach)
        elif it.get("shopify_collection"):
            achados = expandir_colecao(it)
            print(f"  (colecao {it['shopify_collection'].rsplit('/',2)[-2]}: {len(achados)} produto(s) encontrados)")
            expandido.extend(achados)
        else:
            expandido.append(it)
    produtos = expandido
    token = os.environ.get("NUVEMSHOP_ACCESS_TOKEN", "")
    if args.live and not token:
        sys.exit("Sem NUVEMSHOP_ACCESS_TOKEN: nao da para criar. Configure o segredo no repositorio.")
    ns = Nuvemshop(STORE_ID, token, CONTATO) if token else None

    print(f"== Acne importer | {len(produtos)} produto(s) | modo: {'LIVE (cria na loja)' if args.live else 'TESTE (nao cria)'} ==\n")
    criados = pulados = falhas = 0
    for i, p in enumerate(produtos, 1):
        nome = p["name"]
        mpn = p.get("mpn", "")
        _tam = ",".join(str(t) for t in p.get("sizes", []))
        print(f"[{i}/{len(produtos)}] {nome}  ->  R$ {p['price_brl']:.0f}  ({p['color']})  [tam: {_tam}]")
        if p.get("shopify_url") and not p.get("images"):
            if not shopify_disponivel(p["shopify_url"]):
                print("    esgotado na Rhode -> pulando"); pulados += 1; continue
        if p.get("images"):
            imgs = p["images"]
        elif p.get("shopify_url"):
            imgs = shopify_images(p["shopify_url"])
        else:
            imgs = extrair_fotos(p["acne_url"], mpn)
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
                if p.get("add_category_if_exists"):
                    r = "nenhum"
                    for pid in existentes:
                        r = ns.add_category(pid, p["category_id"])
                    print(f"    ja existe -> categoria {p['category_id']} adicionada ({r})"); pulados += 1; continue
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
