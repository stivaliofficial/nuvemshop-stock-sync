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


def get_product_js(base_url, handle):
    for url in (f"{base_url}/products/{handle}.js",
                f"{base_url.rsplit('/', 1)[0]}/products/{handle}.js"):
        try:
            r = get(url)
            return r.json()
        except (ValueError, requests.HTTPError):
            continue
    raise ValueError("não consegui ler as variações (.js)")


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


def build_description(brand, name_full, tr, size_lines):
    esc = html.escape
    fit = list(size_lines) + list(tr.get("caimento", []))
    parts = [
        f"<p><strong>{esc(brand)}</strong></p>",
        f"<p><strong>{esc(name_full)}</strong></p>",
        "<p><strong>DESCRIÇÃO</strong></p>",
        f"<p>{esc(tr['descricao'])}</p>",
        "<p><strong>TAMANHO &amp; CAIMENTO</strong></p>",
        "<p>" + "<br>".join("• " + esc(x) for x in fit) + "</p>",
        "<p><strong>MATERIAL &amp; CUIDADO</strong></p>",
        "<p>" + "<br>".join("• " + esc(x) for x in tr["material"]) + "</p>",
        FOOTER,
    ]
    return "".join(parts)


def translation_ok(tr):
    need = ("descricao", "material", "seo_title", "seo_description", "tags")
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

    try:
        js = get_product_js(base, handle)
    except Exception as e:  # noqa
        out["issues"].append(f"variações: {e}")
        return out

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
    for u in [norm_url(x) for x in js.get("images", [])] + ld_urls:
        if not u:
            continue
        key = u.split("?")[0].rsplit("/", 1)[-1].lower()
        if key in seen_img:
            continue
        seen_img.add(key)
        imgs.append(u)
    cap = bcfg.get("max_images", 0)
    out["images"] = imgs[:cap] if cap else imgs
    out["image_sources"] = {"variacoes_js": len(js.get("images", [])), "json_ld": len([u for u in ld_urls if u])}
    if not out["images"]:
        out["issues"].append("sem fotos")

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
    desc = build_description(brand, p["name"], tr, p["size_lines"])
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
