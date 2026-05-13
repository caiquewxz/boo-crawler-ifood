# ifood-intercept

Interceptação e crawling do iFood para mapeamento de todas as lojas de SP.
Adapatado da mesma stack usada em `../99-intercept`.

---

## Stack

- **mitmproxy** — interceptação HTTPS
- **Frida** — bypass de SSL pinning no app Android
- **Python** — parse de capturas e crawler automático

---

## Pré-requisitos

Mesmos do 99-intercept (já configurados):
- `mitmdump` instalado e no PATH
- `frida-tools` instalado (`pip install frida-tools`)
- `adb` disponível (platform-tools)
- `httpx` para o crawler (`pip install httpx`)
- Dispositivo com KernelSU + LSPosed
- LSPosed: XposedFakeLocation habilitado para `br.com.brainweb.ifood`

---

## Fluxo de trabalho

### 1. Configurar device

No LSPosed, habilitar o XposedFakeLocation para `br.com.brainweb.ifood`
com qualquer coordenada de SP (ex: `-23.5505, -46.6333` — Sé).

### 2. Subir Frida

```powershell
cd ifood-intercept
.\scripts\frida_setup.ps1
```

### 3. Capturar sessão de referência

Em um terminal:
```powershell
.\scripts\capture.ps1
```

Configure o proxy WiFi do dispositivo para `<IP_PC>:8080`.

Em outro terminal, inicie o iFood com bypass de pinning:
```
frida -U -f br.com.brainweb.ifood -l hooks\ssl_bypass.js --no-pause
```

Navegue no app: abra a tela principal, deixe carregar os restaurantes.
Quando terminar, Ctrl+C no mitmdump. O `.mitm` fica em `captures/`.

### 4. Inspecionar a captura

```
cd ifood-intercept
python scripts\scan_sessions.py
python scripts\export_mitm_txt.py
```

**Importante:** Identifique qual endpoint é o real vs. fallback.
Ajuste `FALLBACK_SIGNALS` em `scripts/crawl_sp.py` antes de crawlar.

### 5. Crawl de SP

```
python scripts\crawl_sp.py --mitm captures\session_XXXXXXXX.mitm --step 2.0 --delay 1.5
```

Parâmetros:
- `--step`: espaçamento da grade em km (2.0 = ~1100 pontos em SP)
- `--delay`: pausa entre requests para evitar rate limiting

Resultados em `captures/crawl_YYYYMMDD_HHMMSS/`:
- `requests.jsonl` — um JSON por linha com request+response completos
- `crawl.log` — progresso e contagem de merchants únicos

---

## Estrutura

```
ifood-intercept/
├── captures/          # .mitm de sessões e output do crawler
├── configs/
│   └── sp_grid.py     # Gerador da grade de coordenadas de SP
├── hooks/
│   └── ssl_bypass.js  # Bypass universal de SSL pinning (Frida)
└── scripts/
    ├── capture.ps1        # Inicia mitmproxy
    ├── frida_setup.ps1    # Deploy do frida-server no device
    ├── scan_sessions.py   # Lista endpoints iFood nas capturas
    ├── export_mitm_txt.py # Exporta .mitm para texto legível
    └── crawl_sp.py        # Crawler principal (grade de SP)
```

---

## Pontos de atenção (pré-execução)

1. **Fallback:** Após o primeiro `scan_sessions.py`, confirme qual URL é o
   endpoint real e ajuste `FALLBACK_SIGNALS` no crawler.
2. **Token expiration:** Os tokens de sessão expiram. Se o crawler rodar por
   horas, pode ser necessário re-capturar e atualizar os headers.
3. **Rate limiting:** O delay padrão de 1.5s é conservador. Reduza com cuidado.
4. **Parâmetros de coordenada:** Confirme os nomes dos params de lat/lon
   no endpoint real e ajuste `LAT_PARAMS`/`LON_PARAMS` no crawler.
