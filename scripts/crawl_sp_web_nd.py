"""
Crawler iFood Web — SP via nodriver (CDP nativo, sem WebDriver).

nodriver controla o Chrome diretamente via CDP sem nenhum marcador de automação.
Respostas da API capturadas via eventos CDP de rede (Network.ResponseReceived +
Network.getResponseBody) — captura qualquer request (fetch, XHR, service worker)
sem pausar ou modificar nada na rede.

Uso:
    pip install nodriver
    python scripts/login.py                     # login manual (Playwright, mesmo perfil)
    python scripts/crawl_sp_web_nd.py [--step 8.0] [--delay 60.0] [--headless]

Saída em captures/crawl_nd_TIMESTAMP/:
    requests.jsonl  — um JSON por linha com request + response
    crawl.log       — progresso e contagem de merchants únicos
"""

import asyncio
import argparse
import base64
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import nodriver as uc
from nodriver import cdp

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.sp_grid import generate_grid

INICIO_URL     = "https://www.ifood.com.br/inicio"
HOME_URL       = "https://www.ifood.com.br/restaurantes"
IFOOD_HOST     = "www.ifood.com.br"
CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'

MERCHANT_UUID_RE = re.compile(
    r'"id"\s*:\s*"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"'
)
EXCLUDED_URL_PARTS = [
    'customers/me', 'wallet', 'benefits', 'orders',
    'payment', 'profile', 'address', 'voucher', 'loyalty',
    'fallback', 'cached', 'default',
]

# Patches de fingerprint injetados antes de qualquer script da página.
# Captura de respostas é feita via CDP Network events (não JS), então o
# interceptor de fetch/XHR não é mais necessário aqui.
STEALTH_SCRIPT = r"""
(function() {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: true,
    });
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });
    const pluginData = [
        {name: 'PDF Viewer',                filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chrome PDF Viewer',         filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chromium PDF Viewer',       filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'WebKit built-in PDF',       filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
    ];
    try {
        const fakePluginArray = Object.create(PluginArray.prototype);
        pluginData.forEach((p, i) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperty(plugin, 'name',        {value: p.name,        enumerable: true});
            Object.defineProperty(plugin, 'filename',    {value: p.filename,    enumerable: true});
            Object.defineProperty(plugin, 'description', {value: p.description, enumerable: true});
            Object.defineProperty(plugin, 'length',      {value: 0,             enumerable: true});
            fakePluginArray[i] = plugin;
        });
        Object.defineProperty(fakePluginArray, 'length', {value: pluginData.length, enumerable: true});
        Object.defineProperty(Navigator.prototype, 'plugins', {get: () => fakePluginArray, configurable: true});
    } catch(e) {}
    Object.defineProperty(Navigator.prototype, 'languages',           {get: () => ['pt-BR', 'pt', 'en-US', 'en'], configurable: true});
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {get: () => 8, configurable: true});
    Object.defineProperty(Navigator.prototype, 'deviceMemory',        {get: () => 8, configurable: true});
    window.chrome = {
        app: {isInstalled: false},
        runtime: {connect: () => {}, sendMessage: () => {}},
        loadTimes: function() {}, csi: function() {},
    };
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
})();
"""


def is_restaurant_listing(url: str, body_text: str) -> bool:
    if any(part in url.lower() for part in EXCLUDED_URL_PARTS):
        return False
    return len(MERCHANT_UUID_RE.findall(body_text)) >= 5


def count_new_merchants(data, seen_ids: set) -> int:
    if not data:
        return 0
    text = json.dumps(data)
    ids  = re.findall(
        r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"',
        text,
    )
    new = [mid for mid in ids if mid not in seen_ids]
    seen_ids.update(new)
    return len(new)


async def set_location_cookies(tab, lat: float, lon: float):
    await tab.send(cdp.network.set_cookie(
        name='address-latitude', value=str(lat), domain=IFOOD_HOST, path='/',
    ))
    await tab.send(cdp.network.set_cookie(
        name='address-longitude', value=str(lon), domain=IFOOD_HOST, path='/',
    ))
    cookies = await tab.send(cdp.network.get_all_cookies())
    for c in cookies:
        if c.name == 'fstr.session' and IFOOD_HOST in (c.domain or ''):
            try:
                padded = c.value + '=' * (-len(c.value) % 4)
                data   = json.loads(base64.b64decode(padded).decode('utf-8'))
                data.setdefault('geoPoint', {})
                data['geoPoint']['latitude']  = lat
                data['geoPoint']['longitude'] = lon
                props = data.setdefault('properties', {})
                props['delLat'] = lat
                props['delLon'] = lon
                new_val = base64.b64encode(
                    json.dumps(data, separators=(',', ':')).encode()
                ).decode().rstrip('=')
                await tab.send(cdp.network.set_cookie(
                    name='fstr.session', value=new_val, domain=IFOOD_HOST, path='/',
                ))
            except Exception:
                pass
            break


async def is_challenge_present(tab) -> bool:
    url = tab.url or ''
    if any(x in url.lower() for x in ['challenge', '/entrar', 'access-denied', 'errors.edgesuite']):
        return True
    result = await tab.evaluate(
        '!!document.querySelector(\'iframe[src*="wra-api"]\')'
    )
    return bool(result)


async def try_auto_hold(tab) -> bool:
    rect = await tab.evaluate("""
        (() => {
            const el = document.querySelector('iframe[src*="wra-api"]');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {cx: r.left + r.width / 2, cy: r.top + r.height / 2};
        })()
    """)
    if not rect:
        return False

    cx, cy = float(rect['cx']), float(rect['cy'])

    await tab.send(cdp.input_.dispatch_mouse_event(type_='mouseMoved', x=cx, y=cy))
    await asyncio.sleep(random.uniform(0.4, 0.8))

    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mousePressed', x=cx, y=cy,
        button=cdp.input_.MouseButton.LEFT, click_count=1,
    ))

    hold  = random.uniform(5.0, 7.0)
    ticks = int(hold / 0.25)
    for _ in range(ticks):
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseMoved',
            x=cx + random.uniform(-2, 2),
            y=cy + random.uniform(-2, 2),
        ))
        await asyncio.sleep(0.25)

    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mouseReleased', x=cx, y=cy,
        button=cdp.input_.MouseButton.LEFT, click_count=1,
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

    print('[!] Timeout aguardando resolucao do desafio.')
    return False


async def simulate_human(tab):
    await asyncio.sleep(random.uniform(1.0, 2.5))
    x, y = random.randint(200, 900), random.randint(100, 400)
    await tab.send(cdp.input_.dispatch_mouse_event(type_='mouseMoved', x=x, y=y))
    await asyncio.sleep(random.uniform(0.4, 1.0))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_='mouseWheel', x=x, y=y, delta_x=0, delta_y=random.randint(200, 600),
    ))
    await asyncio.sleep(random.uniform(0.5, 1.2))
    x2, y2 = random.randint(100, 800), random.randint(200, 500)
    await tab.send(cdp.input_.dispatch_mouse_event(type_='mouseMoved', x=x2, y=y2))
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def natural_browse(tab):
    """Navega em /inicio como usuário real antes do crawl."""
    await tab.get(INICIO_URL)

    if await is_challenge_present(tab):
        await handle_challenge(tab)

    await asyncio.sleep(random.uniform(2.0, 4.0))

    for _ in range(random.randint(3, 6)):
        x, y = random.randint(150, 1000), random.randint(100, 600)
        await tab.send(cdp.input_.dispatch_mouse_event(type_='mouseMoved', x=x, y=y))
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseWheel', x=x, y=y, delta_x=0, delta_y=random.randint(200, 500),
        ))
        await asyncio.sleep(random.uniform(0.8, 2.2))

    # Hover sintético em card de restaurante
    if random.random() < 0.6:
        await tab.evaluate("""
            (() => {
                const cards = document.querySelectorAll('[class*="merchant"], [class*="card"]');
                if (cards.length > 0) {
                    const c = cards[Math.floor(Math.random() * Math.min(cards.length, 5))];
                    c.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                }
            })()
        """)
        await asyncio.sleep(random.uniform(0.6, 1.8))

    if random.random() < 0.4:
        x, y = random.randint(200, 800), random.randint(200, 400)
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_='mouseWheel', x=x, y=y, delta_x=0, delta_y=-random.randint(150, 350),
        ))
        await asyncio.sleep(random.uniform(0.5, 1.2))

    await asyncio.sleep(random.uniform(1.5, 3.0))
    print('[*] Navegacao natural concluida')


async def crawl_point(tab, lat: float, lon: float, timeout: float = 35.0) -> dict | None:
    captured = {}
    done     = asyncio.Event()
    pending  = {}   # request_id -> url
    active   = True  # flag para desligar handlers após retorno

    def on_request(event: cdp.network.RequestWillBeSent):
        if active and 'site-api' in (event.request.url or ''):
            pending[event.request_id] = event.request.url

    async def on_response(event: cdp.network.ResponseReceived):
        if not active or done.is_set():
            return
        url = pending.pop(event.request_id, None)
        if not url or any(p in url.lower() for p in EXCLUDED_URL_PARTS):
            return
        try:
            # Busca o body imediatamente — Chrome libera o buffer logo depois
            result   = await tab.send(cdp.network.get_response_body(event.request_id))
            body_str = result.body
            if getattr(result, 'base64_encoded', False):
                body_str = base64.b64decode(body_str).decode('utf-8')
            body      = json.loads(body_str)
            body_text = json.dumps(body)
            if is_restaurant_listing(url, body_text):
                captured['api_url']   = url
                captured['status']    = event.response.status
                captured['resp_body'] = body
                done.set()
        except Exception:
            pass

    # Registra handlers antes de navegar para não perder respostas durante o load
    tab.add_handler(cdp.network.RequestWillBeSent, on_request)
    tab.add_handler(cdp.network.ResponseReceived,  on_response)

    try:
        await tab.get(HOME_URL)
        await simulate_human(tab)

        deadline = asyncio.get_event_loop().time() + timeout
        while not done.is_set() and asyncio.get_event_loop().time() < deadline:
            if await is_challenge_present(tab):
                await handle_challenge(tab)
            await asyncio.sleep(0.5)
    finally:
        active = False
        # Remove handlers desta chamada da lista interna do nodriver
        for evt, fn in [
            (cdp.network.RequestWillBeSent, on_request),
            (cdp.network.ResponseReceived,  on_response),
        ]:
            try:
                tab.handlers.get(evt, []).remove(fn)
            except (ValueError, AttributeError):
                pass

    return captured if captured else None


async def main(step_km: float, delay: float, headless: bool):
    points = generate_grid(step_km=step_km)
    print(f'[*] Grade {step_km} km: {len(points)} pontos')

    ts         = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir    = Path(__file__).parent.parent / 'captures' / f'crawl_nd_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / 'requests.jsonl'
    log_path   = out_dir / 'crawl.log'

    seen_ids     = set()
    total_new    = 0
    detected_api = None

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
        ],
    )

    tab = await browser.get('about:blank')

    # Registra patches de stealth para rodar em todo novo documento
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=STEALTH_SCRIPT))
    await tab.send(cdp.network.enable())

    # Verifica sessão
    print('[*] Verificando sessao...')
    await tab.get(HOME_URL)
    if await is_challenge_present(tab):
        await handle_challenge(tab)
    cookies   = await tab.send(cdp.network.get_all_cookies())
    logged_in = any(c.name == 'aAccessToken' and c.value for c in cookies)
    if not logged_in:
        print('[!] Sessao invalida. Execute login.py e tente novamente.')
        browser.stop()
        return
    print('[+] Sessao valida. Iniciando crawl.')
    print(f'[*] Salvando em: {out_dir}')
    print(f'[*] Delay base: {delay}s (+-20% jitter)\n')

    with open(jsonl_path, 'w', encoding='utf-8') as jf, \
         open(log_path,   'w', encoding='utf-8') as lf:

        lf.write(f'Inicio: {datetime.now()}\n')
        lf.write(f'Pontos: {len(points)}\n\n')

        for i, (lat, lon) in enumerate(points, 1):
            try:
                await set_location_cookies(tab, lat, lon)
                await natural_browse(tab)
                result = await crawl_point(tab, lat, lon)

                if result is None:
                    raise Exception('nenhuma chamada API com merchants (timeout)')

                if detected_api is None and result.get('api_url'):
                    detected_api = result['api_url'].split('?')[0]
                    msg = f'[+] Endpoint detectado: {detected_api}'
                    print(msg)
                    lf.write(msg + '\n')

                new_here   = count_new_merchants(result.get('resp_body'), seen_ids)
                total_new += new_here

                record = {'point_index': i, 'lat': lat, 'lon': lon, **result}
                jf.write(json.dumps(record, ensure_ascii=False) + '\n')
                jf.flush()

                line = (f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) '
                        f'status={result.get("status")} '
                        f'novos={new_here} total={total_new}')

            except Exception as e:
                line = f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) ERRO: {e}'

            print(line)
            lf.write(line + '\n')
            lf.flush()

            jitter = delay * random.uniform(0.8, 1.2)
            await asyncio.sleep(jitter)

        lf.write(f'\nFim: {datetime.now()}\n')
        lf.write(f'Total de merchants unicos: {total_new}\n')

    print(f'\n[+] Concluido. Merchants unicos: {total_new}')
    print(f'[+] Requests: {jsonl_path}')
    print(f'[+] Log:      {log_path}')

    browser.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler iFood Web SP (nodriver)')
    parser.add_argument('--step',     type=float, default=8.0,  help='Espacamento da grade em km')
    parser.add_argument('--delay',    type=float, default=60.0, help='Delay base entre navegacoes (s)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    args = parser.parse_args()
    uc.loop().run_until_complete(main(args.step, args.delay, args.headless))
