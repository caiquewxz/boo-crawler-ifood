"""
Crawler iFood Web — multi-cidade via nodriver (CDP nativo, sem WebDriver).

Metodologia de bypass de bot:
  - nodriver comunica com Chrome diretamente via CDP sem ChromeDriver
  - navigator.webdriver ausente por design (nodriver nao injeta WebDriver)
  - STEALTH_SCRIPT via Page.addScriptToEvaluateOnNewDocument antes de
    qualquer JS do site: cobre webdriver, cdc_, WebGL, Canvas, Permissions,
    screen, connection, battery, chrome runtime
  - Movimento de mouse via curva Bezier cubica + easing smoothstep
  - Perfil persistente acumula cookies/historico como usuario real
  - Captura de API via CDP Network events (sem interceptar nem modificar)

Uso:
    pip install nodriver
    python scripts/login.py
    python scripts/crawl.py [--city sao-paulo] [--step 5.0] [--delay 60.0] [--headless]
    python scripts/crawl.py --list-cities

Saida em captures/crawl_nd_CIDADE_TIMESTAMP/:
    requests.jsonl  — um JSON por linha com request + response
    merchants.jsonl — merchants unicos encontrados
    crawl.log       — progresso e contagem de merchants unicos
"""

import asyncio
import argparse
import base64
import json
import math
import random
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import nodriver as uc
from nodriver import cdp

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.cities import CITIES, generate_city_grid

HOME_URL       = 'https://www.ifood.com.br/restaurantes'
IFOOD_HOST     = 'www.ifood.com.br'
CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
_PID_FILE      = CHROME_PROFILE / '.crawler_pid'

# Intercepta o fetch() natural do iFood para capturar method + headers + body
# do request bm/home, que e um POST com Authorization JWT.
# Injetado via addScriptToEvaluateOnNewDocument — roda antes do JS do site.
CAPTURE_SCRIPT = r"""
(function() {
    if (window.__ifCapInstalled) return;
    window.__ifCapInstalled = true;
    window.__ifCapResult = null;
    const _origFetch = window.fetch;
    window.fetch = function() {
        const args = Array.from(arguments);
        let url = '';
        if (typeof args[0] === 'string') url = args[0];
        else if (args[0] && typeof args[0].url === 'string') url = args[0].url;

        var _isBmHome  = url.indexOf('/bm/home') !== -1;
        var _isFallback = url.indexOf('home:fallback') !== -1;
        if ((_isBmHome || _isFallback)
                && url.indexOf('customers') === -1
                && url.indexOf('wallet') === -1) {
            // Substitui coordenadas e size apenas para bm/home
            if (_isBmHome) {
                try {
                    var tLat  = localStorage.getItem('__ifTargetLat');
                    var tLon  = localStorage.getItem('__ifTargetLon');
                    var tSize = localStorage.getItem('__ifTargetSize');
                    if (tLat)  url = url.replace(/latitude=[^&]+/,  'latitude='  + tLat);
                    if (tLon)  url = url.replace(/longitude=[^&]+/, 'longitude=' + tLon);
                    if (tSize) url = url.replace(/size=[^&]+/,      'size='      + tSize);
                    if (tLat || tLon || tSize) {
                        if (typeof args[0] === 'string') args[0] = url;
                        else args[0] = new Request(url, args[0]);
                    }
                } catch(e) {}
            }

            var promise = _origFetch.apply(this, args);
            promise.then(function(r) {
                var s = r.status;
                var capUrl = url;
                return r.clone().json().then(function(d) {
                    if (!window.__ifCapResult)
                        window.__ifCapResult = JSON.stringify({status: s, data: d, url: capUrl});
                });
            }).catch(function() {});
            return promise;
        }

        return _origFetch.apply(this, args);
    };
})();
"""

MERCHANT_UUID_RE = re.compile(
    r'"id"\s*:\s*"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"'
)
EXCLUDED_URL_PARTS = [
    'customers/me', 'wallet', 'benefits', 'orders',
    'payment', 'profile', 'address', 'voucher', 'loyalty',
    'cached', 'default',
]
# fallback REMOVIDO: home:fallback retorna 200 sem auth e pode conter merchants

# ---------------------------------------------------------------------------
# STEALTH_SCRIPT — injetado via CDP antes de qualquer script do site
# Cobre os principais vetores de detecção de automação
# ---------------------------------------------------------------------------
STEALTH_SCRIPT = r"""
(function() {
    // webdriver flag — vetor primario de deteccao
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: true,
    });

    // cdc_ vars injetadas pelo Chrome DevTools Client
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });

    // plugins — sites checam instanceof PluginArray, nao apenas .length
    const pluginData = [
        {name: 'PDF Viewer',                filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chrome PDF Viewer',         filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chromium PDF Viewer',       filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'WebKit built-in PDF',       filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    ];
    try {
        const arr = Object.create(PluginArray.prototype);
        pluginData.forEach((p, i) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperty(plugin, 'name',        {value: p.name,        enumerable: true});
            Object.defineProperty(plugin, 'filename',    {value: p.filename,    enumerable: true});
            Object.defineProperty(plugin, 'description', {value: p.description, enumerable: true});
            Object.defineProperty(plugin, 'length',      {value: 0,             enumerable: true});
            arr[i] = plugin;
        });
        Object.defineProperty(arr, 'length', {value: pluginData.length, enumerable: true});
        Object.defineProperty(Navigator.prototype, 'plugins', {get: () => arr, configurable: true});
    } catch(e) {}

    // propriedades do navigator
    Object.defineProperty(Navigator.prototype, 'languages',           {get: () => ['pt-BR','pt','en-US','en'], configurable: true});
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {get: () => 8,       configurable: true});
    Object.defineProperty(Navigator.prototype, 'deviceMemory',        {get: () => 8,       configurable: true});
    Object.defineProperty(Navigator.prototype, 'platform',            {get: () => 'Win32', configurable: true});
    Object.defineProperty(Navigator.prototype, 'maxTouchPoints',      {get: () => 0,       configurable: true});

    // WebGL — fingerprint de GPU (Intel Iris para parecer laptop comum)
    try {
        const _get  = WebGLRenderingContext.prototype.getParameter;
        const _get2 = WebGL2RenderingContext.prototype.getParameter;
        const spoof = function(orig) {
            return function(p) {
                if (p === 37445) return 'Intel Inc.';
                if (p === 37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
                return orig.apply(this, arguments);
            };
        };
        WebGLRenderingContext.prototype.getParameter  = spoof(_get);
        WebGL2RenderingContext.prototype.getParameter = spoof(_get2);
    } catch(e) {}

    // Canvas — ruido minimo imperceptivel para quebrar fingerprint hash
    try {
        const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, q) {
            const ctx = this.getContext('2d');
            if (ctx) { const d = ctx.getImageData(0,0,1,1); d.data[0] ^= 1; ctx.putImageData(d,0,0); }
            return _toDataURL.apply(this, arguments);
        };
        const _toBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function(cb, type, q) {
            const ctx = this.getContext('2d');
            if (ctx) { const d = ctx.getImageData(0,0,1,1); d.data[0] ^= 1; ctx.putImageData(d,0,0); }
            return _toBlob.apply(this, arguments);
        };
    } catch(e) {}

    // Permissions API — leak de automacao via query de notificacoes
    try {
        const _query = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.__proto__.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({state: Notification.permission, onchange: null})
                : _query(p);
    } catch(e) {}

    // screen — headless padrao e 800x600 ou 0x0, muito detectavel
    try {
        Object.defineProperty(window.screen, 'width',       {get: () => 1920});
        Object.defineProperty(window.screen, 'height',      {get: () => 1080});
        Object.defineProperty(window.screen, 'availWidth',  {get: () => 1920});
        Object.defineProperty(window.screen, 'availHeight', {get: () => 1040});
        Object.defineProperty(window.screen, 'colorDepth',  {get: () => 24});
        Object.defineProperty(window.screen, 'pixelDepth',  {get: () => 24});
    } catch(e) {}

    // connection — ausencia e detectavel
    try {
        Object.defineProperty(Navigator.prototype, 'connection', {
            get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false}),
            configurable: true,
        });
    } catch(e) {}

    // battery API — usado como fingerprint, remover e mais seguro
    try { delete Navigator.prototype.getBattery; } catch(e) {}

    // chrome runtime mock completo — mock simples e detectado
    window.chrome = {
        app: {
            isInstalled: false,
            InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
            RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'},
        },
        runtime: {connect: () => {}, sendMessage: () => {}, id: undefined, OnInstalledReason: {}},
        loadTimes: function() {
            const now = Date.now() / 1000;
            return {
                requestTime: now - Math.random(), startLoadTime: now - Math.random() * 0.5,
                commitLoadTime: now - Math.random() * 0.3, finishDocumentLoadTime: now,
                finishLoadTime: now + 0.05, firstPaintTime: now, firstPaintAfterLoadTime: 0,
                navigationType: 'Other', wasFetchedViaSpdy: false, wasNpnNegotiated: true,
                npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2',
            };
        },
        csi: function() {
            return {startE: Date.now(), onloadT: Date.now(), pageT: Math.random() * 5000, tran: 15};
        },
    };

    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
})();
"""


# ---------------------------------------------------------------------------
# Bezier mouse movement
# ---------------------------------------------------------------------------

def _bez(t, p0, p1, p2, p3):
    return (1-t)**3*p0 + 3*(1-t)**2*t*p1 + 3*(1-t)*t**2*p2 + t**3*p3


async def move_mouse(tab, x0: float, y0: float, x1: float, y1: float):
    """Move cursor de (x0,y0) a (x1,y1) via Bezier cubico com easing."""
    steps = max(12, int(math.hypot(x1-x0, y1-y0) / 18))
    cp1x = x0 + (x1-x0)*random.uniform(0.15, 0.45) + random.uniform(-50, 50)
    cp1y = y0 + (y1-y0)*random.uniform(0.15, 0.45) + random.uniform(-50, 50)
    cp2x = x0 + (x1-x0)*random.uniform(0.55, 0.85) + random.uniform(-50, 50)
    cp2y = y0 + (y1-y0)*random.uniform(0.55, 0.85) + random.uniform(-50, 50)
    for i in range(steps + 1):
        t  = i / steps
        te = t*t*(3 - 2*t)  # smoothstep easing
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseMoved',
            x=_bez(te, x0, cp1x, cp2x, x1),
            y=_bez(te, y0, cp1y, cp2y, y1),
        ))
        await asyncio.sleep(random.uniform(0.004, 0.018))


# ---------------------------------------------------------------------------
# Helpers de scraping
# ---------------------------------------------------------------------------

def is_restaurant_listing(url: str, body_text: str) -> bool:
    if any(p in url.lower() for p in EXCLUDED_URL_PARTS):
        return False
    if not body_text or body_text.strip() in ('null', '{}', '[]', ''):
        return False
    # endpoint primario — valida pelo alias da resposta, nao so pela URL
    if 'site-api/v2/bm/home' in url:
        return 'HOME_FOOD_DELIVERY' in body_text or 'sections' in body_text
    return len(MERCHANT_UUID_RE.findall(body_text)) >= 5


def extract_merchants(data: dict) -> list[dict]:
    """Extrai merchants de cards MERCHANT_LIST_V2 do response bm/home."""
    found = []
    base_img = (data or {}).get('baseImageUrl', 'https://static-images.ifood.com.br/image/upload')
    for section in (data or {}).get('sections', []):
        for card in section.get('cards', []):
            if card.get('cardType') != 'MERCHANT_LIST_V2':
                continue
            for item in card.get('data', {}).get('contents', []):
                mid  = item.get('id')
                name = item.get('name')
                if not (mid and name):
                    continue
                slug = item.get('slug') or ''
                if not slug:
                    action = item.get('action', '')
                    if 'merchant?' in action:
                        params = dict(urllib.parse.parse_qsl(action.split('?', 1)[1]))
                        slug = params.get('slug', '')
                link = f'https://www.ifood.com.br/delivery/{slug}' if slug else ''
                delivery = item.get('deliveryInfo') or {}
                raw_img  = item.get('imageUrl', '')
                image_url = (base_img + '/t_thumbnail/' + raw_img.lstrip(':')) if raw_img else ''
                found.append({
                    'id':                mid,
                    'name':              name,
                    'link':              link,
                    'category':          item.get('mainCategory', ''),
                    'rating':            item.get('userRating'),
                    'distance_km':       item.get('distance'),
                    'delivery_fee':      delivery.get('fee'),         # centavos (dividir por 100)
                    'delivery_min_min':  delivery.get('timeMinMinutes'),
                    'delivery_max_min':  delivery.get('timeMaxMinutes'),
                    'image_url':         image_url,
                    'is_new':            item.get('isNew'),
                    'is_super':          item.get('isSuperRestaurant'),
                    'is_ifood_delivery': item.get('isIfoodDelivery'),
                    'available':         item.get('available'),
                })
    return found


def filter_new_merchants(merchants: list[dict], seen_ids: set) -> list[dict]:
    new = [m for m in merchants if m['id'] not in seen_ids]
    seen_ids.update(m['id'] for m in new)
    return new


async def set_location_cookies(tab, lat: float, lon: float):
    await tab.send(cdp.network.set_cookie(name='address-latitude',  value=str(lat), domain=IFOOD_HOST, path='/'))
    await tab.send(cdp.network.set_cookie(name='address-longitude', value=str(lon), domain=IFOOD_HOST, path='/'))
    for c in await tab.send(cdp.network.get_cookies(urls=[f'https://{IFOOD_HOST}'])):
        if c.name == 'fstr.session' and IFOOD_HOST in (c.domain or ''):
            try:
                data = json.loads(base64.b64decode(c.value + '=' * (-len(c.value) % 4)).decode())
                data.setdefault('geoPoint', {})
                data['geoPoint']['latitude']  = lat
                data['geoPoint']['longitude'] = lon
                data.setdefault('properties', {}).update({'delLat': lat, 'delLon': lon})
                new_val = base64.b64encode(json.dumps(data, separators=(',',':')).encode()).decode().rstrip('=')
                await tab.send(cdp.network.set_cookie(name='fstr.session', value=new_val, domain=IFOOD_HOST, path='/'))
            except Exception:
                pass
            break


# ---------------------------------------------------------------------------
# Deteccao e resolucao de desafio Akamai
# ---------------------------------------------------------------------------

async def is_challenge_present(tab) -> bool:
    url = tab.url or ''
    if any(x in url.lower() for x in ['challenge', '/entrar', 'access-denied', 'errors.edgesuite']):
        return True
    return bool(await tab.evaluate('!!document.querySelector(\'iframe[src*="wra-api"]\')'))


async def try_auto_hold(tab) -> bool:
    rect = await tab.evaluate("""(() => {
        const el = document.querySelector('iframe[src*="wra-api"]');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {cx: r.left + r.width/2, cy: r.top + r.height/2};
    })()""")
    if not rect:
        return False
    cx, cy = float(rect['cx']), float(rect['cy'])
    await move_mouse(tab, random.randint(100, 400), random.randint(100, 400), cx, cy)
    await asyncio.sleep(random.uniform(0.3, 0.7))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mousePressed', x=cx, y=cy, button=cdp.input_.MouseButton.LEFT, click_count=1,
    ))
    for _ in range(int(random.uniform(5.0, 7.0) / 0.25)):
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseMoved', x=cx+random.uniform(-2,2), y=cy+random.uniform(-2,2),
        ))
        await asyncio.sleep(0.25)
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mouseReleased', x=cx, y=cy, button=cdp.input_.MouseButton.LEFT, click_count=1,
    ))
    await asyncio.sleep(2.0)
    print('[*] Desafio: hold simulado no iframe wra-api')
    return True


async def handle_challenge(tab, manual_timeout: int = 120) -> bool:
    if not await is_challenge_present(tab):
        return True
    print(f'\n[!] Desafio detectado: {tab.url}')
    if await try_auto_hold(tab):
        await asyncio.sleep(2)
        if not await is_challenge_present(tab):
            print('[+] Desafio resolvido automaticamente.')
            return True
    print(f'[!] Automacao falhou — resolva manualmente no browser ({manual_timeout}s)...')
    deadline = asyncio.get_event_loop().time() + manual_timeout
    while asyncio.get_event_loop().time() < deadline:
        if not await is_challenge_present(tab):
            print('[+] Desafio resolvido manualmente.')
            return True
        await asyncio.sleep(1)
    print('[!] Timeout aguardando resolucao.')
    return False


# ---------------------------------------------------------------------------
# Simulacao de comportamento humano
# ---------------------------------------------------------------------------

async def simulate_human(tab):
    """Movimentos Bezier + scroll para alimentar o sensor comportamental."""
    await asyncio.sleep(random.uniform(1.0, 2.5))
    cx, cy = random.randint(200, 900), random.randint(100, 400)
    await move_mouse(tab, random.randint(0, 300), random.randint(0, 200), cx, cy)
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mouseWheel', x=cx, y=cy, delta_x=0, delta_y=random.randint(200, 600),
    ))
    await asyncio.sleep(random.uniform(0.4, 1.0))
    x2, y2 = random.randint(100, 800), random.randint(200, 500)
    await move_mouse(tab, cx, cy, x2, y2)
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def natural_browse(tab):
    """Navega em /restaurantes como usuario real antes do crawl."""
    await tab.get(HOME_URL)
    if await is_challenge_present(tab):
        await handle_challenge(tab)
    await asyncio.sleep(random.uniform(2.0, 4.0))

    px, py = random.randint(400, 700), random.randint(200, 400)
    for _ in range(random.randint(3, 6)):
        x, y = random.randint(150, 1000), random.randint(100, 600)
        await move_mouse(tab, px, py, x, y)
        px, py = x, y
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseWheel', x=x, y=y, delta_x=0, delta_y=random.randint(150, 450),
        ))
        await asyncio.sleep(random.uniform(0.7, 2.4))

    # hover sintetico em card — dispatch de evento sem usar retorno do evaluate
    if random.random() < 0.6:
        await tab.evaluate("""
            (() => {
                const cards = document.querySelectorAll('[class*="merchant"],[class*="card"]');
                if (cards.length > 0) {
                    const c = cards[Math.floor(Math.random() * Math.min(cards.length, 5))];
                    c.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                }
            })()
        """)
        await asyncio.sleep(random.uniform(0.6, 1.8))

    if random.random() < 0.4:
        x, y = random.randint(200, 800), random.randint(200, 400)
        await move_mouse(tab, px, py, x, y)
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseWheel', x=x, y=y, delta_x=0, delta_y=-random.randint(120, 320),
        ))
        await asyncio.sleep(random.uniform(0.5, 1.2))

    await asyncio.sleep(random.uniform(1.5, 3.0))
    print('[*] Navegacao natural concluida')


# ---------------------------------------------------------------------------
# Captura de ponto da grade
# ---------------------------------------------------------------------------

_body_cache: list = [None]   # POST body do bm/home compartilhado entre pontos
_url_cache:  list = [None]   # URL template com alias/params corretos

# JS que le os headers necessarios diretamente dos cookies da sessao
_HEADERS_JS = r"""
(function() {
    var c = {};
    document.cookie.split(';').forEach(function(s) {
        var i = s.indexOf('=');
        if (i > 0) c[s.slice(0,i).trim()] = s.slice(i+1).trim();
    });
    function d(v) { try { return decodeURIComponent(v||''); } catch(e) { return v||''; } }
    return JSON.stringify({
        'authorization':          'Bearer ' + d(c['aAccessToken']),
        'x-ifood-device-id':      d(c['aDeviceId']),
        'x-ifood-session-id':     d(c['aSessionId']),
        'x-ifood-user-id':        d(c['aAccountId']),
        'x-client-application-key': d(c['aFasterAppKey']),
        'account_id':             d(c['aAccountId']),
        'app_version':            d(c['aAppVersion']) || '9.141.4',
        'platform':               'Desktop',
        'browser':                'Windows',
        'country':                'BR',
        'content-type':           'application/json'
    });
})()
"""


async def crawl_point(tab, lat: float, lon: float, page_size: int = 50, timeout: float = 35.0) -> dict | None:
    # request_id -> (url, status)
    pending_resp: dict[str, tuple[str, int]] = {}
    captured: dict = {}
    cdp_done = asyncio.Event()

    CAPTURE_PATHS = ('bm/home', 'home:fallback', 'home:')

    def on_request(event: cdp.network.RequestWillBeSent):
        url = event.request.url
        if any(p in url for p in CAPTURE_PATHS) and not any(p in url.lower() for p in EXCLUDED_URL_PARTS):
            rid = str(event.request_id)
            pending_resp[rid] = (url, 0)
            if 'bm/home' in url:
                if not _url_cache[0]:
                    _url_cache[0] = url
                post = getattr(event.request, 'post_data', None)
                if post and not _body_cache[0]:
                    _body_cache[0] = post

    async def on_response(event: cdp.network.ResponseReceived):
        rid = str(event.request_id)
        if rid not in pending_resp:
            return
        url = pending_resp[rid][0]
        pending_resp[rid] = (url, event.response.status)

        # SW-served responses: LoadingFinished nao dispara, body ja disponivel aqui
        if getattr(event.response, 'from_service_worker', False) and not cdp_done.is_set():
            if event.response.status == 200:
                pending_resp.pop(rid, None)
                try:
                    await asyncio.sleep(0.05)
                    result = await tab.send(cdp.network.get_response_body(event.request_id))
                    body_text = (
                        base64.b64decode(result.body).decode('utf-8', errors='replace')
                        if result.base_64_encoded else result.body
                    )
                    data = json.loads(body_text)
                    if data is None or not isinstance(data, dict):
                        return
                    if is_restaurant_listing(url, body_text):
                        captured.update({'url': url, 'status': 200, 'data': data})
                        _url_cache[0] = url
                        cdp_done.set()
                except Exception as e:
                    print(f'  [sw-body] {e}')

    async def on_loading_finished(event: cdp.network.LoadingFinished):
        # Body disponivel somente apos LoadingFinished — momento correto para get_response_body
        if cdp_done.is_set():
            return
        rid = str(event.request_id)
        if rid not in pending_resp:
            return
        url, status = pending_resp.pop(rid)
        if status not in (200, 0):
            return
        try:
            result = await tab.send(cdp.network.get_response_body(event.request_id))
            body_text = (
                base64.b64decode(result.body).decode('utf-8', errors='replace')
                if result.base_64_encoded else result.body
            )
            data = json.loads(body_text)
            if data is None or not isinstance(data, dict):
                return
            if is_restaurant_listing(url, body_text):
                captured.update({'url': url, 'status': status, 'data': data})
                _url_cache[0] = url
                cdp_done.set()
        except Exception:
            pass

    tab.add_handler(cdp.network.RequestWillBeSent, on_request)
    tab.add_handler(cdp.network.ResponseReceived, on_response)
    tab.add_handler(cdp.network.LoadingFinished, on_loading_finished)

    try:
        # Cookies de localizacao antes da navegacao — garante coordenadas corretas
        # mesmo que CAPTURE_SCRIPT nao consiga interceptar (ex: service worker).
        await set_location_cookies(tab, lat, lon)

        # Grava coordenadas e size no localStorage para o CAPTURE_SCRIPT substituir na URL
        await tab.evaluate(f"""
            try {{
                localStorage.setItem('__ifTargetLat',  '{lat}');
                localStorage.setItem('__ifTargetLon',  '{lon}');
                localStorage.setItem('__ifTargetSize', '{page_size}');
            }} catch(e) {{}}
            window.__ifCapResult = null;
            window.__result = null;
        """)

        print('  [nav] carregando /restaurantes...')
        await tab.get(HOME_URL)
        print('  [nav] carregada')

        await simulate_human(tab)

        if await is_challenge_present(tab):
            print('  [challenge] detectado')
            if not await handle_challenge(tab):
                return None

        # Estrategia 1: CDP ResponseReceived (captura qualquer request, incluindo SW)
        try:
            await asyncio.wait_for(cdp_done.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass

        if cdp_done.is_set():
            print('  [api] ok (CDP)')
            return {'api_url': captured['url'], 'status': captured['status'], 'resp_body': captured['data']}

        # Estrategia 2: CAPTURE_SCRIPT — fetch intercept via JS (leitura unica, sem polling)
        nat_raw = await tab.evaluate('window.__ifCapResult')
        if nat_raw:
            try:
                payload = json.loads(nat_raw)
                data    = payload.get('data')
                nat_url = payload.get('url', '')
                if nat_url:
                    _url_cache[0] = nat_url
                if data is not None:
                    data_s = json.dumps(data)
                    if is_restaurant_listing(nat_url or 'bm/home', data_s):
                        print('  [api] ok (natural)')
                        return {'api_url': nat_url, 'status': payload.get('status', 200), 'resp_body': data}
                    if isinstance(data, dict) and data.get('message') == 'Not Found':
                        return {'api_url': nat_url, 'status': 200, 'resp_body': data}
                    print(f'  [warn-natural] {data_s[:100]}')
            except Exception:
                pass

        # Estrategia 3: fetch manual com headers dos cookies (fallback final)
        print('  [natural] nao-interceptado — fetch manual')

        base_url = _url_cache[0] or 'https://www.ifood.com.br/site-api/v2/bm/home'
        parsed   = urllib.parse.urlparse(base_url)
        params   = dict(urllib.parse.parse_qsl(parsed.query))
        params['latitude']  = str(lat)
        params['longitude'] = str(lon)
        params['size']      = str(page_size)
        bm_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))

        headers_raw = await tab.evaluate(_HEADERS_JS)
        headers_js  = headers_raw or '{}'
        auth_token  = json.loads(headers_js).get('authorization', '')
        print(f'  [auth] {"ok" if "Bearer ey" in auth_token else "VAZIO"}  '
              f'[body] {"cached" if _body_cache[0] else "null"}')

        body_js = json.dumps(_body_cache[0]) if _body_cache[0] else 'null'

        for attempt in range(2):
            print(f'  [fetch] POST {bm_url[:90]}{" (retry)" if attempt else ""}')
            await tab.evaluate(f"""
            (function() {{
                window.__result = null;
                var opts = {{
                    method: 'POST',
                    credentials: 'include',
                    cache: 'no-cache',
                    headers: {headers_js}
                }};
                var body = {body_js};
                if (body !== null) opts.body = body;
                fetch({json.dumps(bm_url)}, opts)
                    .then(function(r) {{ return r.json(); }})
                    .then(function(data) {{
                        window.__result = JSON.stringify({{ok: true, data: data}});
                    }})
                    .catch(function(e) {{
                        window.__result = JSON.stringify({{ok: false, err: e.message}});
                    }});
            }})();
            """)

            fetch_result = None
            fetch_end = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < fetch_end:
                raw = await tab.evaluate('window.__result')
                if raw:
                    fetch_result = json.loads(raw)
                    break
                await asyncio.sleep(0.3)

            if fetch_result is None:
                print('  [warn] timeout fetch JS')
                return None

            if fetch_result.get('ok'):
                body = fetch_result['data']
                body_s = json.dumps(body)
                if is_restaurant_listing(bm_url, body_s):
                    print('  [api] ok')
                    return {'api_url': bm_url, 'status': 200, 'resp_body': body}
                if isinstance(body, dict) and body.get('message') == 'Not Found':
                    return {'api_url': bm_url, 'status': 200, 'resp_body': body}
                print(f'  [warn] {body_s[:120]}')
                return None

            err = fetch_result.get('err', '')
            print(f'  [api-err] {err}')

            if attempt == 0 and ('not valid JSON' in err or '<html' in err.lower()):
                print('  [recovery] aguardando auto-refresh do token (5s)...')
                await asyncio.sleep(5.0)
                headers_raw = await tab.evaluate(_HEADERS_JS)
                headers_js  = headers_raw or '{}'
                await tab.evaluate('window.__result = null;')
                continue

            return None

        return None

    finally:
        for evt_cls, fn in [
            (cdp.network.RequestWillBeSent, on_request),
            (cdp.network.ResponseReceived, on_response),
            (cdp.network.LoadingFinished, on_loading_finished),
        ]:
            try:
                tab.handlers.get(evt_cls, []).remove(fn)
            except (ValueError, AttributeError):
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _clear_profile_locks():
    for f in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        (CHROME_PROFILE / f).unlink(missing_ok=True)


def _stop_our_chrome(pid: int | None):
    """Encerra somente o processo Chrome lançado por este script (e seus filhos)."""
    if pid is not None:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    _PID_FILE.unlink(missing_ok=True)
    _clear_profile_locks()


def _kill_previous_crawler_chrome():
    """Encerra somente o Chrome do crawler: pelo PID salvo e/ou pelo perfil."""
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        except Exception:
            pass
        _PID_FILE.unlink(missing_ok=True)

    # Sempre busca Chrome com nosso perfil — cobre casos sem PID salvo
    profile_name = CHROME_PROFILE.name
    subprocess.run([
        'powershell', '-Command',
        f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{profile_name}*'}} | "
        f"ForEach-Object {{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}",
    ], capture_output=True)

    _clear_profile_locks()


async def main(city: str, step_km: float | None, delay: float, headless: bool, max_points: int = 0, page_size: int = 50, use_boundary: bool = False):
    city_cfg = CITIES[city]
    points   = generate_city_grid(city, step_km=step_km, use_boundary=use_boundary)
    used_step = step_km if step_km is not None else city_cfg['step_km']
    if max_points:
        points = points[:max_points]
    print(f'[*] Cidade: {city_cfg["name"]}')
    print(f'[*] Grade {used_step} km: {len(points)} pontos')

    ts              = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir         = Path(__file__).parent.parent / 'captures' / f'crawl_nd_{city}_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path      = out_dir / 'requests.jsonl'
    merchants_path  = out_dir / 'merchants.jsonl'
    log_path        = out_dir / 'crawl.log'
    points_path     = out_dir / 'points.jsonl'

    seen_ids     = set()
    total_new    = 0
    detected_api = None
    browser_pid  = None

    # Encerra somente o Chrome de execucao anterior do crawler (pelo PID salvo)
    _kill_previous_crawler_chrome()
    await asyncio.sleep(0.5)

    CHROME_PROFILE.mkdir(exist_ok=True)

    browser = await uc.start(
        user_data_dir=str(CHROME_PROFILE),
        headless=headless,
        lang='pt-BR',
        browser_args=[
            '--lang=pt-BR',
            '--disable-blink-features=AutomationControlled',
            '--no-first-run',
            '--no-default-browser-check',
            '--exclude-switches=enable-automation',
            '--disable-infobars',
            '--window-size=1920,1080',
            '--start-minimized',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        ],
    )

    try:
        browser_pid = browser.process.pid
        CHROME_PROFILE.mkdir(exist_ok=True)
        _PID_FILE.write_text(str(browser_pid))
        print(f'[*] Chrome PID: {browser_pid}')
    except AttributeError:
        browser_pid = None

    tab = await browser.get('about:blank')
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=STEALTH_SCRIPT))
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=CAPTURE_SCRIPT))
    await tab.send(cdp.network.enable())
    try:
        await tab.send(cdp.network.set_bypass_service_worker(bypass=True))
        print('[*] Service Worker bypass ativo (home:fallback vai a rede)')
    except Exception:
        print('[*] set_bypass_service_worker nao disponivel — usando fallback SW')

    # Verifica sessao — espera ate 15s para aAccessToken ser renovado automaticamente
    print('[*] Verificando sessao...')
    await tab.get(HOME_URL)
    if await is_challenge_present(tab):
        await handle_challenge(tab)

    logged_in = False
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        cookies = await tab.send(cdp.network.get_cookies(urls=[f'https://{IFOOD_HOST}']))
        if any(c.name == 'aAccessToken' and c.value for c in cookies):
            logged_in = True
            break
        print('[*] Aguardando renovacao do token...')
        await asyncio.sleep(2.0)

    if not logged_in:
        print('[!] Sessao invalida. Execute login.py e tente novamente.')
        try:
            browser.stop()
        except Exception:
            pass
        _stop_our_chrome(browser_pid)
        _clear_profile_locks()
        return
    print('[+] Sessao valida.')

    # Aquecimento: navega naturalmente para acumular sinais positivos no sensor Akamai
    print('[*] Aquecendo sensor Akamai (2 navegacoes naturais)...')
    for _ in range(2):
        await natural_browse(tab)
        await asyncio.sleep(random.uniform(3.0, 6.0))
    print('[+] Aquecimento concluido. Iniciando crawl.')
    print(f'[*] Salvando em: {out_dir}')
    print(f'[*] Delay base: {delay}s (+-20% jitter)\n')

    with open(jsonl_path,     'w', encoding='utf-8') as jf, \
         open(merchants_path, 'w', encoding='utf-8') as mf, \
         open(log_path,       'w', encoding='utf-8') as lf, \
         open(points_path,    'w', encoding='utf-8') as pf:

        lf.write(f'Cidade: {city_cfg["name"]}\n')
        lf.write(f'Inicio: {datetime.now()}\n')
        lf.write(f'Pontos: {len(points)}\n\n')

        for i, (lat, lon) in enumerate(points, 1):
            try:
                await natural_browse(tab)
                result = await crawl_point(tab, lat, lon, page_size=page_size)

                if result is None:
                    raise Exception('nenhuma chamada API (timeout)')

                if detected_api is None and result.get('api_url'):
                    detected_api = result['api_url'].split('?')[0]
                    msg = f'[+] Endpoint detectado: {detected_api}'
                    print(msg); lf.write(msg + '\n')

                all_here = extract_merchants(result.get('resp_body'))
                new_here = filter_new_merchants(all_here, seen_ids)
                total_new += len(new_here)

                for m in new_here:
                    mf.write(json.dumps({**m, 'lat': lat, 'lon': lon},
                                        ensure_ascii=False) + '\n')
                mf.flush()

                jf.write(json.dumps({'point_index': i, 'lat': lat, 'lon': lon, **result},
                                    ensure_ascii=False) + '\n')
                jf.flush()

                pf.write(json.dumps({
                    'index': i, 'lat': lat, 'lon': lon,
                    'count': len(all_here), 'total': total_new,
                    'names': [m['name'] for m in all_here[:10]],
                    'error': False,
                }, ensure_ascii=False) + '\n')
                pf.flush()

                line = (f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) '
                        f'status={result.get("status")} novos={len(new_here)} total={total_new}')
                if new_here:
                    preview = ', '.join(m['name'] for m in new_here[:3])
                    extra   = f' (+{len(new_here)-3})' if len(new_here) > 3 else ''
                    line   += f'\n  -> {preview}{extra}'

            except Exception as e:
                pf.write(json.dumps({
                    'index': i, 'lat': lat, 'lon': lon,
                    'count': 0, 'total': total_new, 'names': [], 'error': True,
                }, ensure_ascii=False) + '\n')
                pf.flush()
                line = f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) ERRO: {e}'

            print(line); lf.write(line + '\n'); lf.flush()
            wait = delay * random.uniform(0.8, 1.2)
            # Keepalive: pinga CDP a cada 10s para nao deixar o WebSocket cair
            slept = 0.0
            while slept < wait:
                chunk = min(10.0, wait - slept)
                await asyncio.sleep(chunk)
                slept += chunk
                if slept < wait:
                    try:
                        await tab.send(cdp.target.get_targets())
                    except Exception:
                        pass

        lf.write(f'\nFim: {datetime.now()}\nTotal merchants unicos: {total_new}\n')

    print(f'\n[+] Concluido. Merchants unicos: {total_new}')
    print(f'[+] Merchants: {merchants_path}')
    print(f'[+] Requests:  {jsonl_path}')
    print(f'[+] Log:       {log_path}')
    try:
        browser.stop()
    except Exception:
        pass
    _stop_our_chrome(browser_pid)
    _clear_profile_locks()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler iFood Web (nodriver) — multi-cidade')
    parser.add_argument('--city',        default='sao-paulo',
                        help='Cidade a crawlear (default: sao-paulo)')
    parser.add_argument('--list-cities', action='store_true',
                        help='Lista cidades disponíveis e sai')
    parser.add_argument('--step',        type=float, default=None,
                        help='Espacamento da grade em km (None = usa padrão da cidade)')
    parser.add_argument('--delay',       type=float, default=60.0, help='Delay base entre navegacoes (s)')
    parser.add_argument('--headless',    action='store_true',      help='Rodar sem janela')
    parser.add_argument('--max-points',  type=int,   default=0,    help='Limite de pontos (0 = sem limite)')
    parser.add_argument('--page-size',   type=int,   default=50,   help='Restaurantes por ponto da grade (default 50)')
    parser.add_argument('--boundary',    action='store_true',      help='Filtrar grade pelo polígono real do município via OSM (requer shapely e requests)')
    args = parser.parse_args()

    if args.list_cities:
        print(f"\n{'Chave':<20} {'Cidade':<28} {'Step padrão':<14} {'Pontos aprox.'}")
        print('-' * 72)
        from configs.cities import generate_city_grid as _gcg
        for key, cfg in CITIES.items():
            n = len(_gcg(key))
            print(f"  {key:<18} {cfg['name']:<28} {cfg['step_km']:<14.1f} ~{n}")
        print()
        sys.exit(0)

    if args.city not in CITIES:
        print(f"[!] Cidade '{args.city}' não encontrada. Use --list-cities para ver as opções.")
        sys.exit(1)

    uc.loop().run_until_complete(main(args.city, args.step, args.delay, args.headless, args.max_points, args.page_size, args.boundary))
