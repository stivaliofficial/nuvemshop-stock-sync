#!/usr/bin/env python3
"""
STIVALI - Padronizador de fundo branco das fotos de produto (Nuvemshop)

O que faz, para cada foto de cada produto:
  1. Baixa a foto e analisa a borda para descobrir a cor do fundo.
  2. Classifica:
       - ja_branco         -> fundo já é branco, nada a fazer
       - fundo_cinza       -> fundo cinza claro liso de estúdio -> CORRIGE
       - fundo_nao_padrao  -> foto de lifestyle/fundo colorido -> NÃO mexe (vai para revisão)
  3. Na correção, clareia SÓ o fundo (detectado a partir das bordas), preservando o
     produto e as sombras suaves, e deixa o fundo em branco puro (#FFFFFF).
  4. Fora do modo teste: sobe a foto nova na mesma posição, passa as variantes que
     usavam a foto antiga para a nova e apaga a antiga.

Segurança:
  - DRY_RUN=true (padrão): não altera NADA na loja. Gera relatório CSV e
    comparações antes/depois para você aprovar.
  - Guarda as fotos originais alteradas em output/originais/ (backup).
  - É idempotente: rodar de novo pula as fotos que já ficaram brancas, então
    se o tempo acabar, é só rodar de novo que ele continua de onde parou.

Variáveis de ambiente:
  NUVEMSHOP_ACCESS_TOKEN  (obrigatória)
  NUVEMSHOP_STORE_ID      (padrão 3277373)
  DRY_RUN                 true/false (padrão true)
  MARCA                   filtra por marca (ex: "Dr. Martens"); vazio = todas
  LIMITE_PRODUTOS         máximo de produtos analisados (0 = sem limite)
  DIAS_RECENTES           só produtos criados nos últimos N dias (0 = todos)
  AMOSTRAS_COMPARACAO     quantas comparações antes/depois salvar (padrão 80)
  PRAZO_MINUTOS           para com segurança depois de N minutos (padrão 330)
  ACAO                    branquear (padrão) | reordenar | reparar
                          reordenar: nas primeiras POSICOES_VITRINE fotos (capa + hover),
                          se a foto não for de fundo branco (ex: foto de modelo), troca de
                          lugar com a próxima foto branca do produto. Não edita nem apaga nada.
  FOTOS_POR_PRODUTO       1 = só a capa, 2 = capa + 2ª foto, 0 = todas (padrão 0)
"""

import base64
import csv
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from PIL import Image, ImageOps
from scipy import ndimage

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
TOKEN = os.environ.get("NUVEMSHOP_ACCESS_TOKEN", "")
STORE_ID = os.environ.get("NUVEMSHOP_STORE_ID", "3277373")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
MARCA = os.environ.get("MARCA", "").strip().lower()
LIMITE_PRODUTOS = int(os.environ.get("LIMITE_PRODUTOS", "0") or 0)
DIAS_RECENTES = int(os.environ.get("DIAS_RECENTES", "0") or 0)
AMOSTRAS_COMPARACAO = int(os.environ.get("AMOSTRAS_COMPARACAO", "80") or 80)
POLIR = os.environ.get("POLIR", "true").strip().lower() != "false"
ACAO = (os.environ.get("ACAO", "branquear") or "branquear").strip().lower()
POSICOES_VITRINE = int(os.environ.get("POSICOES_VITRINE", "2") or 2)  # capa + hover
BRANCA_TOTAL_MIN = 0.84   # na vitrine só vale foto com a borda praticamente toda branca (sem faixa de chão)
PRAZO_MINUTOS = int(os.environ.get("PRAZO_MINUTOS", "330") or 330)
FOTOS_POR_PRODUTO = int(os.environ.get("FOTOS_POR_PRODUTO", "0") or 0)

API = f"https://api.nuvemshop.com.br/v1/{STORE_ID}"
USER_AGENT = "STIVALI Fundo Branco (contato@stivaliofficial.com)"
OUT_DIR = "output"

# Parâmetros da detecção (valores conservadores)
BORDA_PCT = 0.02          # faixa da borda usada para medir o fundo (2% da imagem)
BRANCO_MIN = 248          # fundo com todos os canais >= isto já é branco
CINZA_MIN = 200           # fundo mais escuro que isto não é "cinza de estúdio"
CROMA_MAX = 14            # diferença máx. entre canais (garante cinza neutro, não bege/colorido)
UNIFORMIDADE_MIN = 0.55   # % da borda que precisa ter a cor do fundo
TOL_FUNDO = 10            # tolerância para considerar pixel da borda "igual ao fundo"
TOL_REGIAO = 60           # tolerância para expandir o fundo (inclui sombras suaves)
GRADIENTE_MAX = 3.0       # o fundo/sombra só "escorre" por áreas lisas; contorno do produto bloqueia
CLAREAR_DE = 226          # depois de clarear, tons do fundo acima disso vão suavemente
CLAREAR_ATE = 248         # ... até virar branco puro a partir deste valor (sem "anel" na sombra)


# ----------------------------------------------------------------------------
# Processamento de imagem
# ----------------------------------------------------------------------------
def carregar_rgb(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fundo = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(fundo, img)
    return img.convert("RGB")


def coords_da_borda(h, w):
    b = max(2, int(min(h, w) * BORDA_PCT))
    m = np.zeros((h, w), bool)
    m[:b, :] = m[-b:, :] = True
    m[:, :b] = m[:, -b:] = True
    return m


def pixels_da_borda(arr: np.ndarray) -> np.ndarray:
    return arr[coords_da_borda(*arr.shape[:2])].reshape(-1, 3)


def tem_chao(arr: np.ndarray) -> bool:
    """Foto editorial (parede + chão): a faixa de baixo tem outra cor que as laterais."""
    h, w, _ = arr.shape
    b = max(2, int(min(h, w) * BORDA_PCT))
    baixo = np.median(arr[-b:, w // 4: 3 * w // 4].reshape(-1, 3), axis=0)
    lados = np.median(np.concatenate([arr[h // 3: 2 * h // 3, :b].reshape(-1, 3),
                                      arr[h // 3: 2 * h // 3, -b:].reshape(-1, 3)]), axis=0)
    return float(np.abs(baixo.astype(float) - lados.astype(float)).max()) > 14

POLIR_DE, POLIR_ATE = 208.0, 244.0

def polir(arr):
    """Foto que já tem fundo branco mas ficou com sombras/degradês cinza-claro no fundo:
    leva suavemente para branco puro só o FUNDO (neutro, claro, liso, ligado à borda).
    Sombra de contato forte (mais escura) e o produto ficam intactos."""
    a = arr.astype(np.float32)
    mn = a.min(axis=2)
    croma = ndimage.gaussian_filter(a.max(axis=2) - mn, 2.0)
    lum_s = ndimage.gaussian_filter(a.mean(axis=2), 1.0)
    grad = np.hypot(ndimage.sobel(lum_s, 0), ndimage.sobel(lum_s, 1)) / 8.0
    cand = (mn >= POLIR_DE - 6) & (croma <= 10) & ((grad <= 2.5) | (mn >= 248))
    lab, _ = ndimage.label(cand)
    borda = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    mask = np.isin(lab, borda[borda != 0])
    if (mask & (mn < 251)).mean() < 0.003:
        return None
    t = np.clip((mn - POLIR_DE) / (POLIR_ATE - POLIR_DE), 0, 1)
    t = (t * t * (3 - 2 * t))[..., None]
    alvo = a + (255.0 - a) * t
    m = ndimage.gaussian_filter(mask.astype(np.float32), 1.2)[..., None]
    out = np.clip(np.rint(a * (1 - m) + alvo * m), 0, 255).astype(np.uint8)
    if np.abs(out.astype(np.int16) - arr.astype(np.int16)).mean() < 0.35:
        return None
    return out


def analisar(arr: np.ndarray):
    """Retorna (classificacao, fundo, mediana, uniformidade).
    Critério validado: só fundo cinza LISO de estúdio. Fotos editoriais (parede +
    chão), fundos coloridos ou escuros ficam de fora de propósito."""
    borda = pixels_da_borda(arr).astype(np.int16)
    fundo = np.median(borda, axis=0)
    uniformidade = float((np.abs(borda - fundo).max(axis=1) <= TOL_FUNDO).mean())
    if fundo.min() >= BRANCO_MIN:
        return "ja_branco", fundo, fundo, uniformidade
    croma = fundo.max() - fundo.min()
    chao = tem_chao(arr)
    if (fundo.min() >= CINZA_MIN and croma <= CROMA_MAX and uniformidade >= UNIFORMIDADE_MIN
            and not chao):
        return "fundo_cinza", fundo, fundo, uniformidade
    return "fundo_nao_padrao", fundo, fundo, uniformidade


def clarear_fundo(arr: np.ndarray, sup: np.ndarray) -> np.ndarray:
    """Deixa o fundo branco preservando produto e sombras suaves."""
    h, w, _ = arr.shape
    a = arr.astype(np.float32)
    f = sup.astype(np.float32)   # fundo pixel a pixel (acompanha o degradê)

    # 1) Candidatos a fundo: neutros, claros, próximos da cor do fundo (inclui sombras
    #    suaves) e em área LISA. O contorno do produto tem gradiente alto e funciona
    #    como barreira, então mesmo produto cinza-claro não é confundido com o fundo.
    dist = np.abs(a - f).max(axis=2)
    croma = a.max(axis=2) - a.min(axis=2)
    croma_fundo = float(np.max(f) - np.min(f)) if np.ndim(f) == 1 else 0.0
    lum = a.mean(axis=2)
    lum_fundo = float(np.mean(f))
    croma_s = ndimage.gaussian_filter(croma, sigma=2.0)   # tira o ruído de cor do JPEG
    # Proteções para produtos claros (creme, off-white, branco):
    #  - fundo de estúdio é NEUTRO: pixel com cor (creme/bege) não é fundo
    #  - fundo pode ter degradê de luz, mas muito mais claro que ele é produto
    lum_s = ndimage.gaussian_filter(lum, sigma=1.0)
    grad = np.hypot(ndimage.sobel(lum_s, axis=0), ndimage.sobel(lum_s, axis=1)) / 8.0
    candidato = ((dist <= TOL_REGIAO)
                 & (croma_s <= croma_fundo + 9)
                 & (lum <= lum_fundo + 22)
                 & ((grad <= GRADIENTE_MAX) | (dist <= TOL_FUNDO)))

    # 2) Só vale o que está ligado à borda da foto (flood fill) -> não pega o produto
    rotulos, _ = ndimage.label(candidato)
    na_borda = np.unique(np.concatenate([
        rotulos[0, :], rotulos[-1, :], rotulos[:, 0], rotulos[:, -1]
    ]))
    na_borda = na_borda[na_borda != 0]
    mascara = np.isin(rotulos, na_borda)

    # 3) "Buracos" de fundo fechados (ex: entre tiras de sandália): só se forem
    #    praticamente idênticos ao fundo e lisos (produto tem textura, fundo não)
    justo = (dist <= 5) & ~mascara
    rot2, n2 = ndimage.label(justo)
    if n2:
        area_min = max(50, int(h * w * 0.0005))
        tamanhos = ndimage.sum(np.ones_like(rot2), rot2, index=np.arange(1, n2 + 1))
        lum = a.mean(axis=2)
        desvios = ndimage.standard_deviation(lum, rot2, index=np.arange(1, n2 + 1))
        validos = [i + 1 for i in range(n2) if tamanhos[i] >= area_min and desvios[i] <= 1.6]
        if validos:
            mascara |= np.isin(rot2, validos)

    # 4) Borda suave para não ficar recortado
    m = ndimage.gaussian_filter(mascara.astype(np.float32), sigma=1.2)[..., None]

    # 5) "Dividir pelo fundo": o cinza vira branco e as sombras continuam proporcionais
    clareado = np.clip(a * (255.0 / np.maximum(f, 1.0)), 0, 255)
    #    Rampa suave para branco: elimina ruído/JPEG do fundo sem criar contorno duro
    t = np.clip((clareado.min(axis=2, keepdims=True) - CLAREAR_DE) / (CLAREAR_ATE - CLAREAR_DE), 0, 1)
    t = t * t * (3 - 2 * t)
    clareado = clareado + (255.0 - clareado) * t

    saida = a * (1 - m) + clareado * m
    return np.clip(np.rint(saida), 0, 255).astype(np.uint8)


def processar_bytes(data: bytes):
    """Retorna (classificacao, info, imagem_nova_em_bytes_ou_None, img_original, img_nova)."""
    img = carregar_rgb(data)
    arr = np.asarray(img)
    classe, sup, fundo, unif = analisar(arr)
    info = {"fundo_rgb": "-".join(str(int(x)) for x in fundo), "uniformidade": round(unif, 2)}
    if classe == "ja_branco" and POLIR and not tem_chao(arr):
        polida = polir(arr)
        if polida is not None:
            info["obs"] = "polimento (sombras cinza-claro do fundo)"
            img_nova = Image.fromarray(polida)
            buf = io.BytesIO()
            img_nova.save(buf, format="JPEG", quality=93, optimize=True, subsampling=0)
            return "corrigir", info, buf.getvalue(), img, img_nova
    if classe != "fundo_cinza":
        return classe, info, None, img, None

    novo = clarear_fundo(arr, sup)
    # Verificação final: a borda tem que ter ficado branca
    borda_nova = np.median(pixels_da_borda(novo), axis=0)
    if borda_nova.min() < 250:
        info["obs"] = f"borda ficou {borda_nova.astype(int).tolist()}"
        return "falhou_verificacao", info, None, img, Image.fromarray(novo)

    img_nova = Image.fromarray(novo)
    buf = io.BytesIO()
    img_nova.save(buf, format="JPEG", quality=93, optimize=True, subsampling=0)
    return "corrigir", info, buf.getvalue(), img, img_nova


def salvar_comparacao(original: Image.Image, nova: Image.Image, caminho: str):
    alvo = 600
    def red(i):
        i = i.copy(); i.thumbnail((alvo, alvo)); return i
    o, n = red(original), red(nova)
    tela = Image.new("RGB", (o.width + n.width + 30, max(o.height, n.height) + 20), (255, 0, 140))
    tela.paste(o, (10, 10)); tela.paste(n, (o.width + 20, 10))
    tela.save(caminho, quality=85)


# ----------------------------------------------------------------------------
# API Nuvemshop
# ----------------------------------------------------------------------------
class Nuvemshop:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authentication": f"bearer {token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })

    def _req(self, metodo, caminho, **kw):
        r = None
        for tentativa in range(10):
            try:
                r = self.s.request(metodo, API + caminho, timeout=90, **kw)
            except requests.RequestException:
                time.sleep(min(5 * (tentativa + 1), 60))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                espera = float(r.headers.get("x-rate-limit-reset", 0) or 0) / 1000 or (2 ** tentativa)
                time.sleep(min(max(espera, 1), 30))
                continue
            # Respeita o "balde" de limite da API (40 req, esvazia 2/s)
            rest = r.headers.get("x-rate-limit-remaining")
            if rest is not None and rest.isdigit() and int(rest) < 5:
                time.sleep(2)
            return r
        if r is None:
            raise RuntimeError(f"sem resposta da API: {metodo} {caminho}")
        r.raise_for_status()
        return r

    def produtos(self, criados_desde=None):
        pagina = 1
        while True:
            params = {"page": pagina, "per_page": 200,
                      "fields": "id,name,brand,images,variants"}
            if criados_desde:
                params["created_at_min"] = criados_desde
            r = self._req("GET", "/products", params=params)
            if r.status_code == 404:   # passou da última página
                return
            r.raise_for_status()
            lote = r.json()
            if not lote:
                return
            yield from lote
            pagina += 1

    def produto(self, pid):
        r = self._req("GET", f"/products/{pid}", params={"fields": "id,name,images,variants"})
        r.raise_for_status()
        return r.json()

    def subir_imagem(self, pid, conteudo: bytes, posicao: int, alt=None, nome="foto.jpg"):
        corpo = {"attachment": base64.b64encode(conteudo).decode(),
                 "filename": nome, "position": posicao}
        if alt:
            corpo["alt"] = alt
        r = self._req("POST", f"/products/{pid}/images", json=corpo)
        if r.status_code >= 400 and alt:
            corpo.pop("alt")
            r = self._req("POST", f"/products/{pid}/images", json=corpo)
        r.raise_for_status()
        return r.json()

    def trocar_imagem_variante(self, pid, vid, image_id):
        r = self._req("PUT", f"/products/{pid}/variants/{vid}", json={"image_id": image_id})
        r.raise_for_status()

    def apagar_imagem(self, pid, image_id):
        r = self._req("DELETE", f"/products/{pid}/images/{image_id}")
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def posicionar_imagem(self, pid, image_id, posicao):
        r = self._req("PUT", f"/products/{pid}/images/{image_id}", json={"position": posicao})
        r.raise_for_status()


def nome_produto(p):
    n = p.get("name") or {}
    return n.get("pt") or n.get("es") or n.get("en") or "" if isinstance(n, dict) else str(n)


def marca_produto(p):
    b = p.get("brand") or ""
    return (b.get("pt") or b.get("es") or "") if isinstance(b, dict) else str(b)


# ----------------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------------
def eh_branca(download, img, cache):
    """True se a foto tem fundo branco. Usa cache para não baixar duas vezes."""
    iid = img["id"]
    if iid not in cache:
        src = img["src"]
        if src.startswith("//"):
            src = "https:" + src
        try:
            r = download.get(src, timeout=60)
            r.raise_for_status()
            im = carregar_rgb(r.content)
            classe, _, _, unif = analisar(np.asarray(im))
            # foto de modelo com parede branca e chão cinza NÃO conta como branca
            cache[iid] = (classe == "ja_branco" and unif >= BRANCA_TOTAL_MIN, im)
        except Exception:
            cache[iid] = (None, None)  # não deu para baixar -> não mexe
    return cache[iid][0]


def salvar_comparacao_ordem(antes, depois, caminho):
    alt = 260
    def red(i):
        i = i.copy(); i.thumbnail((alt, alt)); return i
    aa = [red(i) for i in antes if i is not None]
    dd = [red(i) for i in depois if i is not None]
    w1 = sum(i.width + 10 for i in aa); w2 = sum(i.width + 10 for i in dd)
    tela = Image.new("RGB", (w1 + w2 + 40, alt + 20), (255, 0, 140))
    x = 10
    for i in aa:
        tela.paste(i, (x, 10)); x += i.width + 10
    x += 20
    for i in dd:
        tela.paste(i, (x, 10)); x += i.width + 10
    tela.save(caminho, quality=82)


def reordenar_produto(api, download, p, w, cont, marca):
    pid, nome = p["id"], nome_produto(p)
    imagens = sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)
    if len(imagens) < 2:
        return
    posicoes = [i.get("position") or (k + 1) for k, i in enumerate(imagens)]
    ordem = list(imagens)
    cache = {}
    trocas = []
    for k in range(min(POSICOES_VITRINE, len(ordem))):
        if eh_branca(download, ordem[k], cache) is not False:
            continue  # já é branca (ou não deu para analisar)
        for j in range(k + 1, len(ordem)):
            if eh_branca(download, ordem[j], cache):
                trocas.append((posicoes[k], posicoes[j]))
                ordem[k], ordem[j] = ordem[j], ordem[k]
                break
    if not trocas:
        cont["ja_ok"] += 1
        return

    if cont["comparacoes"] < AMOSTRAS_COMPARACAO:
        antes = [cache.get(i["id"], (None, None))[1] for i in imagens[:POSICOES_VITRINE]]
        depois = [cache.get(i["id"], (None, None))[1] for i in ordem[:POSICOES_VITRINE]]
        salvar_comparacao_ordem(antes, depois, f"{OUT_DIR}/comparacoes/{pid}_ordem.jpg")
        cont["comparacoes"] += 1

    desc = "; ".join(f"foto {b} vai para posição {a}" for a, b in trocas)
    if DRY_RUN:
        cont["reordenados"] += 1
        w.writerow([pid, nome, marca, "", "", "vitrine_nao_branca", "SERIA_REORDENADO", "", "", "", desc])
        return
    for img, pos in zip(ordem, posicoes):
        api.posicionar_imagem(pid, img["id"], pos)
    cont["reordenados"] += 1
    w.writerow([pid, nome, marca, "", "", "vitrine_nao_branca", "REORDENADO", "", "", "", desc])


def reparar(api, w, cont):
    """Refaz, a partir da foto ORIGINAL guardada no backup, as fotos que o branqueamento
    antigo danificou (produtos creme/claros com manchas). Lê reparar.csv."""
    linhas = list(csv.DictReader(open("reparar.csv", encoding="utf-8")))
    print(f"   {len(linhas)} fotos para reparar")
    prazo = time.time() + PRAZO_MINUTOS * 60
    for ln in linhas:
        if time.time() > prazo:
            print("ATENÇÃO: parou pelo prazo. Rode de novo.")
            break
        pid, atual = int(ln["product_id"]), int(ln["image_id_atual"])
        caminho = os.path.join("artefatos", ln["artefato"], "originais", ln["arquivo_original"])
        base = [pid, "", "", atual, "", "reparo"]
        try:
            original = open(caminho, "rb").read()
            classe, info, novo, im_orig, im_novo = processar_bytes(original)
            if novo is None:          # se não der para branquear com segurança, volta a original
                novo = original
            if cont["comparacoes"] < AMOSTRAS_COMPARACAO and im_novo is not None:
                salvar_comparacao(im_orig, im_novo, f"{OUT_DIR}/comparacoes/{pid}_{atual}_reparo.jpg")
                cont["comparacoes"] += 1
            if DRY_RUN:
                cont["reparadas"] += 1
                w.writerow(base + ["SERIA_REPARADA", "", "", caminho, classe])
                continue
            p = api.produto(pid)
            imagens = sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)
            alvo = next((i for i in imagens if i["id"] == atual), None)
            if alvo is None:
                w.writerow(base + ["nao_encontrada", "", "", caminho, "foto atual não existe mais"])
                continue
            pos = alvo.get("position") or 1
            nova = api.subir_imagem(pid, novo, pos, alt=alvo.get("alt"), nome=f"stivali-{pid}-{pos}.jpg")
            for v in p.get("variants") or []:
                if v.get("image_id") == atual:
                    api.trocar_imagem_variante(pid, v["id"], nova["id"])
            api.apagar_imagem(pid, atual)
            alvo["id"] = nova["id"]
            for img in imagens:
                try:
                    api.posicionar_imagem(pid, img["id"], img.get("position") or 1)
                except Exception:
                    pass
            cont["reparadas"] += 1
            w.writerow(base + ["REPARADA", "", "", caminho, f"nova_image_id={nova['id']}" + (" | " + info["obs"] if info.get("obs") else "")])
        except Exception as e:
            cont["erros"] += 1
            w.writerow(base + ["ERRO", "", "", caminho, str(e)[:200]])


def main():
    if not TOKEN:
        sys.exit("ERRO: defina NUVEMSHOP_ACCESS_TOKEN")

    inicio = time.time()
    prazo = inicio + PRAZO_MINUTOS * 60
    os.makedirs(f"{OUT_DIR}/comparacoes", exist_ok=True)
    os.makedirs(f"{OUT_DIR}/originais", exist_ok=True)

    api = Nuvemshop(TOKEN)
    download = requests.Session()
    download.headers["User-Agent"] = USER_AGENT

    criados_desde = None
    if DIAS_RECENTES > 0:
        criados_desde = (datetime.now(timezone.utc) - timedelta(days=DIAS_RECENTES)).isoformat()

    modo = "TESTE (dry run - nada é alterado)" if DRY_RUN else "REAL (alterando a loja)"
    fotos = "só a capa" if FOTOS_POR_PRODUTO == 1 else (f"{FOTOS_POR_PRODUTO} primeiras" if FOTOS_POR_PRODUTO else "todas")
    print(f"== Padronizador de fundo branco | ação: {ACAO} | modo: {modo} | marca: {MARCA or 'todas'} | fotos: {fotos} ==")

    relatorio = open(f"{OUT_DIR}/relatorio.csv", "w", newline="", encoding="utf-8")
    w = csv.writer(relatorio)
    w.writerow(["product_id", "produto", "marca", "image_id", "posicao", "classificacao",
                "acao", "fundo_rgb", "uniformidade", "src_original", "obs"])

    cont = {"produtos": 0, "imagens": 0, "ja_branco": 0, "corrigidas": 0,
            "reordenados": 0, "ja_ok": 0, "reparadas": 0,
            "nao_padrao": 0, "erros": 0, "comparacoes": 0}
    parou_por_prazo = False

    if ACAO == "reparar":
        reparar(api, w, cont)
        relatorio.close()
        print(f"\n== RESUMO ==\nFotos {'que seriam reparadas' if DRY_RUN else 'reparadas'}: {cont['reparadas']}\nErros: {cont['erros']}")
        return

    for p in api.produtos(criados_desde):  # erros de rede já têm novas tentativas em _req
        if time.time() > prazo:
            parou_por_prazo = True
            break
        marca = marca_produto(p)
        if MARCA and MARCA not in marca.lower():
            continue
        if LIMITE_PRODUTOS and cont["produtos"] >= LIMITE_PRODUTOS:
            break
        cont["produtos"] += 1
        if ACAO == "reordenar":
            try:
                reordenar_produto(api, download, p, w, cont, marca)
            except Exception as e:
                cont["erros"] += 1
                w.writerow([p.get("id"), nome_produto(p), marca, "", "", "erro_produto", "nenhuma",
                            "", "", "", str(e)[:200]])
            relatorio.flush()
            continue
        try:
            pid, nome = p["id"], nome_produto(p)
            todas_imagens = sorted(p.get("images") or [], key=lambda i: i.get("position") or 0)
            imagens = todas_imagens[:FOTOS_POR_PRODUTO] if FOTOS_POR_PRODUTO > 0 else todas_imagens
            variantes = p.get("variants") or []
            mudou = False

            for img in imagens:
                cont["imagens"] += 1
                iid, src, pos = img["id"], img["src"], img.get("position") or 1
                if src.startswith("//"):
                    src = "https:" + src
                base = [pid, nome, marca, iid, pos]
                try:
                    r = download.get(src, timeout=60)
                    r.raise_for_status()
                    classe, info, novo, im_orig, im_novo = processar_bytes(r.content)
                except Exception as e:
                    cont["erros"] += 1
                    w.writerow(base + ["erro_download", "nenhuma", "", "", src, str(e)[:200]])
                    continue

                if classe == "ja_branco":
                    cont["ja_branco"] += 1
                    w.writerow(base + [classe, "nenhuma", info["fundo_rgb"], info["uniformidade"], src, ""])
                    continue
                if classe in ("fundo_nao_padrao", "falhou_verificacao"):
                    cont["nao_padrao"] += 1
                    w.writerow(base + [classe, "revisar_manual", info["fundo_rgb"],
                                       info["uniformidade"], src, info.get("obs", "")])
                    continue

                # classe == corrigir
                if cont["comparacoes"] < AMOSTRAS_COMPARACAO:
                    salvar_comparacao(im_orig, im_novo,
                                      f"{OUT_DIR}/comparacoes/{pid}_{iid}.jpg")
                    cont["comparacoes"] += 1

                if DRY_RUN:
                    cont["corrigidas"] += 1
                    w.writerow(base + ["fundo_cinza", "SERIA_CORRIGIDA", info["fundo_rgb"],
                                       info["uniformidade"], src, info.get("obs", "")])
                    continue

                try:
                    with open(f"{OUT_DIR}/originais/{pid}_{iid}.jpg", "wb") as fh:
                        fh.write(r.content)
                    nova = api.subir_imagem(pid, novo, pos, alt=img.get("alt"),
                                            nome=f"stivali-{pid}-{pos}.jpg")
                    for v in variantes:
                        if v.get("image_id") == iid:
                            api.trocar_imagem_variante(pid, v["id"], nova["id"])
                            v["image_id"] = nova["id"]
                    api.apagar_imagem(pid, iid)
                    img["id"] = nova["id"]
                    mudou = True
                    cont["corrigidas"] += 1
                    w.writerow(base + ["fundo_cinza", "CORRIGIDA", info["fundo_rgb"],
                                       info["uniformidade"], src, f"nova_image_id={nova['id']}" + (" | " + info["obs"] if info.get("obs") else "")])
                except Exception as e:
                    cont["erros"] += 1
                    w.writerow(base + ["fundo_cinza", "ERRO_AO_TROCAR", info["fundo_rgb"],
                                       info["uniformidade"], src, str(e)[:200]])

            # Garante que a ordem das fotos ficou exatamente como antes
            if mudou:
                for img in todas_imagens:
                    try:
                        api.posicionar_imagem(pid, img["id"], img.get("position") or 1)
                    except Exception:
                        pass
        except Exception as e:  # um produto com problema não derruba a execução
            cont["erros"] += 1
            w.writerow([p.get("id"), nome_produto(p), marca, "", "", "erro_produto", "nenhuma",
                        "", "", "", str(e)[:200]])

        relatorio.flush()
        if cont["produtos"] % 50 == 0:
            print(f"  ... {cont['produtos']} produtos | {cont['corrigidas']} fotos "
                  f"{'a corrigir' if DRY_RUN else 'corrigidas'}")

    relatorio.close()
    minutos = (time.time() - inicio) / 60
    print("\n== RESUMO ==")
    print(f"Produtos analisados:           {cont['produtos']}")
    print(f"Fotos analisadas:              {cont['imagens']}")
    print(f"Já estavam brancas:            {cont['ja_branco']}")
    print(f"Fundo cinza {'(seriam corrigidas)' if DRY_RUN else '(corrigidas)'}: {cont['corrigidas']}")
    print(f"Revisar manualmente:           {cont['nao_padrao']}")
    if ACAO == "reordenar":
        print(f"Vitrine já estava branca:      {cont['ja_ok']}")
        print(f"Produtos {'que seriam reordenados' if DRY_RUN else 'reordenados'}: {cont['reordenados']}")
    print(f"Erros:                         {cont['erros']}")
    print(f"Tempo:                         {minutos:.1f} min")
    if parou_por_prazo:
        print("ATENÇÃO: parou pelo prazo. Rode de novo - ele pula o que já está branco.")


if __name__ == "__main__":
    main()
