# boo-crawler-ifood

Crawler web do iFood para mapeamento completo de restaurantes por grade de coordenadas — suporta múltiplas cidades brasileiras.

Controla o Chrome diretamente via CDP (sem WebDriver), contornando o Akamai Bot Manager e extraindo nome, link e metadados de cada restaurante.

---

## Stack

- **[nodriver](https://github.com/ultrafunkamsterdam/nodriver)** — controle do Chrome via CDP puro, sem protocolo WebDriver
- **Python 3.10+** — scripts de login, crawler e extração
- **Google Chrome** — browser real com perfil persistente (evita re-login)

---

## Pré-requisitos

| Requisito | Versão mínima |
|-----------|--------------|
| Python | 3.10 |
| Google Chrome | qualquer versão recente |
| pip | — |

---

## Instalação

```bash
pip install -r requirements.txt
playwright install chromium   # para check_fingerprint.py (opcional)
```

---

## Uso

### 1. Login (primeira vez ou quando o token expirar)

```powershell
python scripts\login.py
```

O browser abre na tela de login do iFood. Faça login normalmente, e quando estiver na tela inicial com os restaurantes pressione **Enter** no terminal. A sessão é salva em `.chrome-profile/` e os cookies em `configs/session.json`.

O token de acesso (`aAccessToken`) expira em algumas horas. Quando o crawler reportar sessão inválida, repita este passo.

### 2. Crawl

```powershell
python scripts\crawl.py
```

Parâmetros opcionais:

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `--city` | `sao-paulo` | Cidade a crawlear (use `--list-cities` para ver todas) |
| `--step` | padrão da cidade | Espaçamento da grade em km |
| `--delay` | `60.0` | Pausa base entre pontos (segundos) |
| `--headless` | off | Rodar sem janela do browser |
| `--max-points` | `0` | Limitar pontos (0 = todos). Útil para testes |

Exemplos:

```powershell
# Listar cidades disponíveis
python scripts\crawl.py --list-cities

# SP com ~150 pontos (step padrão 5km)
python scripts\crawl.py --city sao-paulo

# Outra cidade
python scripts\crawl.py --city rio-de-janeiro

# Teste rápido com 3 pontos
python scripts\crawl.py --max-points 3 --delay 15

# Grade densa (3km, mais pontos, demora mais)
python scripts\crawl.py --city sao-paulo --step 3 --delay 60 --headless
```

---

## Saída

Cada execução cria um diretório em `captures/crawl_nd_CIDADE_TIMESTAMP/` com três arquivos:

### `merchants.jsonl`

Um restaurante por linha, deduplicado por UUID. Este é o arquivo principal de resultado.

```json
{"id": "11ff40c8-b204-45d4-a74a-1a13f42f9982", "name": "Julios Sushi", "link": "https://www.ifood.com.br/delivery/caieiras-sp/julios-sushi-laranjeiras", "category": "Japonesa", "rating": 4.8, "lat": -23.357, "lon": -46.826}
```

| Campo | Descrição |
|-------|-----------|
| `id` | UUID único do restaurante no iFood |
| `name` | Nome completo |
| `link` | URL direta no iFood |
| `category` | Categoria principal (Pizza, Lanches, etc.) |
| `rating` | Nota de 0 a 5 |
| `lat` / `lon` | Coordenada do ponto da grade onde foi encontrado |

### `requests.jsonl`

Um JSON por linha com o request+response completo de cada ponto da grade. Útil para debug.

### `crawl.log`

Log de progresso com contagem de merchants novos e totais por ponto.

```
[  1/56] (-23.3570,-46.8260) status=200 novos=20 total=20
  -> Julios Sushi, Hamburgueria Johnny Smash Burger Caieiras, Bar do Pay... (+17)
[  2/56] (-23.3570,-46.7475) status=200 novos=8 total=28
  -> Sabor da Parmegiana, Recanto do Pastel... (+6)
```

---

## Estrutura do projeto

```
boo-crawler-ifood/
├── configs/
│   ├── cities.py           # Bounding boxes e step padrão de 10 cidades brasileiras
│   └── sp_grid.py          # Gerador legado (substituído por cities.py)
├── scripts/
│   ├── login.py            # Login manual + salva sessão Chrome
│   └── crawl.py            # Crawler principal (nodriver + CDP, multi-cidade)
├── captures/               # Saída dos crawls (gitignored)
├── requirements.txt
└── JOURNAL.md              # Diário de desenvolvimento com todas as tentativas
```

---

## Como funciona

O crawler opera em três camadas de captura para garantir que a resposta da API seja obtida independentemente de como o iFood a sirva:

1. **CDP Network events** — escuta `LoadingFinished` para capturar o body da resposta diretamente do buffer do Chrome, sem interceptar nem modificar requests
2. **JS fetch interceptor** — patch de `window.fetch` injetado antes de qualquer script do site via `addScriptToEvaluateOnNewDocument`
3. **Fetch manual** — como último recurso, executa um POST manual ao endpoint com os headers/cookies da sessão atual

O bot detection do Akamai é contornado por:

- nodriver comunica com Chrome via CDP puro — sem `navigator.webdriver`, sem protocolo WebDriver
- Perfil Chrome persistente acumula cookies e histórico como usuário real
- Stealth script corrige fingerprint de `Navigator.prototype`, WebGL, Canvas, Permissions e `window.chrome`
- Movimento de mouse via curva Bézier cúbica com easing antes de cada ponto
- Navegação natural em `/restaurantes` para acumular sinais comportamentais positivos no sensor Akamai

---

## Configuração da grade

A grade de SP é definida em `configs/sp_grid.py`. Os limites padrão cobrem o município de São Paulo. Para ajustar:

```python
SP_LAT_MIN = -24.008
SP_LAT_MAX = -23.357
SP_LON_MIN = -46.826
SP_LON_MAX = -46.365
```

Com `--step 8` a grade tem ~56 pontos. Com `--step 2` tem ~900 pontos e cobre praticamente todos os restaurantes do município.

---

## Notas

- O Chrome lançado pelo crawler usa um perfil dedicado (`.chrome-profile/`). Suas janelas Chrome normais não são afetadas.
- O crawler detecta automaticamente se há uma instância anterior do Chrome com o mesmo perfil e a encerra antes de lançar uma nova.
- O `aAccessToken` do iFood expira em algumas horas. O crawler avisa quando a sessão estiver inválida.
