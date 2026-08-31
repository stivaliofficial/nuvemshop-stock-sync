# Nuvemshop Stock Sync

Script que checa diariamente se produtos de terceiros (Dr. Martens, Timberland etc.)
ainda estao disponiveis no site oficial da marca e, se nao estiverem, desativa o
produto automaticamente na sua loja Nuvemshop.

Como voce nao tem SKU nem link do fornecedor cadastrado, a busca e feita pelo
**titulo do produto**: o script pesquisa esse titulo no site da marca e compara a
similaridade do resultado encontrado. Por isso, revise o log nas primeiras execucoes.

## 1. Gerar o Access Token da Nuvemshop

1. Acesse o [painel de parceiros da Nuvemshop](https://partners.nuvemshop.com.br/) e
   crie um app (tipo "privado"), ou use o app que sua loja ja tiver.
2. Autorize o app para a sua loja - isso gera um `access_token` e o `store_id` (o
   `user_id` retornado no processo de OAuth).
3. Garanta que o app tem permissao de **leitura e escrita de produtos**
   (`read_products`, `write_products`).
4. Guarde o `store_id` e o `access_token` - voce vai usa-los no passo 3.

## 2. Preencher o `config.json`

Abra `config.json` e:

- Em `nuvemshop`, preencha `store_id` e `access_token` (ou deixe em branco e use
  variaveis de ambiente - recomendado para producao, ver passo 3).
- Em `brands`, cada chave e um trecho do titulo do produto (em minusculo) que o
  script usa para reconhecer a marca. Ja vem com Dr. Martens, Timberland e New
  Rock configurados como exemplo - **adicione as demais marcas que voce vende**,
  seguindo o mesmo modelo:
  - `search_url_template`: URL de busca do site da marca, com `{query}` no lugar
    do termo pesquisado.
  - `product_link_selectors`: seletores CSS dos links de produto na pagina de
    busca (inspecione o site da marca com F12 para achar as classes certas).
  - `out_of_stock_keywords` / `buy_button_keywords`: palavras que aparecem na
    pagina do produto quando esta esgotado ou disponivel.
- Em `settings`, mantenha `"dry_run": true` enquanto testa. Nesse modo o script
  mostra no log o que faria, mas nao altera nada na loja.

## 3. Testar localmente (opcional, mas recomendado)

```bash
pip install -r requirements.txt
python nuvemshop_stock_sync.py
```

Verifique o arquivo `stock_sync.log` (e a tela) para ver:
- Quais produtos tiveram a marca reconhecida.
- Qual resultado foi encontrado no site da marca e a similaridade do titulo.
- O que o script decidiu fazer (desativar / manter / nao foi possivel decidir).

So mude `dry_run` para `false` depois de conferir que os resultados fazem sentido.

## 4. Rodar automaticamente todo dia, de graca, via GitHub Actions

Este e o metodo recomendado - roda na nuvem do GitHub, sem precisar deixar seu
computador ligado, e sem custo (dentro do limite gratuito de minutos do GitHub,
que sobra de sobra para 1 execucao diaria).

1. Crie um repositorio no GitHub (pode ser privado) e suba estes arquivos:
   ```
   nuvemshop_stock_sync.py
   config.json
   requirements.txt
   .github/workflows/stock-sync.yml
   ```
2. **Importante:** para nao deixar o `access_token` exposto no repositorio,
   remova o valor real de `config.json` (deixe os placeholders) e cadastre o
   token como *secret* do GitHub:
   - No repositorio: `Settings` -> `Secrets and variables` -> `Actions` ->
     `New repository secret`.
   - Crie `NUVEMSHOP_STORE_ID` com o ID da loja.
   - Crie `NUVEMSHOP_ACCESS_TOKEN` com o token gerado no passo 1.
   - O script ja esta preparado para ler essas variaveis de ambiente e usa-las
     no lugar do que estiver em `config.json`.
3. O workflow em `.github/workflows/stock-sync.yml` ja esta configurado para
   rodar todo dia as 06:00 (horario de Brasilia). Voce pode ajustar o horario
   editando a linha `cron`.
4. Para testar sem esperar o horario agendado: va na aba **Actions** do
   repositorio, escolha o workflow "Nuvemshop Stock Sync" e clique em
   **Run workflow**.
5. Depois de cada execucao, o log fica disponivel para download na propria
   pagina da execucao, em "Artifacts".

### Alternativa: PythonAnywhere

Se preferir nao usar GitHub Actions:

1. Crie uma conta gratuita em [pythonanywhere.com](https://www.pythonanywhere.com).
2. Va em **Files** e faca upload de `nuvemshop_stock_sync.py`, `config.json` e
   `requirements.txt` (ou clone seu repositorio via `git clone` no console Bash).
3. No console Bash do PythonAnywhere: `pip install --user -r requirements.txt`.
4. Va em **Tasks** (disponivel no plano gratuito com 1 tarefa agendada por dia)
   e crie uma tarefa diaria com o comando:
   ```
   python3 /home/SEU_USUARIO/nuvemshop-stock-sync/nuvemshop_stock_sync.py
   ```
5. Defina as variaveis `NUVEMSHOP_STORE_ID` e `NUVEMSHOP_ACCESS_TOKEN` na secao
   de variaveis de ambiente da conta, ou preencha direto no `config.json`
   (menos seguro, mas funciona, ja que o arquivo fica so na sua conta).

## Limitacoes a ter em mente

- A correspondencia por titulo e uma **heuristica**. Marcas com nomes de
  produto muito genericos (ex: "Bota Preta") podem gerar falsos positivos ou
  negativos - ajuste `min_title_similarity` no config.json se notar isso.
- Sites de marca mudam de layout com frequencia. Quando isso acontece, o
  script tenta um fallback generico de busca, mas o ideal e revisar e
  atualizar os `product_link_selectors` daquela marca de tempos em tempos.
- Por seguranca, o script **so reativa produtos automaticamente se voce
  ligar** `auto_reenable_if_back_in_stock` no config.json. Por padrao ele so
  desativa - reativar fica a seu criterio manual, para evitar publicar de
  volta um item por engano com base em uma leitura errada do site da marca.
- Respeite os termos de uso do site de cada marca ao fazer scraping regular;
  o delay entre requisicoes no config.json ajuda a manter uma frequencia
  razoavel.
