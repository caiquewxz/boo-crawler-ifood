# boo-crawler-ifood

Crawler do iFood para mapeamento completo de restaurantes por grade de coordenadas — suporta múltiplas cidades brasileiras.

Dois modos de operação:

- **`crawl_api.py`** (recomendado) — browser sobe uma única vez para capturar a sessão; todos os requests `bm/home` são feitos diretamente via HTTP. ~2–5s por ponto, coleta ~400 restaurantes por ponto com paginação completa.
- **`crawl.py`** — browser navega ponto a ponto (mais lento, ~60s/ponto), útil quando `crawl_api.py` falha por bloqueio de IP.

Após coletar os merchants, use `crawl_catalog.py` para buscar o cardápio completo de cada restaurante.

---

## Stack

| Biblioteca | Uso |
|------------|-----|
| **[nodriver](https://github.com/ultrafunkamsterdam/nodriver)** | Controle do Chrome via CDP puro, sem protocolo WebDriver |
| **httpx** | Requests diretos à API (crawl_api.py e crawl_catalog.py) |
| **Python 3.10+** | Scripts de login, crawlers e extração |
| **Google Chrome** | Browser real com perfil persistente |

---

## Pré-requisitos

| Requisito | Versão mínima |
|-----------|--------------|
| Python | 3.10 |
| Google Chrome | qualquer versão recente |

---

## Instalação

```bash
pip install -r requirements.txt
```

---

## Uso

### 1. Login (primeira vez ou quando o token expirar)

```powershell
python scripts\login.py
```

O browser abre na tela de login do iFood. Faça login normalmente e, quando estiver na tela inicial com os restaurantes, pressione **Enter** no terminal. A sessão é salva em `.chrome-profile/`.

O token de acesso (`aAccessToken`) expira em algumas horas. Quando o crawler reportar sessão inválida, repita este passo.

---

### 2a. Crawl via API direta (recomendado)

```powershell
python scripts\crawl_api.py
```

Browser sobe uma vez, captura a sessão e fecha. Todos os requests `bm/home` são feitos via httpx com paginação automática — percorre todos os cards `NEXT_CONTENT` até esgotar os resultados.

Parâmetros:

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `--city` | `sao-paulo` | Cidade (use `--list-cities` para ver todas) |
| `--step` | padrão da cidade | Espaçamento da grade em km |
| `--delay` | `2.0` | Pausa base entre requests (segundos) |
| `--headless` | off | Captura de sessão sem janela |
| `--max-points` | `0` | Limitar pontos (0 = todos) |
| `--page-size` | `100` | Restaurantes por request |
| `--boundary` | off | Filtrar grade pelo polígono real do município via OSM |
| `--proxy` | — | Proxy HTTP/SOCKS5 |

Exemplos:

```powershell
# Listar cidades disponíveis
python scripts\crawl_api.py --list-cities

# São Paulo (step padrão 5km, ~150 pontos)
python scripts\crawl_api.py --city sao-paulo

# Cidade menor
python scripts\crawl_api.py --city sao-joao-del-rei

# Teste rápido com 3 pontos
python scripts\crawl_api.py --max-points 3 --delay 1

# Com boundary OSM (filtra pontos fora do município)
python scripts\crawl_api.py --city sao-paulo --boundary
```

---

### 2b. Crawl via browser (fallback)

```powershell
python scripts\crawl.py
```

Parâmetros:

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `--city` | `sao-paulo` | Cidade |
| `--step` | padrão da cidade | Espaçamento da grade em km |
| `--delay` | `60.0` | Pausa base entre navegações (segundos) |
| `--headless` | off | Rodar sem janela |
| `--max-points` | `0` | Limitar pontos |
| `--page-size` | `100` | Restaurantes por ponto |
| `--boundary` | off | Filtrar pelo polígono do município |

---

### 3. Catálogos (cardápios)

```powershell
# A partir de uma captura existente do crawl_api
python scripts\crawl_catalog.py --capture-dir captures\crawl_api_sao-joao-del-rei_20260522_120000

# Ou passando arquivos merchants.jsonl diretamente
python scripts\crawl_catalog.py captures\crawl_api_*\merchants.jsonl

# Com opções
python scripts\crawl_catalog.py --capture-dir captures\... --delay 2 --max 50 --headless
```

Parâmetros:

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `--capture-dir` | — | Diretório de captura do crawl_api |
| `--delay` | `2.0` | Pausa base entre requests (segundos) |
| `--max` | `0` | Máximo de lojas (0 = todas) |
| `--headless` | off | Captura de sessão sem janela |
| `--proxy` | — | Proxy HTTP/SOCKS5 |

---

## Saída

### `crawl_api.py` → `captures/crawl_api_CIDADE_TIMESTAMP/`

#### `merchants.jsonl`

Um restaurante por linha, deduplicado por UUID.

```json
{"id": "11ff40c8-b204-45d4-a74a-1a13f42f9982", "name": "Julios Sushi", "link": "https://www.ifood.com.br/delivery/caieiras-sp/julios-sushi-laranjeiras", "category": "Japonesa", "rating": 4.8, "distance_km": 1.2, "delivery_fee": 599, "delivery_min_min": 30, "delivery_max_min": 45, "lat": -23.357, "lon": -46.826}
```

| Campo | Descrição |
|-------|-----------|
| `id` | UUID único do restaurante |
| `name` | Nome |
| `link` | URL direta no iFood |
| `category` | Categoria principal |
| `rating` | Nota de 0 a 5 |
| `delivery_fee` | Taxa de entrega em centavos |
| `delivery_min_min` / `delivery_max_min` | Janela de entrega estimada (minutos) |
| `lat` / `lon` | Coordenada do ponto da grade onde foi encontrado |

#### `points.jsonl`

Um JSON por linha com resultado de cada ponto da grade (contagem, merchants encontrados, erro).

#### `crawl.log`

Log de progresso:

```
[  1/36] (-21.0700,-44.3400) novos=73  total=73
  -> Burguer King, McDonald's, Subway... (+70)
[  2/36] (-21.0700,-44.3179) novos=18  total=91
  -> Pizza Hut, Domino's... (+16)
```

---

### `crawl_catalog.py` → dentro do mesmo `capture-dir`

Para cada restaurante, cria uma pasta `{nome}-{uuid}/` com:

- `catalog.json` — resposta bruta da API de catálogo + metadados
- `products.jsonl` — um produto por linha com preço em centavos e BRL

---

## Estrutura do projeto

```
boo-crawler-ifood/
├── configs/
│   ├── cities.py           # Bounding boxes e step padrão de 11 cidades
│   └── boundary.py         # Filtro por polígono real do município via OSM
├── scripts/
│   ├── login.py            # Login manual + salva sessão Chrome
│   ├── crawl_api.py        # Crawler principal: API direta + paginação completa
│   ├── crawl.py            # Crawler browser (fallback)
│   └── crawl_catalog.py    # Busca catálogos de cada restaurante via API
├── captures/               # Saída dos crawls (gitignored)
├── CHANGELOG.md
├── JOURNAL.md              # Diário de desenvolvimento com todas as tentativas
└── requirements.txt
```

---

## Como funciona

### Paginação (`crawl_api.py`)

A API `bm/home` retorna apenas os restaurantes "destaque" na primeira resposta. Os demais ficam em páginas adicionais acessadas via um cursor que vem num card separado de tipo `NEXT_CONTENT`:

```
section.cards[0] = MERCHANT_LIST_V2   ← merchants desta página
section.cards[1] = NEXT_CONTENT       ← action: "card-content?cursor=<TOKEN>"
```

`fetch_all_pages()` segue esse cursor automaticamente até não haver mais resultados, coletando ~400 restaurantes por ponto vs. ~73 sem paginação.

### Bot detection

| Camada | Sistema | Solução |
|--------|---------|---------|
| Browser fingerprint | Akamai | nodriver (CDP puro, sem `navigator.webdriver`) |
| Challenge iframe | Akamai | nodriver bypassa sem challenge |
| Enforcement server-side | HUMAN Security (PerimeterX) | Cookies `_px3`/`_pxhd`/`_pxvid` capturados via CDP e enviados no header `x-px-cookies` em todos os requests httpx |
| Sensor JS | HUMAN Security | STEALTH_SCRIPT patcha `Element.prototype.getAttribute`, artefatos ChromeDriver, `window.external` e `Function.prototype.toString` |

### Captura de sessão

Browser (nodriver) sobe uma vez, navega para `/restaurantes`, captura via CDP:
- JWT `aAccessToken` (cookies iFood)
- Cookies PX `_px3`, `_pxhd`, `_pxvid` (HUMAN Security)
- URL template do endpoint `bm/home`
- `User-Agent` real do Chrome

Depois fecha o browser. Todos os requests seguintes são httpx puro, sem browser. Sessão renovada automaticamente após 45 min ou 3 erros 401/403 consecutivos.

---

## Cidades disponíveis

| Chave | Cidade | Step padrão | Pontos aprox. |
|-------|--------|-------------|---------------|
| `sao-paulo` | São Paulo - SP | 5.0 km | ~150 |
| `rio-de-janeiro` | Rio de Janeiro - RJ | 5.0 km | ~130 |
| `belo-horizonte` | Belo Horizonte - MG | 4.0 km | ~100 |
| `curitiba` | Curitiba - PR | 4.0 km | ~90 |
| `porto-alegre` | Porto Alegre - RS | 4.0 km | ~100 |
| `fortaleza` | Fortaleza - CE | 4.0 km | ~80 |
| `salvador` | Salvador - BA | 4.0 km | ~80 |
| `brasilia` | Brasília - DF | 5.0 km | ~120 |
| `manaus` | Manaus - AM | 5.0 km | ~80 |
| `recife` | Recife - PE | 3.0 km | ~80 |
| `sao-joao-del-rei` | São João del Rei - MG | 2.0 km | ~36 |
| `ribeirao-preto` | Ribeirão Preto - SP | 3.0 km | ~64 |

---

## Notas

- O Chrome usa um perfil dedicado (`.chrome-profile/`). Suas janelas Chrome normais não são afetadas.
- O crawler encerra automaticamente qualquer instância anterior do Chrome com o mesmo perfil antes de lançar uma nova.
- O `aAccessToken` expira em algumas horas. Execute `login.py` quando o crawler reportar sessão inválida.
