#!/usr/bin/env python3
"""
nuvemshop_stock_sync.py
------------------------
Sincroniza a disponibilidade de produtos de terceiros (Dr. Martens, Timberland etc.)
cadastrados manualmente na Nuvemshop, checando se o produto ainda esta disponivel
no site oficial da marca.

Como funciona (fluxo geral):
1. Busca todos os produtos ATIVOS da loja via API da Nuvemshop.
2. Para cada produto, tenta identificar a marca a partir do TITULO (usando o
   dicionario "brands" do config.json).
3. Faz uma busca no site da marca usando o titulo do produto.
4. Compara o titulo do resultado mais parecido com o titulo do seu produto
   (similaridade de texto) para confirmar que e o mesmo item.
5. Se nao encontrar nada parecido, OU encontrar mas estiver marcado como
   esgotado, o produto e DESATIVADO (published=false) na Nuvemshop.
6. Loga tudo em arquivo + console, e nunca derruba a execucao inteira por
   causa de erro em um produto so (try/except por item).

Requisitos: ver requirements.txt
Configuracao: ver config.json (mesma pasta)

IMPORTANTE:
- Deixe "dry_run": true no config.json enquanto estiver testando. Nesse modo
  o script mostra o que FARIA, mas nao altera nada na loja.
- Como a correspondencia e feita por similaridade de titulo (nao por SKU/link
  exato), ela e uma HEURISTICA. Revise o log das primeiras execucoes antes de
  confiar 100% no modo automatico (dry_run=false).
- Se a busca no site da marca FALHAR (bloqueio anti-bot, timeout, erro de
  rede, resposta 404/403 inesperada), o produto NUNCA e desativado por causa
  disso - o script trata como "nao foi possivel confirmar" e pede revisao
  manual. So desativa quando a busca funcionou de verdade e nao achou nada
  parecido o suficiente. Isso evita, por exemplo, que uma marca inteira seja
  desativada soh porque o site dela passou a bloquear scraping (caso real:
  Rick Owens, atras de protecao Cloudflare, devolvendo 404 em toda busca).

CORRECAO 2026-09-18 (Luiz):
- Descoberto caso real de falso positivo: "NEW ROCK ANKLE BOOT METALLIC
  M-285-S30" foi desativado por engano porque o catalogo da New Rock usa
  APENAS O CODIGO como titulo do produto (ex: titulo real = "M-285-S30"),
  enquanto a Nuvemshop tem titulo + descricao completa. A similaridade de
  texto penaliza a descricao extra e derruba o score abaixo do minimo.
  Correcao: para marcas com strategy=shopify_products_json, tenta primeiro
  extrair o codigo de referencia (ultimo token do titulo, ex "M-285-S30")
  e bater EXATO contra o titulo/handle do catalogo antes de cair na
  similaridade de texto. So usa similaridade como fallback quando nao ha
  codigo identificavel no titulo do produto.
"""

import json
import logging
import os
import re
import signal
import sys
import time
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuracao / constantes
# --------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
NUVEMSHOP_API_BASE = "https://api.nuvemshop.com.br/v1"

# Sinalizador usado para distinguir "buscamos e nao achamos nada parecido" (produto
# realmente saiu de linha, seguro desativar) de "nao conseguimos nem fazer a busca"
# (site bloqueou/caiu/deu timeout - NAO e seguro desativar, e um falso positivo).
# Marcas atras de protecao anti-bot (ex: Rick Owens, que devolve a pagina "checking
# your browser..." do Cloudflare) sao o caso classico: toda busca falha com 404/403,
# e sem essa distincao o script desativaria a marca inteira por engano.
SEARCH_UNAVAILABLE = object()


def load_config(path: str = CONFIG_PATH) -> dict:
    """Carrega o config.json e permite sobrescrever credenciais via variaveis
    de ambiente (recomendado ao rodar no GitHub Actions / PythonAnywhere,
    para nao deixar token gravado em texto no repositorio)."""
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Variaveis de ambiente tem prioridade sobre o config.json
    env_store_id = os.environ.get("NUVEMSHOP_STORE_ID")
    env_token = os.environ.get("NUVEMSHOP_ACCESS_TOKEN")
    if env_store_id:
        config["nuvemshop"]["store_id"] = env_store_id
    if env_token:
        config["nuvemshop"]["access_token"] = env_token

    return config


def setup_logging(log_file: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# --------------------------------------------------------------------------
# Utilitarios de texto
# --------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Remove acentos, baixa a caixa e limpa espacos extras, para comparacoes
    de texto mais confiaveis entre PT/EN e sites diferentes."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def title_similarity(title_a: str, title_b: str, brand_name: str = "") -> float:
    """Retorna um score de 0 a 1 de o quanto dois titulos sao parecidos.
    Remove o nome da marca do title_a antes de comparar, ja que o site
    oficial da marca normalmente NAO repete o proprio nome no titulo do
    produto (ex: Nuvemshop tem 'DR. MARTENS 1460 BLACK SMOOTH CLASSIC',
    mas o site da Dr. Martens tem soh '1460 Smooth Leather Boots'). Sem
    essa remocao, o prefixo da marca derruba artificialmente o score de
    TODOS os produtos daquela marca."""
    clean_a = normalize_text(title_a)
    if brand_name:
        clean_a = clean_a.replace(normalize_text(brand_name), "").strip()
    return SequenceMatcher(None, clean_a, normalize_text(title_b)).ratio()


def extract_reference_code(title: str):
    """Extrai um codigo de referencia do fabricante a partir do FINAL do
    titulo do produto, ex: 'NEW ROCK ANKLE BOOT METALLIC M-285-S30' ->
    'M-285-S30'. Retorna None se o ultimo token nao parecer um codigo
    (letras+numeros com separador - ou _, nao apenas um numero de tamanho).
    Usado para correspondencia EXATA contra catalogos onde o titulo do
    fornecedor e so o codigo (caso real: New Rock)."""
    tokens = title.strip().split()
    if not tokens:
        return None
    last = tokens[-1]
    if re.match(r"^[A-Za-z]{1,4}[-_][A-Za-z0-9_\-]+$", last):
        return last
    return None


def detect_brand(product_title: str, brands_config: dict):
    """Tenta achar a marca de um produto procurando o nome da marca dentro
    do titulo. Retorna (chave_da_marca, config_da_marca) ou (None, None)."""
    normalized_title = normalize_text(product_title)
    for brand_key, brand_cfg in brands_config.items():
        if normalize_text(brand_key) in normalized_title:
            return brand_key, brand_cfg
    return None, None


# --------------------------------------------------------------------------
# Cliente da API Nuvemshop
# --------------------------------------------------------------------------

class NuvemshopClient:
    def __init__(self, store_id: str, access_token: str, user_agent: str, timeout: int = 15):
        self.store_id = store_id
        self.timeout = timeout
        self.base_url = f"{NUVEMSHOP_API_BASE}/{store_id}"
        self.headers = {
            "Authentication": f"bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }

    def get_active_products(self) -> list:
        """Busca todos os produtos publicados, paginando automaticamente."""
        products = []
        page = 1
        per_page = 200
        while True:
            url = f"{self.base_url}/products"
            params = {"published": "true", "page": page, "per_page": per_page}
            try:
                with hard_timeout(self.timeout + 10):
                    resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
                resp.raise_for_status()
            except HardTimeout as e:
                logging.error("Timeout absoluto ao buscar produtos da Nuvemshop (pagina %s): %s", page, e)
                break
            except requests.RequestException as e:
                logging.error("Falha ao buscar produtos da Nuvemshop (pagina %s): %s", page, e)
                break

            batch = resp.json()
            if not batch:
                break
            products.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
            time.sleep(0.5)  # respeita rate limit da API

        logging.info("Total de produtos ativos encontrados na Nuvemshop: %d", len(products))
        return products

    def set_product_published(self, product_id: int, published: bool) -> bool:
        """Ativa/desativa um produto na loja. Retorna True se deu certo."""
        url = f"{self.base_url}/products/{product_id}"
        payload = {"published": published}
        try:
            with hard_timeout(self.timeout + 10):
                resp = requests.put(url, headers=self.headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except HardTimeout as e:
            logging.error("Timeout absoluto ao atualizar produto %s na Nuvemshop: %s", product_id, e)
            return False
        except requests.RequestException as e:
            logging.error("Falha ao atualizar produto %s na Nuvemshop: %s", product_id, e)
            return False


# --------------------------------------------------------------------------
# Checagem no site do fornecedor
# --------------------------------------------------------------------------

class HardTimeout(Exception):
    """Levantada quando uma operacao ultrapassa o tempo maximo absoluto,
    mesmo que o timeout normal do requests nao tenha disparado (alguns
    servidores 'vazam' dados devagar o suficiente para escapar do timeout
    de leitura padrao - conhecido como slowloris)."""


@contextmanager
def hard_timeout(seconds: int):
    """Watchdog baseado em signal.alarm: garante que o bloco de codigo dentro
    do 'with' nunca trava por mais que 'seconds', não importa a causa.
    Funciona em Linux/macOS (inclusive nos runners do GitHub Actions).
    Em plataformas sem suporte a SIGALRM (ex: Windows), vira um no-op."""
    if not hasattr(signal, "SIGALRM"):
        yield  # Windows nao suporta SIGALRM - roda sem watchdog nesse caso
        return

    def _handler(signum, frame):
        raise HardTimeout(f"Operacao excedeu o limite absoluto de {seconds}s")

    previous_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def fetch_html(url: str, timeout: int, user_agent: str):
    """Busca uma URL e devolve (status_code, BeautifulSoup ou None).
    Nunca levanta excecao para fora - qualquer problema vira log + None."""
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (compatible; StockSyncBot/1.0)"}
    try:
        # Timeout absoluto = timeout normal + margem de seguranca. Protege contra
        # requisicoes que "vazam" bytes devagar e conseguem escapar do timeout
        # padrao do requests (que reseta a cada pedaco de dado recebido).
        with hard_timeout(timeout + 10):
            resp = requests.get(url, headers=headers, timeout=timeout)
    except HardTimeout as e:
        logging.warning("Timeout absoluto ao acessar %s: %s", url, e)
        return None, None
    except requests.RequestException as e:
        logging.warning("Erro de rede ao acessar %s: %s", url, e)
        return None, None

    if resp.status_code == 404:
        return 404, None

    if resp.status_code >= 400:
        logging.warning("Site retornou status %s para %s", resp.status_code, url)
        return resp.status_code, None

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:  # protege contra HTML malformado / mudanca de estrutura
        logging.warning("Erro ao interpretar HTML de %s: %s", url, e)
        return resp.status_code, None

    return resp.status_code, soup


def search_brand_site(product_title: str, brand_cfg: dict, settings: dict, user_agent: str):
    """Pesquisa o titulo do produto no site da marca e retorna o melhor
    resultado encontrado como dict: {"title": str, "url": str, "similarity": float},
    SEARCH_UNAVAILABLE se a busca nao pode nem ser carregada (bloqueio/erro de
    rede - nao sabemos se o produto existe ou nao), ou None se a busca
    carregou normalmente mas nao achou nenhum candidato relevante."""

    search_url = brand_cfg["search_url_template"].format(query=quote_plus(product_title))
    status, soup = fetch_html(search_url, settings["request_timeout_seconds"], user_agent)

    if soup is None:
        logging.warning("Nao foi possivel carregar a busca em %s (status=%s)", search_url, status)
        return SEARCH_UNAVAILABLE

    candidates = []

    # Tenta os seletores configurados especificamente para essa marca primeiro
    selectors = brand_cfg.get("product_link_selectors", [])
    for selector in selectors:
        try:
            for link in soup.select(selector):
                text = link.get_text(strip=True)
                href = link.get("href")
                if text and href:
                    candidates.append((text, href))
        except Exception as e:
            logging.debug("Seletor '%s' falhou em %s: %s", selector, search_url, e)

    # Fallback generico: qualquer link cujo texto pareca um nome de produto
    # (mais de 3 palavras, sem ser menu/rodape) - usado se os seletores
    # especificos nao acharem nada, por exemplo apos o site mudar de layout.
    # Se a marca tiver "product_url_pattern" configurado no config.json, o
    # fallback so aceita links cuja URL bate com esse padrao (regex) - isso
    # evita pegar links de menu/rodape (ex: "Terug naar school") que tem
    # 3+ palavras mas nao levam a pagina nenhuma de produto.
    if not candidates:
        url_pattern = brand_cfg.get("product_url_pattern")
        compiled_pattern = re.compile(url_pattern) if url_pattern else None
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            href = link["href"]
            if not text or len(text.split()) < 3:
                continue
            if compiled_pattern is not None and not compiled_pattern.search(href):
                continue
            candidates.append((text, href))

    if not candidates:
        return None

    # Escolhe o candidato mais parecido com o titulo do nosso produto
    best_text, best_href, best_score = None, None, 0.0
    for text, href in candidates:
        score = title_similarity(product_title, text, brand_name=brand_cfg.get("display_name", ""))
        if score > best_score:
            best_text, best_href, best_score = text, href, score

    if best_text is None:
        return None

    return {"title": best_text, "url": best_href, "similarity": best_score}


def check_product_page_stock(url: str, brand_cfg: dict, settings: dict, user_agent: str):
    """Abre a pagina do produto encontrado e decide se esta em estoque.
    Retorna True (em estoque), False (esgotado/indisponivel) ou None
    (nao foi possivel determinar - trata-se com cautela pelo chamador)."""

    if not url.startswith("http"):
        # Sites costumam devolver links relativos; sem o dominio nao da pra checar
        logging.debug("URL de produto relativa e sem dominio, pulando checagem de pagina: %s", url)
        return None

    status, soup = fetch_html(url, settings["request_timeout_seconds"], user_agent)

    if status == 404:
        return False  # pagina sumiu = produto nao existe mais

    if soup is None:
        return None  # erro de rede/parse - nao decide nada com base nisso

    page_text = normalize_text(soup.get_text(" "))

    out_of_stock_keywords = [normalize_text(k) for k in brand_cfg.get("out_of_stock_keywords", [])]
    buy_keywords = [normalize_text(k) for k in brand_cfg.get("buy_button_keywords", [])]

    has_out_of_stock_text = any(k in page_text for k in out_of_stock_keywords)
    has_buy_button = any(k in page_text for k in buy_keywords)

    if has_out_of_stock_text and not has_buy_button:
        return False
    if has_buy_button and not has_out_of_stock_text:
        return True

    # Sinal ambiguo (achou os dois, ou nenhum dos dois) - nao decide sozinho,
    # melhor deixar para revisao manual do que desativar por engano.
    return None


# --------------------------------------------------------------------------
# Fluxo principal
# --------------------------------------------------------------------------

# Cache em memoria do catalogo Shopify por marca, valido apenas durante esta
# execucao do script. Sem isso, cada PRODUTO da marca dispararia um download
# completo e repetido do catalogo (paginado, 20 requests) - o que sobrecarrega
# o site (erros 429 "Too Many Requests") e consome a maior parte do tempo
# disponivel antes do deadline, impedindo o script de processar o catalogo
# inteiro da Nuvemshop.
_shopify_catalog_cache: dict = {}


def fetch_shopify_products(base_products_json_url: str, timeout: int, user_agent: str, max_pages: int = 20):
    """Baixa o catalogo completo de uma loja Shopify via o endpoint publico
    /products.json (paginado). Alternativa mais confiavel do que raspar HTML
    quando a busca do site depende de JavaScript para mostrar resultados.
    Resultado e armazenado em cache (por URL base) para nao repetir o
    download a cada produto da mesma marca.

    Retorna a lista de produtos, SEARCH_UNAVAILABLE se nem a primeira pagina
    do catalogo pode ser carregada (bloqueio/erro de rede - nao sabemos se o
    produto existe ou nao), ou lista vazia se o catalogo carregou mas
    realmente veio sem produtos."""
    if base_products_json_url in _shopify_catalog_cache:
        return _shopify_catalog_cache[base_products_json_url]

    headers = {"User-Agent": user_agent or "Mozilla/5.0 (compatible; StockSyncBot/1.0)"}
    separator = "&" if "?" in base_products_json_url else "?"
    all_products = []
    load_failed = False
    for page in range(1, max_pages + 1):
        url = f"{base_products_json_url}{separator}page={page}"
        try:
            with hard_timeout(timeout + 10):
                resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
        except HardTimeout as e:
            logging.warning("Timeout absoluto ao acessar %s: %s", url, e)
            load_failed = (page == 1)
            break
        except requests.RequestException as e:
            logging.warning("Erro de rede ao acessar %s: %s", url, e)
            load_failed = (page == 1)
            break
        try:
            data = resp.json()
        except ValueError:
            logging.warning("Resposta nao-JSON em %s, parando paginacao.", url)
            load_failed = (page == 1)
            break
        batch = data.get("products", [])
        if not batch:
            break
        all_products.extend(batch)
        page_size_hint = 30
        if len(batch) < page_size_hint:
            break

    if all_products:
        # So armazena em cache resultados nao-vazios: se a primeira tentativa
        # falhou (ex: rate limit passageiro), deixamos o proximo produto
        # dessa marca tentar de novo, em vez de travar "catalogo indisponivel"
        # para o resto da execucao inteira.
        _shopify_catalog_cache[base_products_json_url] = all_products
        return all_products

    if load_failed:
        # Nem a primeira pagina do catalogo carregou - nao sabemos se o produto
        # existe ou nao, so que nao conseguimos checar agora.
        return SEARCH_UNAVAILABLE

    return all_products  # catalogo carregou normalmente e realmente veio vazio


def search_shopify_products_json(product_title: str, brand_cfg: dict, settings: dict, user_agent: str):
    """Estrategia alternativa para lojas Shopify cuja pagina de busca depende
    de JavaScript. Baixa o catalogo via /products.json e decide disponibilidade.

    Tenta DUAS estrategias, nessa ordem:
    1. Correspondencia EXATA por codigo de referencia: extrai o codigo do
       final do titulo do produto (ex: "M-285-S30") e busca um produto no
       catalogo cujo titulo OU handle bata exatamente com esse codigo. Muitos
       catalogos de fornecedor (caso confirmado: New Rock) usam SO o codigo
       como titulo do produto, entao comparar por similaridade de texto contra
       o titulo completo da Nuvemshop (que tem descricao extra) frequentemente
       falha mesmo quando o produto existe.
    2. Fallback por similaridade de texto (metodo original), usado quando nao
       ha codigo identificavel no titulo ou quando o codigo nao bate com nada
       no catalogo.
    """
    products_json_url = brand_cfg["products_json_url"]
    all_products = fetch_shopify_products(products_json_url, settings["request_timeout_seconds"], user_agent)

    if all_products is SEARCH_UNAVAILABLE:
        return SEARCH_UNAVAILABLE

    if not all_products:
        logging.warning("Catalogo Shopify carregou vazio: %s", products_json_url)
        return None

    # --- Estrategia 1: correspondencia exata por codigo de referencia ---
    code = extract_reference_code(product_title)
    if code:
        normalized_code = normalize_text(code)
        normalized_code_compact = normalized_code.replace("-", "").replace("_", "")
        for p in all_products:
            candidate_title = normalize_text(p.get("title", ""))
            candidate_handle = normalize_text(p.get("handle", "")).replace("-", "")
            if normalized_code == candidate_title or normalized_code_compact == candidate_handle:
                variants = p.get("variants", [])
                in_stock = any(v.get("available") for v in variants) if variants else None
                logging.info(
                    "Correspondencia exata por codigo '%s' -> produto '%s' do catalogo.",
                    code, p.get("title", ""),
                )
                return {
                    "title": p.get("title", ""),
                    "similarity": 1.0,
                    "matched": True,
                    "in_stock": in_stock,
                    "match_method": "exact_code",
                }
        logging.debug("Codigo '%s' extraido do titulo mas nao encontrado no catalogo; tentando similaridade.", code)

    # --- Estrategia 2: fallback por similaridade de texto ---
    best_product, best_score = None, 0.0
    for p in all_products:
        score = title_similarity(product_title, p.get("title", ""), brand_name=brand_cfg.get("display_name", ""))
        if score > best_score:
            best_product, best_score = p, score

    if best_product is None:
        return None

    min_similarity = settings.get("min_title_similarity", 0.6)
    matched = best_score >= min_similarity
    in_stock = None
    if matched:
        variants = best_product.get("variants", [])
        in_stock = any(v.get("available") for v in variants) if variants else None

    return {
        "title": best_product.get("title", ""),
        "similarity": best_score,
        "matched": matched,
        "in_stock": in_stock,
        "match_method": "similarity",
    }


def process_product(product: dict, brands_config: dict, settings: dict, user_agent: str, client: NuvemshopClient) -> bool:
    """Processa um produto. Retorna True se fez alguma chamada de rede (para o
    chamador saber se vale a pena aplicar o intervalo de espera entre produtos),
    False se o produto foi pulado sem nenhuma requisicao externa."""
    product_id = product.get("id")
    # Titulo pode vir em varios idiomas: pega o primeiro disponivel (pt preferencial)
    name_field = product.get("name", {})
    product_title = name_field.get("pt") or name_field.get("es") or name_field.get("en") or ""
    product_title = product_title.strip()

    if not product_title:
        logging.warning("Produto %s sem titulo legivel, pulando.", product_id)
        return False

    brand_key, brand_cfg = detect_brand(product_title, brands_config)
    if not brand_cfg:
        logging.info("[%s] '%s' - marca nao mapeada no config.json, pulando (mapeie manualmente se necessario).",
                     product_id, product_title)
        return False

    logging.info("[%s] '%s' -> marca identificada: %s", product_id, product_title, brand_cfg["display_name"])

    if brand_cfg.get("strategy") == "shopify_products_json":
        try:
            sj_result = search_shopify_products_json(product_title, brand_cfg, settings, user_agent)
        except Exception as e:
            logging.error("[%s] Erro inesperado consultando products.json da marca: %s", product_id, e)
            return True

        if sj_result is SEARCH_UNAVAILABLE:
            logging.info("[%s] Catalogo Shopify da marca %s indisponivel (bloqueio/erro de rede) - "
                         "nenhuma acao tomada (revise manualmente).", product_id, brand_cfg["display_name"])
            return True

        if sj_result is None or not sj_result["matched"]:
            found_desc = (f"(melhor match: '{sj_result['title']}' score={sj_result['similarity']:.2f})"
                          if sj_result else "(catalogo vazio)")
            logging.info("[%s] Nao encontrado no catalogo Shopify da marca %s -> desativar. %s",
                         product_id, brand_cfg["display_name"], found_desc)
            deactivate_product(product_id, product_title, settings, client)
            return True

        logging.info("[%s] Encontrado no catalogo: '%s' (similaridade=%.2f, metodo=%s)",
                     product_id, sj_result["title"], sj_result["similarity"], sj_result.get("match_method", "?"))

        if sj_result["in_stock"] is False:
            logging.info("[%s] Sem variantes disponiveis no catalogo -> desativar.", product_id)
            deactivate_product(product_id, product_title, settings, client)
        elif sj_result["in_stock"] is True:
            logging.info("[%s] Disponivel no catalogo Shopify.", product_id)
            if settings.get("auto_reenable_if_back_in_stock") and not product.get("published", True):
                reactivate_product(product_id, product_title, settings, client)
        else:
            logging.info("[%s] Nao foi possivel confirmar disponibilidade com certeza - nenhuma acao tomada.", product_id)
        return True

    try:
        result = search_brand_site(product_title, brand_cfg, settings, user_agent)
    except Exception as e:
        # Rede caiu, site mudou de estrutura, etc. Nunca deixa isso derrubar o script.
        logging.error("[%s] Erro inesperado buscando no site da marca: %s", product_id, e)
        return True

    if result is SEARCH_UNAVAILABLE:
        logging.info("[%s] Busca no site da marca %s indisponivel (bloqueio/erro de rede) - "
                     "nenhuma acao tomada (revise manualmente).", product_id, brand_cfg["display_name"])
        return True

    min_similarity = settings.get("min_title_similarity", 0.6)

    if result is None or result["similarity"] < min_similarity:
        # Nao achamos nada parecido o suficiente -> tratamos como indisponivel
        found_desc = f"(melhor match: '{result['title']}' score={result['similarity']:.2f})" if result else "(nenhum resultado)"
        logging.info("[%s] Nao encontrado no site da marca %s -> desativar. %s",
                     product_id, brand_cfg["display_name"], found_desc)
        deactivate_product(product_id, product_title, settings, client)
        return True

    logging.info("[%s] Encontrado: '%s' (similaridade=%.2f) -> %s",
                 product_id, result["title"], result["similarity"], result["url"])

    try:
        in_stock = check_product_page_stock(result["url"], brand_cfg, settings, user_agent)
    except Exception as e:
        logging.error("[%s] Erro inesperado checando estoque na pagina do produto: %s", product_id, e)
        return True

    if in_stock is False:
        logging.info("[%s] Marcado como ESGOTADO no site da marca -> desativar.", product_id)
        deactivate_product(product_id, product_title, settings, client)
    elif in_stock is True:
        logging.info("[%s] Disponivel no site da marca.", product_id)
        if settings.get("auto_reenable_if_back_in_stock") and not product.get("published", True):
            reactivate_product(product_id, product_title, settings, client)
    else:
        logging.info("[%s] Nao foi possivel confirmar o status de estoque com certeza - "
                     "nenhuma acao tomada (revise manualmente).", product_id)

    return True


def deactivate_product(product_id: int, product_title: str, settings: dict, client: NuvemshopClient):
    if settings.get("dry_run", True):
        logging.info("[DRY-RUN] Desativaria o produto %s ('%s') na Nuvemshop.", product_id, product_title)
        return
    ok = client.set_product_published(product_id, False)
    if ok:
        logging.info("Produto %s ('%s') DESATIVADO na Nuvemshop.", product_id, product_title)


def reactivate_product(product_id: int, product_title: str, settings: dict, client: NuvemshopClient):
    if settings.get("dry_run", True):
        logging.info("[DRY-RUN] Reativaria o produto %s ('%s') na Nuvemshop.", product_id, product_title)
        return
    ok = client.set_product_published(product_id, True)
    if ok:
        logging.info("Produto %s ('%s') REATIVADO na Nuvemshop.", product_id, product_title)


def main():
    config = load_config()
    settings = config["settings"]
    setup_logging(settings.get("log_file", "stock_sync.log"))

    logging.info("=== Iniciando sincronizacao de estoque (dry_run=%s) ===", settings.get("dry_run", True))

    store_id = config["nuvemshop"]["store_id"]
    access_token = config["nuvemshop"]["access_token"]
    user_agent = config["nuvemshop"].get("user_agent", "StockSyncBot/1.0")

    if not store_id or "SEU_" in str(store_id) or not access_token or "SEU_" in str(access_token):
        logging.error("Credenciais da Nuvemshop nao configuradas. Preencha config.json ou defina as "
                       "variaveis de ambiente NUVEMSHOP_STORE_ID e NUVEMSHOP_ACCESS_TOKEN.")
        sys.exit(1)

    client = NuvemshopClient(store_id, access_token, user_agent, settings.get("request_timeout_seconds", 15))
    products = client.get_active_products()

    if not products:
        logging.warning("Nenhum produto ativo retornado pela API. Encerrando.")
        return

    # SISTEMA DE DEADLINE: para graciosamente se o tempo estiver acabando.
    start_time = time.time()
    deadline_seconds = 85 * 60  # 85 minutos (margem de 5 min antes do timeout de 90 min do workflow)
    total_products = len(products)

    for idx, product in enumerate(products, 1):
        # Checa se ja passou do deadline ANTES de processar mais um produto
        elapsed = time.time() - start_time
        if elapsed > deadline_seconds:
            logging.warning(
                f"=== DEADLINE ATINGIDO: {elapsed:.0f}s decorridos (limite: {deadline_seconds}s). "
                f"Parando graciosamente. Processados {idx-1}/{total_products} produtos. ==="
            )
            break

        try:
            made_network_call = process_product(product, config["brands"], settings, user_agent, client)
        except Exception as e:
            # Ultima linha de defesa: um produto com problema nunca para os demais.
            logging.error("Erro nao tratado processando produto %s: %s", product.get("id"), e)
            made_network_call = True  # por seguranca, aplica o intervalo mesmo assim

        if made_network_call:
            time.sleep(settings.get("delay_between_requests_seconds", 2))

    elapsed_final = time.time() - start_time
    logging.info(f"=== Sincronizacao concluida === (tempo total: {elapsed_final:.0f}s)")


if __name__ == "__main__":
    main()
