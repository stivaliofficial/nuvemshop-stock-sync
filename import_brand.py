#!/usr/bin/env python3
"""
Importador de marca -> Nuvemshop (STIVALI).

Fluxo:
  1. Lista os produtos da aba configurada no site oficial da marca.
  2. Para cada produto lê: preço em EUR, fotos, variações com estoque real.
  3. Aplica a fórmula de preço e converte os tamanhos para o padrão brasileiro.
  4. Monta a descrição no formato da loja (textos em português vêm de translations_<marca>.json).
  5. Modo teste (padrão): só gera preview_<marca>.json e mostra o resumo no log.
     Modo --live: cria o produto na Nuvemshop pela API, já com as fotos.

Uso:
  python import_brand.py --brand skims --limit 3            # teste
  python import_brand.py --brand skims --handles a,b        # teste em produtos específicos
  python import_brand.py --brand skims --live               # cria de verdade
"""

import argparse
import html
import json
import math
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

FOOTER = (
    "<hr>"
    "<p><strong>✓ Produto 100% original, verificado pela nossa curadoria antes do envio.<br>"
    "✓ Compra 100% segura · Em até 12x no cartão · Envio para todo o Brasil</strong></p>"
)

# ---------------------------------------------------------------- tamanhos
# Roupa: mesmo padrão de letras das outras marcas de roupa da loja (PP, P, M, G, GG, 2GG...)
CLOTHING = {
    "XXS": "XPP", "XS": "PP", "S": "P", "M": "M", "L": "G", "XL": "GG",
    "2X": "2GG", "3X": "3GG", "4X": "4GG", "5X": "5GG",
    "2XL": "2GG", "3XL": "3GG", "4XL": "4GG", "5XL": "5GG",
    "ONE SIZE": "ÚNICO", "OS": "ÚNICO",
}
# Sutiã: faixa BR = faixa EUA + 8 ; taça: DD americana = E ; DDD/F = F
BRA_CUP = {"A": "A", "B": "B", "C": "C", "D": "D", "DD": "E", "E": "E",
           "DDD": "F", "F": "F", "G": "G"}


class Blocked(Exception):
    pass


class SizeError(Exception):
    pass


def convert_clothing(value):
    """'S/M' -> 'P/M', 'XL' -> 'GG'. Levanta SizeError se não souber converter."""
    parts = [p.strip().upper() for p in re.split(r"\s*/\s*", value.strip())]
    out = []
    for p in parts:
        if p not in CLOTHING:
            raise SizeError(f"tamanho de roupa desconhecido: {value!r}")
        out.append(CLOTHING[p])
    return "/".join(out)


def convert_bra(band, cup):
    """band '34', cup 'DD' -> ('42E', info dict)"""
    try:
        band_us = int(str(band).strip())
    except ValueError:
        raise SizeError(f"faixa de sutiã inválida: {band!r}")
    cup_us = str(cup).strip().upper()
    if cup_us not in BRA_CUP:
        raise SizeError(f"taça de sutiã desconhecida: {cup!r}")
    band_br = band_us + 8
    band_eu = int(65 + (band_us - 30) * 2.5)
    cup_br = BRA_CUP[cup_us]
    return f"{band_br}{cup_br}", {"us": f"{band_us}{cup_us}", "band_us": band_us,
                                  "band_br": band_br, "band_eu": band_eu,
                                  "cup_us": cup_us, "cup_br": cup_br}


# ---------------------------------------------------------------- preço
def final_price(eur, pricing):
    """EUR x multiplicador + valor fixo, sem centavos (corta)."""
    return int(math.floor(eur * pricing["multiplier"] + pricing["fixed_brl"]))


# ---------------------------------------------------------------- rede
def get(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"{r.status_code} em {url}")
    r.raise_for_status()
    return r


HANDLE_RE = re.compile(
    r'href="(?:https?://[^"/]+)?(?:/[a-z]{2}-[a-z]{2})?/products/([a-z0-9][a-z0-9_%\-]*)(?=[?"#])', re.I)
NEXT_RE = re.compile(r'href="([^"]*direction=next[^"]*cursor=[^"]*)"', re.I)


def main_content(page):
    """Corta o menu do topo (que tem links de produtos promocionais)."""
    idx = page.find('id="main-content"')
    return page[idx:] if idx > 0 else page


def list_handles(collection_url, max_pages, sleep=1.0):
    handles, url, pages = [], collection_url, 0
    while url and pages < max_pages:
        page = get(url).text
        body = main_content(page)
        for h in HANDLE_RE.findall(body):
            if h not in handles:
                handles.append(h)
        m = NEXT_RE.search(body)
        url = urljoin(url, html.unescape(m.group(1))) if m else None
        pages += 1
        time.sleep(sleep)
    return handles


def parse_ld(page):
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("Product", "ProductGroup"):
                return it
    return None


def extract_price(ld, page):
    """Retorna (valor, moeda) como aparece na loja em EUR."""
    offers = []
    if ld:
        o = ld.get("offers")
        if o:
            offers += o if isinstance(o, list) else [o]
        for v in ld.get("hasVariant", []) or []:
            o = v.get("offers")
            if o:
                offers += o if isinstance(o, list) else [o]
    for o in offers:
        try:
            return float(o["price"]), o.get("priceCurrency")
        except (KeyError, TypeError, ValueError):
            continue
    m = re.search(r'property="product:price:amount"\s+content="([\d.,]+)"', page)
    c = re.search(r'property="product:price:currency"\s+content="([A-Z]{3})"', page)
    if m:
        return float(m.group(1).replace(",", ".")), (c.group(1) if c else None)
    m = re.search(r"€\s?(\d+(?:[.,]\d+)?)", main_content(page))
    if m:
        return float(m.group(1).replace(",", ".")), "EUR"
    return None, None


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


NUM_TOKEN_RE = re.compile(r"^\d{3,5}[A-Z]?$")
THUMB_RE = re.compile(r"_(?:x|\d+x)\d*\.(?:jpe?g|png|webp)$", re.I)
PAGE_IMG_RE = re.compile(r"(?:https?:)?//[A-Za-z0-9.\-]*(?:skims\.imgix\.net|cdn\.shopify\.com)/[^\"'\s\\)<>]+?\.(?:jpe?g|png|webp)", re.I)


def code_pair(text):
    """'BD-THG-9551W-ONX-LD-SKIMS_0035-SD' -> ('9551W', 'ONX'): número do estilo + código da cor."""
    stem = text.strip().split("/")[-1].split("?")[0].rsplit(".", 1)[0].split("_")[0]
    toks = [t for t in stem.upper().split("-") if t]
    for i, t in enumerate(toks[:-1]):
        if NUM_TOKEN_RE.match(t):
            return (t, toks[i + 1])
    return None


def is_thumb(url):
    return bool(THUMB_RE.search(url.split("?")[0]))


def images_by_code(page, skus, known_images):
    """Fotos da página cujo nome de arquivo traz o número do estilo e a cor deste produto."""
    pares = set()
    for t in list(skus) + list(known_images):
        if t:
            p = code_pair(t)
            if p:
                pares.add(p)
    if not pares:
        return []
    out = []
    for m in PAGE_IMG_RE.finditer(page):
        u = m.group(0)
        fname = u.split("?")[0].rsplit("/", 1)[-1].upper()
        if is_thumb(u):
            continue
        if any(a in fname and b in fname for a, b in pares):
            out.append(u)
    return out


def detail_snippets(page, limit=6):
    """Trechos da página oficial com ficha de tecido/cuidado/caimento (texto em inglês, para tradução)."""
    def clean(t):
        t = t.replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">").replace('\\"', '"')
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()
    page = main_content(page)
    keys = ("Fit & Fabric", "Fit &amp; Fabric", "Fit \\u0026 Fabric", "Machine wash", "Hand wash",
            "Polyamide", "Elastane", "Cotton", "Modal", "Nylon", "Polyester", "Imported")
    found, spans = [], []
    for k in keys:
        for m in re.finditer(re.escape(k), page):
            st, en = max(0, m.start() - 350), min(len(page), m.end() + 650)
            if any(st < e and en > s0 for s0, e in spans):
                continue
            spans.append((st, en))
            txt = clean(page[st:en])
            if txt and txt not in found:
                found.append(txt[:1100])
            break
        if len(found) >= limit:
            break
    return found


def get_product_js(base_url, handle, attempts):
    """Tenta o endpoint padrão da Shopify (.js). Guarda o que aconteceu em attempts."""
    for url in (f"{base_url}/products/{handle}.js",
                f"{base_url.rsplit('/', 1)[0]}/products/{handle}.js"):
        try:
            r = SESSION.get(url, timeout=30)
            attempts.append({"url": url, "status": r.status_code,
                             "content_type": r.headers.get("content-type", ""), "head": r.text[:120]})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("variants"):
                    return data
        except Exception as e:  # noqa
            attempts.append({"url": url, "error": str(e)[:150]})
    return None


def extract_json_blobs(page):
    """Acha blocos JSON dentro dos <script> da página (inclusive window.X = {...})."""
    blobs, dec = [], json.JSONDecoder()
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", page, re.S | re.I):
        attrs, body = m.group(1), m.group(2).strip()
        if not body or len(body) > 4_000_000:
            continue
        if body[0] in "{[":
            try:
                blobs.append(json.loads(body))
                continue
            except ValueError:
                pass
        tries = 0
        for mm in re.finditer(r"=\s*(\{)", body):
            tries += 1
            if tries > 4:
                break
            try:
                obj, _ = dec.raw_decode(body[mm.start(1):])
                blobs.append(obj)
            except ValueError:
                continue
    return blobs


def _nodes(x):
    if isinstance(x, dict):
        if isinstance(x.get("nodes"), list):
            return x["nodes"]
        if isinstance(x.get("edges"), list):
            return [e.get("node") for e in x["edges"] if isinstance(e, dict)]
    return x if isinstance(x, list) else []


def find_products(obj, out, depth=0):
    if depth > 14:
        return
    if isinstance(obj, dict):
        vs = _nodes(obj.get("variants")) if "variants" in obj else []
        if vs and isinstance(vs[0], dict) and ("title" in obj or "handle" in obj):
            out.append(obj)
        for v in obj.values():
            find_products(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            find_products(v, out, depth + 1)


IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)|imgix|cdn\.shopify", re.I)


def collect_urls(x, out, depth=0):
    if depth > 6:
        return
    if isinstance(x, str):
        if x.startswith(("http", "//")) and IMG_RE.search(x):
            out.append(x)
    elif isinstance(x, dict):
        for k in ("url", "src", "originalSrc", "transformedSrc"):
            if k in x:
                collect_urls(x[k], out, depth + 1)
        for k in ("nodes", "edges", "node", "image", "preview"):
            if k in x:
                collect_urls(x[k], out, depth + 1)
    elif isinstance(x, list):
        for v in x:
            collect_urls(v, out, depth + 1)


def shape_product(p, handle):
    """Converte o produto achado na página para o mesmo formato do .js da Shopify."""
    raw_opts = p.get("options") or []
    names = []
    for o in raw_opts:
        names.append(o.get("name") if isinstance(o, dict) else str(o))
    variants = []
    for v in _nodes(p.get("variants")):
        if not isinstance(v, dict):
            continue
        vals = [None, None, None]
        sel = v.get("selectedOptions")
        if sel:
            if not names:
                names = [x.get("name") for x in sel]
            for x in sel:
                if x.get("name") in names[:3]:
                    vals[names.index(x["name"])] = x.get("value")
        else:
            vals = [v.get("option1"), v.get("option2"), v.get("option3")]
        av = v.get("available")
        if av is None:
            av = v.get("availableForSale")
        if av is None and v.get("quantityAvailable") is not None:
            av = (v.get("quantityAvailable") or 0) > 0
        variants.append({"option1": vals[0], "option2": vals[1], "option3": vals[2],
                         "available": av, "sku": v.get("sku")})
    imgs = []
    for k in ("images", "media", "featuredImage", "featured_image"):
        if k in p:
            collect_urls(p[k], imgs)
    return {"title": p.get("title", ""), "handle": p.get("handle", handle),
            "options": [{"name": n} for n in names], "variants": variants, "images": imgs}


BRA_NAME_RE = re.compile(r"(?<!\d)(\d{2})\s*[-/ ]?\s*(DDD|DD|[A-G])\s*$", re.I)
BRA_SKU_RE = re.compile(r"-(\d{2})(DDD|DD|[A-G])$", re.I)
CLOTH_RE = re.compile(
    r"(?:^|[\s/\-])(XXS/XS|S/M|L/XL|2X/3X|4X/5X|XXS|XS|S|M|L|XL|2X|3X|4X|5X)\s*$", re.I)


def _txt(x):
    if isinstance(x, dict):
        return str(x.get("name") or x.get("value") or "")
    return "" if x is None else str(x)


def _availability(v):
    offers = v.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    a = _txt((offers or {}).get("availability")).lower() if isinstance(offers, dict) else ""
    if any(k in a for k in ("instock", "limitedavailability", "onlineonly")):
        return True
    if any(k in a for k in ("outofstock", "soldout", "discontinued")):
        return False
    return None


def _variant_size(v):
    """Devolve ('bra', faixa, taça) | ('size', texto) | None, olhando campos explícitos, nome e SKU."""
    band = cup = None
    texts = [_txt(v.get("size"))]
    for ap in v.get("additionalProperty") or []:
        if not isinstance(ap, dict):
            continue
        n, val = _txt(ap.get("name")).lower(), _txt(ap.get("value"))
        if "band" in n:
            band = val
        elif "cup" in n:
            cup = val
        elif "size" in n:
            texts.append(val)
    if band and cup:
        return ("bra", band, cup)
    name, sku = _txt(v.get("name")).strip(), _txt(v.get("sku")).strip()
    for t in texts + [name]:
        m = BRA_NAME_RE.search(t.strip())
        if m:
            return ("bra", m.group(1), m.group(2))
        m = CLOTH_RE.search(t.strip())
        if m:
            return ("size", m.group(1))
    m = BRA_SKU_RE.search(sku)
    if m:
        return ("bra", m.group(1), m.group(2))
    m = CLOTH_RE.search(sku)
    if m:
        return ("size", m.group(1))
    return None


def shape_from_ld(ld, page, handle):
    """Monta o produto a partir do bloco schema.org ProductGroup da página."""
    if not ld:
        return None
    hv = ld.get("hasVariant")
    if not isinstance(hv, list) or not hv:
        return None
    parsed = [(v, _variant_size(v)) for v in hv if isinstance(v, dict)]
    kinds = {p[0] for _, p in parsed if p}
    if not kinds:
        return None
    is_bra = "bra" in kinds
    options = [{"name": "Band Size"}, {"name": "Cup Size"}] if is_bra else [{"name": "Size"}]
    variants = []
    for v, p in parsed:
        if not p:
            continue
        if is_bra:
            if p[0] != "bra":
                continue
            o1, o2 = p[1], p[2]
        else:
            o1, o2 = p[1], None
        variants.append({"option1": o1, "option2": o2, "option3": None,
                         "available": _availability(v), "sku": _txt(v.get("sku")) or None})
    imgs = []
    for src in [ld.get("image")] + [v.get("image") for v in hv if isinstance(v, dict)]:
        collect_urls(src, imgs)
    title = _txt(ld.get("name"))
    tag = re.search(r"<title>(.*?)</title>", page, re.S)
    if "|" not in title and tag:
        title = re.sub(r"\s*\|\s*SKIMS\s*$", "", html.unescape(tag.group(1)).strip(), flags=re.I)
    return {"title": title, "handle": handle, "options": options, "variants": variants, "images": imgs}


def product_from_page(page, handle):
    js = shape_from_ld(parse_ld(page), page, handle)
    if js and js["variants"]:
        return js, -1
    found = []
    for b in extract_json_blobs(page):
        find_products(b, found)
    if not found:
        return None, 0
    pick = next((p for p in found if p.get("handle") == handle), found[0])
    return shape_product(pick, handle), len(found)


def option_kinds(js):
    kinds = []
    for o in js.get("options", []):
        name = (o.get("name") if isinstance(o, dict) else str(o)).lower()
        if "band" in name:
            kinds.append("band")
        elif "cup" in name:
            kinds.append("cup")
        elif "colo" in name:
            kinds.append("color")
        else:
            kinds.append("size")
    return kinds


def norm_url(u):
    if not u:
        return None
    u = "https:" + u if u.startswith("//") else u
    return u


# ---------------------------------------------------------------- produto
def build_variants(js, defaults):
    kinds = option_kinds(js)
    variants, issues, bra_info, size_pairs = [], [], [], []
    seen = set()
    for v in js.get("variants", []):
        if v.get("available") is None:
            issues.append("estoque por tamanho desconhecido")
            break
        if not v.get("available"):
            continue
        raw = {}
        for kind, key in zip(kinds, ("option1", "option2", "option3")):
            raw[kind] = v.get(key)
        try:
            if "band" in raw and "cup" in raw:
                label, info = convert_bra(raw["band"], raw["cup"])
                bra_info.append(info)
                orig = info["us"]
            elif "size" in raw:
                label = convert_clothing(raw["size"])
                orig = raw["size"].upper()
                size_pairs.append((orig, label))
            else:
                raise SizeError("produto sem opção de tamanho reconhecível")
        except SizeError as e:
            issues.append(str(e))
            continue
        if label in seen:
            issues.append(f"tamanho duplicado: {label}")
            continue
        seen.add(label)
        variants.append({"label": label, "original": orig,
                         "sku": v.get("sku") or f"SKIMS-{js.get('handle')}-{label}",
                         "stock": defaults["stock"]})
    return variants, issues, bra_info, size_pairs


def size_note_lines(bra_info, size_pairs):
    if bra_info:
        bands = sorted({(i["band_us"], i["band_br"], i["band_eu"]) for i in bra_info})
        lines = ["Numeração já convertida para o padrão brasileiro: o número é o contorno "
                 "abaixo do busto e a letra é o tamanho da taça"]
        lines.append("Equivalência da faixa: " + " · ".join(
            f"BR {br} = EUA {us} = Europa {eu}" for us, br, eu in bands))
        cups = sorted({(i["cup_us"], i["cup_br"]) for i in bra_info if i["cup_us"] != i["cup_br"]})
        if cups:
            lines.append("Equivalência da taça: " + " · ".join(f"{u} americana = {b} brasileira" for u, b in cups))
        return lines
    if size_pairs:
        order = {k: i for i, k in enumerate(CLOTHING)}
        pairs = sorted(set(size_pairs), key=lambda p: order.get(p[0].split("/")[0], 99))
        return ["Numeração já convertida para o padrão brasileiro",
                "Equivalência: " + " · ".join(f"{o} = {b}" for o, b in pairs)]
    return []


GAP = "<p><br></p>"  # linha em branco entre os blocos


def build_description(brand, name_full, tr):
    """SKIMS / NOME / DESCRIÇÃO (texto em português) / CAIMENTO & MATERIAL (só o Fit & Fabric) / selos,
    com uma linha em branco entre os blocos."""
    esc = html.escape
    paras = tr["descricao"] if isinstance(tr["descricao"], list) else [tr["descricao"]]
    blocks = [
        f"<p><strong>{esc(brand)}</strong></p>",
        f"<p><strong>{esc(name_full)}</strong></p>",
        "<p><strong>DESCRIÇÃO</strong></p>",
    ]
    blocks += [f"<p>{esc(p)}</p>" for p in paras]
    blocks += [
        "<p><strong>CAIMENTO &amp; MATERIAL</strong></p>",
        "<p>" + "<br>".join("• " + esc(x) for x in tr["caimento_material"]) + "</p>",
        FOOTER,
    ]
    return GAP.join(blocks)


def translation_ok(tr):
    need = ("descricao", "caimento_material", "seo_title", "seo_description", "tags")
    return bool(tr) and all(tr.get(k) for k in need)


def process(handle, bcfg, translations):
    out = {"handle": handle, "issues": [], "warnings": []}
    base = bcfg["base_url"]
    try:
        page = get(f"{base}/products/{handle}").text
    except Blocked as e:
        out["issues"].append(f"BLOQUEIO: {e}")
        out["blocked"] = True
        return out
    except Exception as e:  # noqa
        out["issues"].append(f"erro ao abrir a página: {e}")
        return out

    ld = parse_ld(page)
    eur, cur = extract_price(ld, page)
    out["eur"], out["currency"] = eur, cur
    if eur is None:
        out["issues"].append("preço não encontrado")
    elif cur and cur != bcfg["currency"]:
        out["issues"].append(f"preço veio em {cur}, não em {bcfg['currency']}")
    else:
        out["final_brl"] = final_price(eur, bcfg["pricing"])

    attempts = []
    js = get_product_js(base, handle, attempts)
    source = ".js"
    n_found = 0
    if not js:
        js, n_found = product_from_page(page, handle)
        source = "json-ld" if n_found == -1 else "página"
    scripts = [{"attrs": a.strip()[:80], "len": len(b), "head": b.strip()[:100]}
               for a, b in re.findall(r"<script([^>]*)>(.*?)</script>", page, re.S | re.I)][:25]
    out["debug"] = {"fonte_variacoes": source if js else None, "tentativas_js": attempts,
                    "produtos_com_variantes_na_pagina": n_found, "tamanho_html": len(page),
                    "scripts": scripts, "urls_imgix_na_pagina": len(re.findall(r"skims\.imgix\.net", page)),
                    "titulo_pagina": (re.search(r"<title>(.*?)</title>", page, re.S) or [None, ""])[1][:100]}
    ld0 = parse_ld(page) or {}
    hv0 = ld0.get("hasVariant") if isinstance(ld0.get("hasVariant"), list) else []
    out["debug"]["json_ld"] = {"tipo": ld0.get("@type"), "chaves": list(ld0.keys())[:30],
                               "n_variantes": len(hv0), "amostra_variantes": hv0[:3]}
    if not js or not js.get("variants"):
        out["debug"]["json_ld_bruto"] = json.dumps(ld0, ensure_ascii=False)[:20000]
        out["issues"].append("variações: não achei pelo .js nem dentro da página")
        return out
    out["fonte_variacoes"] = source

    title = js.get("title", "")
    if "|" in title:
        name, color = [p.strip() for p in title.rsplit("|", 1)]
    else:
        name, color = title.strip(), ""
    name_caps = name.upper()
    if not name_caps.startswith(bcfg["display_name"].upper()):
        name_caps = f"{bcfg['display_name'].upper()} {name_caps}"
    color = color.upper()
    if not color:
        out["warnings"].append("cor não identificada no título")
    out["name"] = f"{name_caps} {color}".strip()
    out["color"] = color

    variants, issues, bra_info, size_pairs = build_variants(js, bcfg["defaults"])
    out["issues"] += issues
    out["variants"] = variants
    if not variants:
        out["issues"].append("nenhum tamanho disponível/convertido")
    out["size_lines"] = size_note_lines(bra_info, size_pairs)

    imgs, seen_img = [], set()
    ld_imgs = (ld or {}).get("image") or []
    ld_imgs = [ld_imgs] if isinstance(ld_imgs, (str, dict)) else ld_imgs
    ld_urls = [norm_url(i.get("url") if isinstance(i, dict) else i) for i in ld_imgs]
    og = re.findall(r'property="og:image"\s+content="([^"]+)"', page)
    for u in [norm_url(x) for x in js.get("images", [])] + ld_urls + [norm_url(x) for x in og]:
        if not u:
            continue
        key = u.split("?")[0].rsplit("/", 1)[-1].lower()
        if key in seen_img or is_thumb(u):
            continue
        seen_img.add(key)
        imgs.append(u)
    known = [u for u in imgs]
    extra = images_by_code(page, [v.get("sku") for v in js.get("variants", [])], known)
    n_before = len(imgs)
    for u in extra:
        u = norm_url(u)
        key = u.split("?")[0].rsplit("/", 1)[-1].lower()
        if key not in seen_img and not is_thumb(u):
            seen_img.add(key)
            imgs.append(u)
    cap = bcfg.get("max_images", 0)
    out["images"] = imgs[:cap] if cap else imgs
    out["image_sources"] = {"variacoes_js": len(js.get("images", [])), "json_ld": len([u for u in ld_urls if u]),
                            "por_codigo_da_pagina": len(imgs) - n_before}
    if not out["images"]:
        out["issues"].append("sem fotos")

    out["detalhes_en"] = detail_snippets(page)
    out["description_en"] = strip_tags((ld or {}).get("description", "") or js.get("description", ""))[:1500]
    out["general_title"] = name.lower()
    tr = translations.get(handle)
    out["has_translation"] = translation_ok(tr)
    if not out["has_translation"]:
        out["issues"].append("falta tradução em translations_<marca>.json")
    return out


# ---------------------------------------------------------------- Nuvemshop
class Nuvemshop:
    def __init__(self, store_id, token, contact):
        self.base = f"https://api.nuvemshop.com.br/v1/{store_id}"
        self.h = {"Authentication": f"bearer {token}", "Content-Type": "application/json",
                  "User-Agent": f"STIVALI Brand Importer ({contact})"}

    def categories(self):
        out, page = [], 1
        while True:
            r = requests.get(f"{self.base}/categories", headers=self.h,
                             params={"per_page": 200, "page": page}, timeout=30)
            if r.status_code == 404:
                break
            r.raise_for_status()
            data = r.json()
            out += data
            if len(data) < 200:
                break
            page += 1
        return out

    def exists(self, name):
        r = requests.get(f"{self.base}/products", headers=self.h,
                         params={"q": name, "per_page": 10, "fields": "id,name"}, timeout=30)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        for p in r.json():
            n = p.get("name", {})
            if any(str(v).strip().upper() == name.strip().upper() for v in n.values()):
                return True
        return False

    def create(self, payload):
        r = requests.post(f"{self.base}/products", headers=self.h, json=payload, timeout=300)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
        return r.json()


def cat_name(c):
    n = c.get("name", {})
    for v in n.values():
        if v:
            return str(v).strip().upper()
    return ""


def resolve_category(path, cats):
    parent = None
    found = None
    for step in path:
        found = None
        for c in cats:
            p = c.get("parent") or None
            if cat_name(c) == step.upper() and p == parent:
                found = c
                break
        if not found:
            return None
        parent = found["id"]
    return found["id"] if found else None


def build_payload(brand, p, tr, bcfg, cat_ids):
    d = bcfg["defaults"]
    desc = build_description(brand, p["name"], tr)
    variants = []
    for v in p["variants"]:
        variants.append({
            "price": f"{p['final_brl']}.00",
            "stock_management": True,
            "stock": v["stock"],
            "sku": v["sku"],
            "weight": d["weight_kg"], "width": d["width_cm"],
            "height": d["height_cm"], "depth": d["depth_cm"],
            "values": [{"pt": p["color"]}, {"pt": v["label"]}] if p["color"] else [{"pt": v["label"]}],
        })
    attrs = [{"pt": "Cor"}, {"pt": "Tamanho"}] if p["color"] else [{"pt": "Tamanho"}]
    return {
        "name": {"pt": p["name"]},
        "description": {"pt": desc},
        "handle": {"pt": p["handle"]},
        "published": True,
        "free_shipping": False,
        "requires_shipping": True,
        "brand": brand,
        "tags": tr["tags"],
        "seo_title": tr["seo_title"][:70],
        "seo_description": tr["seo_description"][:320],
        "categories": cat_ids,
        "attributes": attrs,
        "variants": variants,
        "images": [{"src": u} for u in p["images"]],
    }


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--handles", default="")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open("brand_config.json", encoding="utf-8"))
    bcfg = cfg["brands"][args.brand]
    brand = bcfg["display_name"]
    try:
        translations = json.load(open(f"translations_{args.brand}.json", encoding="utf-8"))
    except FileNotFoundError:
        translations = {}

    token = os.environ.get("NUVEMSHOP_ACCESS_TOKEN", "")
    ns = Nuvemshop(cfg["nuvemshop"]["store_id"], token, cfg["nuvemshop"]["contact"]) if token else None
    if args.live and not ns:
        sys.exit("Modo --live precisa do NUVEMSHOP_ACCESS_TOKEN.")

    print(f"== {brand} | modo: {'LIVE (cria na loja)' if args.live else 'TESTE (não cria nada)'} ==")
    if args.handles:
        handles = [h.strip() for h in args.handles.split(",") if h.strip()]
    else:
        handles = list_handles(bcfg["collection_url"], bcfg["max_pages"])
        print(f"{len(handles)} produtos encontrados na aba.")
    if args.limit:
        handles = handles[: args.limit]

    cats = []
    brand_cat = None
    if ns:
        cats = ns.categories()
        brand_cat = resolve_category(bcfg["category_path"], cats)
        if brand_cat:
            print(f"Categoria da marca encontrada: {' > '.join(bcfg['category_path'])}")
        else:
            print(f"ATENÇÃO: categoria {' > '.join(bcfg['category_path'])} não existe na loja.")
            if args.live:
                sys.exit("Crie a categoria no painel antes do modo --live.")
    else:
        print("(sem token: pulando conferência de categorias)")

    results, blocked_in_row = [], 0
    for h in handles:
        p = process(h, bcfg, translations)
        results.append(p)
        blocked_in_row = blocked_in_row + 1 if p.get("blocked") else 0
        status = "OK" if not p["issues"] else "PENDÊNCIA"
        print(f"- [{status}] {p.get('name', h)} | EUR {p.get('eur')} -> R$ {p.get('final_brl')} | "
              f"{len(p.get('variants', []))} tamanhos | {len(p.get('images', []))} fotos")
        for i in p["issues"]:
            print(f"    ! {i}")
        d = p.get("debug") or {}
        if d and any("variações" in i for i in p["issues"]) and sum(1 for r in results if r is not p and r.get("debug_shown")) < 2:
            p["debug_shown"] = True
            print(f"    [diagnóstico] .js: {[(a.get('status'), a.get('content_type', '')[:25]) for a in d.get('tentativas_js', [])]}"
                  f" | scripts na página: {len(d.get('scripts', []))} | produtos achados: {d.get('produtos_com_variantes_na_pagina')}"
                  f" | imagens imgix: {d.get('urls_imgix_na_pagina')}")
        for w in p["warnings"]:
            print(f"    ~ {w}")
        if blocked_in_row >= 3:
            print("O site bloqueou 3 pedidos seguidos. Parando para não insistir.")
            break
        time.sleep(1.0)

    created = skipped = 0
    if args.live:
        for p in results:
            if p["issues"]:
                skipped += 1
                continue
            tr = translations[p["handle"]]
            cat_ids = [brand_cat] if brand_cat else []
            for g in bcfg.get("general_categories", []):
                if re.search(g["title_regex"], p["general_title"], re.I):
                    gid = resolve_category(g["path"], cats)
                    if gid:
                        cat_ids.append(gid)
                    else:
                        print(f"  ~ categoria geral {' > '.join(g['path'])} não encontrada")
            try:
                if ns.exists(p["name"]):
                    print(f"  = já existe na loja: {p['name']}")
                    skipped += 1
                    continue
                res = ns.create(build_payload(brand, p, tr, bcfg, cat_ids))
                print(f"  + criado: {p['name']} (id {res.get('id')})")
                created += 1
            except Exception as e:  # noqa
                print(f"  ! falhou: {p['name']}: {e}")
                skipped += 1

    with open(f"preview_{args.brand}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    ok = sum(1 for p in results if not p["issues"])
    print(f"\nResumo: {len(results)} lidos | {ok} sem pendência | "
          f"{len(results) - ok} com pendência" + (f" | {created} criados | {skipped} pulados" if args.live else ""))
    print(f"Detalhes em preview_{args.brand}.json")


if __name__ == "__main__":
    main()
