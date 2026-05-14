# Diário de Desenvolvimento — Crawler iFood Web

Registro cronológico de cada tentativa, problema encontrado e solução aplicada
durante o desenvolvimento do crawler web do iFood para coleta de merchants por grade de SP.

---

## Tentativa 1 — Playwright básico (`8a3dc7b`)

**O que foi feito:**
- Crawler inicial com Playwright em Python usando `chromium.launch()`
- `page.route("**/site-api/**")` para interceptar e capturar chamadas da API de restaurantes
- Sem nenhum tratamento de bot detection

**Problema:**
- O iFood usa Akamai Bot Manager. O Playwright, por padrão, injeta o flag `--enable-automation` no Chrome e expõe `navigator.webdriver = true`
- O Akamai detectava imediatamente e redirecionava para um desafio de "pressione e segure o botão"
- Não havia nenhuma automação do desafio — precisava ser resolvido manualmente toda vez

---

## Tentativa 2 — camoufox / Firefox (`ab3a80d → cb22869`)

**O que foi feito:**
- Migração para `camoufox`, um fork do Firefox com patches anti-detecção
- A ideia era que Firefox teria fingerprint diferente do Chrome automatizado

**Problemas:**
1. `TypeError: BrowserType.launch() got an unexpected keyword argument 'timezone'` — o parâmetro `timezone_id` precisava ir no `new_context()`, não no launch
2. Após corrigir o timezone, o iFood retornou **"Access Denied / You don't have permission"** — bloqueio total
3. O fingerprint do Firefox headless com camoufox foi reconhecido pelo Akamai como bot

**Conclusão:** O Akamai estava configurado para bloquear Firefox especificamente nesse endpoint. Abandonada.

---

## Tentativa 3 — CDP direto com Chrome real (`ad1ffe1`)

**O que foi feito:**
- Tentativa de conectar ao Chrome via CDP (Chrome DevTools Protocol) sem Playwright
- Uso do binário real do Chrome para ter fingerprint mais limpo

**Problema:**
- A implementação era complexa e tinha problemas de sessão
- Decidido restaurar o Playwright com melhorias em cima

---

## Tentativa 4 — Restaura Playwright + handler de challenge (`64b5d1c → 887daf4`)

**O que foi feito:**
- Restaurado o crawler original do Playwright (`git reset` para `8a3dc7b`)
- Adicionado `handle_challenge()` com tentativa de automação do botão "pressione e segure"
- Tentativa de encontrar o botão dentro do iframe do Akamai via `page.frames` e seletores CSS

**Problemas:**
1. O desafio ocorre dentro de um `<iframe src="https://client.wra-api.net/...">` — iframe cross-origin
2. `page.locator()` do Playwright não entra em iframes cross-origin por padrão
3. Iterando `page.frames`, encontrava o frame correto (`frame[2]`) mas não conseguia interagir com os elementos dentro dele (bloqueado por CORS/cross-origin)

**Debug relevante:**
```
[DEBUG] 5 frame(s) encontrado(s):
[0] https://www.ifood.com.br/restaurantes
[1] about:blank
[2] https://client.wra-api.net/HYU232/iframe.html#...
[3] about:blank
[4] about:blank
```

---

## Tentativa 5 — Bounding box do iframe (`4d2d1c0`)

**Insight chave:** Em vez de tentar entrar no iframe cross-origin para clicar no botão, pegar as **coordenadas do próprio `<iframe>`** na página principal e simular o mouse diretamente lá.

**O que foi feito:**
```python
box = await page.locator('iframe[src*="wra-api"]').bounding_box()
cx = box['x'] + box['width'] / 2
cy = box['y'] + box['height'] / 2
await page.mouse.down()  # hold no centro do iframe
```

**Resultado:** A mecânica do mouse funcionou — o movimento e o hold eram fisicamente executados sobre o iframe do Akamai. O challenge às vezes era resolvido, mas a detecção do challenge ainda era inconsistente.

---

## Tentativa 6 — Remoção do banner de automação (`0956ab7`)

**Problema identificado:** O Chrome ainda exibia "Este navegador está sendo controlado por um software de teste automatizado" — um sinal óbvio de automação.

**O que foi feito:**
```python
context = await p.chromium.launch_persistent_context(
    ...
    ignore_default_args=['--enable-automation'],  # remove o flag do Playwright
)
```

**Resultado:** Banner removido. O Akamai ainda detectava, mas eliminamos esse sinal visual e técnico.

---

## Tentativa 7 — Análise de fingerprint com sannysoft (`e193f55 → 083d43e`)

**O que foi feito:**
- Criado script `check_fingerprint.py` que abre `bot.sannysoft.com` e extrai os resultados como texto
- Objetivo: identificar quais sinais de automação ainda estavam expostos

**Resultados do primeiro scan:**
```
WebDriver (New) | present (FAILED)
Plugins is of type PluginArray | FAILED
```

**Causa:**
1. `WebDriver`: O patch estava em `navigator` (instância) em vez de `Navigator.prototype` — os testes "New" do sannysoft checam no prototype
2. `PluginArray`: O código retornava um `Array` simples em vez de um objeto que herda de `PluginArray`

---

## Tentativa 8 — Correção do fingerprint (`50c8d80`)

**O que foi feito:**
```javascript
// ANTES (errado — instância)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined })

// DEPOIS (correto — prototype)
Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined })

// PluginArray real
const fakePluginArray = Object.create(PluginArray.prototype)
pluginData.forEach((p, i) => {
    const plugin = Object.create(Plugin.prototype)
    // ...
    fakePluginArray[i] = plugin
})
```

**Resultado do sannysoft após fix:**
```
WebDriver (New) | missing (PASSED)
Plugins is of type PluginArray | PASSED
```

Todos os checks do sannysoft passando. O challenge do Akamai continuava aparecendo — indicando que o problema restante era **comportamental**, não de fingerprint JS.

---

## Tentativa 9 — Detecção assíncrona do challenge (`e6d9744`)

**Problema identificado:** O `handle_challenge()` era chamado uma única vez logo após o `page.goto()`. Porém o Akamai injeta o iframe do desafio **de forma assíncrona** — depois que o DOM já carregou. O check acontecia cedo demais e nunca encontrava o iframe.

**O que foi feito:**
```python
challenge_flag = asyncio.Event()

def on_frame_navigated(frame):
    if 'wra-api' in frame.url:
        challenge_flag.set()

page.on('framenavigated', on_frame_navigated)

# No loop de espera:
while not done.is_set():
    if challenge_flag.is_set():
        challenge_flag.clear()
        await handle_challenge(page)
    await asyncio.sleep(0.5)
```

**Resultado:** A detecção passou a ser event-driven — assim que o frame do `wra-api` navegava, o flag era acionado.

---

## Tentativa 10 — Navegação natural antes do crawl (`986eb47`)

**Observação:** O challenge **não aparecia em `/inicio`** mas aparecia em `/restaurantes`. Sinal de detecção comportamental específica da página de listagem.

**O que foi feito:**
- Adicionada função `natural_browse()` que visita `/inicio` antes de cada ponto do crawl
- Simula scroll gradual, movimento de mouse, hover em cards de restaurante
- Acumula sinais comportamentais legítimos no sensor do Akamai antes de ir para `/restaurantes`

```python
async def natural_browse(page):
    await page.goto(INICIO_URL)
    # scroll, mouse moves, hover em cards...
    await page.goto(HOME_URL, referer=INICIO_URL)  # navega "de dentro do site"
```

**Resultado:** Reduziu a frequência do challenge, mas não eliminou.

---

## Tentativa 11 — Remoção do `page.route()` (`f880cad`)

**Root cause identificado:** O `page.route()` do Playwright **pausa cada request** que corresponde ao padrão até que o Python chame `route.continue_()`. Isso cria uma **latência artificial consistente em todos os requests `site-api`** — exatamente o tipo de padrão que o Bot Manager usa para identificar automação.

Timeline do problema:
```
Request site-api → Playwright intercepta → pausa → Python processa → continue()
                   ↑ latência extra de ~5-20ms em TODO request
```

**O que foi feito:**
- Removido completamente o `page.route()`
- Substituído por `page.on('request', ...)` — apenas observa, sem pausar nada
- Coordenadas de localização passaram a vir 100% dos cookies pre-setados

**Resultado:** Significativa redução na detecção. O challenge passou a ser muito menos frequente.

---

## Tentativa 12 — Migração para nodriver (`22ba94f`)

**Motivação:** nodriver é o sucessor do `undetected-chromedriver`, controla o Chrome diretamente via CDP sem nenhum marcador do protocolo WebDriver. Nenhum `navigator.webdriver`, sem IPC do WebDriver, sem latência de automação.

**O que foi feito:**
- Criado `crawl_sp_web_nd.py` usando `nodriver`
- Browser iniciado com `uc.start(user_data_dir=CHROME_PROFILE)` — mesmo perfil persistente
- Stealth patches via `Page.addScriptToEvaluateOnNewDocument`
- Captura via injeção de interceptor `fetch`/`XHR` em `window.__ifood_captured`

**Resultado parcial:**
- ✅ Challenge do Akamai **não apareceu mais** — nodriver bypassa completamente
- ❌ Endpoint não capturado — `window.__ifood_captured` sempre vazio

**Causa:** O `addScriptToEvaluateOnNewDocument` não garantia que o interceptor de `fetch`/`XHR` rodasse antes das chamadas de API do iFood. O bundle do iFood pode capturar `window.fetch` durante a inicialização do módulo de forma que o patch chegava tarde.

---

## Tentativa 13 — CDP Network events (`01c3f6a`) — estado atual

**O que foi feito:**
- Abandonada a abordagem de JS injection para captura
- Substituída por eventos CDP de rede: `Network.RequestWillBeSent` + `Network.ResponseReceived` + `Network.getResponseBody`
- Handlers registrados **antes** do `tab.get(HOME_URL)` para não perder respostas durante o carregamento
- `get_response_body` chamado imediatamente no handler de resposta (Chrome libera o buffer rapidamente)

```python
def on_request(event: cdp.network.RequestWillBeSent):
    if active and 'site-api' in (event.request.url or ''):
        pending[event.request_id] = event.request.url

async def on_response(event: cdp.network.ResponseReceived):
    url = pending.pop(event.request_id, None)
    result = await tab.send(cdp.network.get_response_body(event.request_id))
    body = json.loads(result.body)
    if is_restaurant_listing(url, json.dumps(body)):
        captured[...] = ...
        done.set()
```

**Vantagem sobre JS injection:** Captura qualquer request independente de como o iFood o faz — `fetch`, `XHR`, service worker, módulo bundled — tudo passa pelo CDP.

**Status:** Challenge não aparece ✅ — Captura de endpoint ainda não confirmada ❌

---

## Resumo dos sinais de automação identificados e tratados

| Sinal | Status | Solução |
|---|---|---|
| `navigator.webdriver = true` | ✅ Resolvido | Patch em `Navigator.prototype` |
| Banner "controlado por software de automação" | ✅ Resolvido | `ignore_default_args=['--enable-automation']` |
| `plugins` não é `PluginArray` real | ✅ Resolvido | `Object.create(PluginArray.prototype)` |
| Marcadores `cdc_*` do Chrome DevTools Client | ✅ Resolvido | Loop de delete no init script |
| Latência artificial em requests (page.route) | ✅ Resolvido | Substituído por `page.on('request')` |
| Fingerprint Firefox (camoufox) | ✅ Descartado | Voltou para Chrome real |
| Challenge Akamai WRA (wra-api.net) | ✅ Resolvido com nodriver | nodriver bypassa sem challenge |
| Captura da resposta da API | ❌ Em andamento | Tentando CDP Network events |

---

## Arquitetura atual

```
login.py                  →  login manual com Playwright (salva .chrome-profile)
crawl_sp_web_nd.py        →  crawler principal (nodriver + CDP)
  ├── uc.start()          →  Chrome real via CDP, sem WebDriver
  ├── STEALTH_SCRIPT      →  patches de fingerprint via addScriptToEvaluateOnNewDocument
  ├── set_location_cookies→  define lat/lon via CDP Network.setCookie
  ├── natural_browse()    →  visita /inicio como usuário real antes do crawl
  ├── crawl_point()       →  navega /restaurantes, captura via CDP Network events
  └── handle_challenge()  →  detecta e tenta resolver challenge automaticamente
check_fingerprint.py      →  diagnóstico: abre bot.sannysoft.com e extrai resultados
configs/sp_grid.py        →  grade de coordenadas de SP
```

---

## Lições aprendidas

1. **Fingerprint JS ≠ detecção comportamental.** Passar 100% no sannysoft não garante passar pelo Akamai — o Bot Manager tem camadas: fingerprint, TLS, comportamento, reputação de IP
2. **`page.route()` é detectável.** A latência que ele adiciona em cada request é um padrão estatisticamente identificável
3. **Firefox é mais bloqueado que Chrome** no iFood — provavelmente regra específica do Akamai para esse endpoint
4. **Patch na instância vs prototype** — testes "New" de bot detection checam o prototype, não a instância
5. **Challenge é assíncrono** — o iframe do Akamai é injetado depois do DOM carregar, não durante
6. **nodriver bypassa o challenge** por operar em um nível mais baixo (CDP puro, sem protocolo WebDriver)
7. **JS injection para captura é frágil** — bundlers e service workers podem capturar `window.fetch` antes do patch rodar
