"""
Crawler iFood API direto — browser sobe UMA vez para capturar o token,
todos os requests bm/home sao feitos diretamente via HTTP (sem browser por ponto).

Vantagem:
  - Sem Human Security por request (o sensor JS roda so no browser)
  - Sem rate limit de sessao de navegacao
  - Muito mais rapido (~2-5s por ponto vs ~60s no crawler normal)
  - Complementar ao crawl.py — ambos podem ser usados

Uso:
    pip install httpx
    python scripts/login.py      # apenas na primeira vez
    python scripts/crawl_api.py [--city sao-joao-del-rei] [--delay 2.0]
    python scripts/crawl_api.py --list-cities

Saida em captures/crawl_api_CIDADE_TIMESTAMP/:
    merchants.jsonl  — merchants unicos encontrados
    points.jsonl     — resultado por ponto da grade
    crawl.log        — progresso

Dependencias extras:
    pip install httpx nodriver
"""

import asyncio
import argparse
import base64
import json
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import nodriver as uc
from nodriver import cdp

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.cities import CITIES, generate_city_grid

HOME_URL       = 'https://www.ifood.com.br/restaurantes'
IFOOD_HOST     = 'www.ifood.com.br'
CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
_PID_FILE      = CHROME_PROFILE / '.crawler_pid'

# Tempo maximo de uso de uma sessao antes de renovar proativamente (45 min)
SESSION_TTL = 45 * 60

MERCHANT_UUID_RE = re.compile(
    r'"id"\s*:\s*"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"'
)
EXCLUDED_URL_PARTS = [
    'customers/me', 'wallet', 'benefits', 'orders',
    'payment', 'profile', 'address', 'voucher', 'loyalty',
    'cached', 'default',
]

# ---------------------------------------------------------------------------
# Skip listener — digitar "s" + Enter pula o ponto atual em caso de erro
# ---------------------------------------------------------------------------

_skip_event = threading.Event()


def _start_skip_listener() -> None:
    def _reader():
        while True:
            try:
                if sys.stdin.readline().strip().lower() == 's':
                    _skip_event.set()
            except Exception:
                break
    threading.Thread(target=_reader, daemon=True).start()


async def _interruptible_sleep(seconds: float) -> bool:
    """Aguarda até `seconds`. Retorna True se o usuário pediu para pular."""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        if _skip_event.is_set():
            _skip_event.clear()
            return True
        await asyncio.sleep(0.2)
    return False


STEALTH_SCRIPT = r"""
(function() {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: true,
    });
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });
    Object.defineProperty(Navigator.prototype, 'languages', {get: () => ['pt-BR','pt','en-US','en'], configurable: true});
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {get: () => 8, configurable: true});
    Object.defineProperty(Navigator.prototype, 'platform', {get: () => 'Win32', configurable: true});
    try {
        Object.defineProperty(Navigator.prototype, 'connection', {
            get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false}),
            configurable: true,
        });
    } catch(e) {}
    try { delete Navigator.prototype.getBattery; } catch(e) {}

    // HUMAN Security / PerimeterX
    try {
        const _ga = Element.prototype.getAttribute;
        Element.prototype.getAttribute = function(name) {
            if (name === 'webdriver') return null;
            return _ga.apply(this, arguments);
        };
    } catch(e) {}
    try { Object.defineProperty(window, 'domAutomation',           {get: () => undefined, configurable: true}); } catch(e) {}
    try { Object.defineProperty(window, 'domAutomationController', {get: () => undefined, configurable: true}); } catch(e) {}
    try { Object.defineProperty(window, '_Selenium_IDE_Recorder',  {get: () => undefined, configurable: true}); } catch(e) {}
    try { Object.defineProperty(window, '__webdriver_script_fn',   {get: () => undefined, configurable: true}); } catch(e) {}
    try {
        if (!window.external || !window.external.AddSearchProvider) {
            Object.defineProperty(window, 'external', {
                value: Object.freeze({AddSearchProvider: function(){}, IsSearchProviderInstalled: function(){}}),
                configurable: true,
            });
        }
    } catch(e) {}
    try {
        const _nativeToStr = Function.prototype.toString;
        const _proxied = new WeakSet();
        Function.prototype.toString = function() {
            if (_proxied.has(this)) return 'function ' + (this.__nativeName || this.name || '') + '() { [native code] }';
            return _nativeToStr.call(this);
        };
        _proxied.add(Element.prototype.getAttribute);
        _proxied.add(Function.prototype.toString);
    } catch(e) {}
})();
"""

_COOKIES_JS = r"""
(function() {
    var c = {};
    document.cookie.split(';').forEach(function(s) {
        var i = s.indexOf('=');
        if (i > 0) c[s.slice(0,i).trim()] = s.slice(i+1).trim();
    });
    function d(v) { try { return decodeURIComponent(v||''); } catch(e) { return v||''; } }
    return JSON.stringify({
        authorization:             'Bearer ' + d(c['aAccessToken']),
        'x-ifood-device-id':       d(c['aDeviceId']),
        'x-ifood-session-id':      d(c['aSessionId']),
        'x-ifood-user-id':         d(c['aAccountId']),
        'x-client-application-key': d(c['aFasterAppKey']),
        account_id:                d(c['aAccountId']),
        app_version:               d(c['aAppVersion']) || '9.141.4',
        platform:                  'Desktop',
        browser:                   'Windows',
        country:                   'BR',
        'content-type':            'application/json',
        'accept':                  'application/json, text/plain, */*',
        'accept-language':         'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'origin':                  'https://www.ifood.com.br',
        'referer':                 'https://www.ifood.com.br/restaurantes',
    });
})()
"""


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------

@dataclass
class Session:
    headers: dict
    cookies: dict              # cookies do browser incluindo _px3, _pxvid (HUMAN Security)
    bm_url_template: str       # URL completa de uma captura real (coords serao substituidas)
    post_body: str | None      # POST body original (pode ser None se GET)
    user_agent: str
    captured_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.captured_at) > SESSION_TTL

    def build_url(self, lat: float, lon: float, page_size: int) -> str:
        parsed = urllib.parse.urlparse(self.bm_url_template)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params['latitude']  = str(lat)
        params['longitude'] = str(lon)
        params['size']      = str(page_size)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))

    def build_paginated_url(self, lat: float, lon: float,
                            section: str, cursor: str, alias: str,
                            page_size: int = 50) -> str:
        """URL para páginas adicionais do 'ver mais' (section + cursor)."""
        parsed = urllib.parse.urlparse(self.bm_url_template)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params['latitude']  = str(lat)
        params['longitude'] = str(lon)
        params['size']      = str(page_size)
        params['section']   = section
        params['cursor']    = cursor
        params['alias']     = alias
        # remove params que não fazem sentido em requests paginados
        params.pop('page', None)
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))


# ---------------------------------------------------------------------------
# Chrome management (apenas para captura de sessao)
# ---------------------------------------------------------------------------

def _clear_profile_locks():
    for f in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        (CHROME_PROFILE / f).unlink(missing_ok=True)


def _kill_previous_chrome():
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        except Exception:
            pass
        _PID_FILE.unlink(missing_ok=True)
    profile_name = CHROME_PROFILE.name
    subprocess.run([
        'powershell', '-Command',
        f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{profile_name}*'}} | "
        f"ForEach-Object {{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}",
    ], capture_output=True)
    _clear_profile_locks()


def _stop_chrome(pid: int | None):
    if pid is not None:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    _PID_FILE.unlink(missing_ok=True)
    _clear_profile_locks()


# ---------------------------------------------------------------------------
# Captura de sessao via browser
# ---------------------------------------------------------------------------

async def capture_session(headless: bool = False, proxy: str | None = None) -> Session:
    """Lanca browser, navega para iFood, captura token + URL + body, fecha browser."""
    print('[*] Iniciando browser para captura de sessao...')

    chrome_args = [
        '--lang=pt-BR',
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        '--exclude-switches=enable-automation',
        '--disable-infobars',
        '--window-size=1920,1080',
        '--start-minimized',
    ]
    if proxy:
        chrome_args.append(f'--proxy-server={proxy}')

    browser = await uc.start(
        user_data_dir=str(CHROME_PROFILE),
        headless=headless,
        lang='pt-BR',
        browser_args=chrome_args,
    )
    browser_pid = None
    try:
        browser_pid = browser.process.pid
        CHROME_PROFILE.mkdir(exist_ok=True)
        _PID_FILE.write_text(str(browser_pid))
    except AttributeError:
        pass

    tab = await browser.get('about:blank')
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=STEALTH_SCRIPT))
    await tab.send(cdp.network.enable())
    try:
        await tab.send(cdp.network.set_bypass_service_worker(bypass=True))
    except Exception:
        pass

    # Aguarda sessao valida
    print('[*] Verificando sessao...')
    await tab.get(HOME_URL)
    deadline = asyncio.get_event_loop().time() + 20.0
    while asyncio.get_event_loop().time() < deadline:
        cookies = await tab.send(cdp.network.get_cookies(urls=[f'https://{IFOOD_HOST}']))
        if any(c.name == 'aAccessToken' and c.value for c in cookies):
            break
        print('[*] Aguardando token...')
        await asyncio.sleep(2.0)
    else:
        try:
            browser.stop()
        except Exception:
            pass
        _stop_chrome(browser_pid)
        raise RuntimeError('Sessao invalida. Execute login.py primeiro.')

    print('[+] Token encontrado.')

    # Captura bm/home URL e POST body via CDP
    bm_url:   list[str | None] = [None]
    post_body: list[str | None] = [None]
    captured  = asyncio.Event()

    def on_request(event: cdp.network.RequestWillBeSent):
        url = event.request.url
        if 'bm/home' in url and not any(p in url.lower() for p in EXCLUDED_URL_PARTS):
            if not bm_url[0]:
                bm_url[0] = url
            pb = getattr(event.request, 'post_data', None)
            if pb and not post_body[0]:
                post_body[0] = pb
            captured.set()

    tab.add_handler(cdp.network.RequestWillBeSent, on_request)

    # Recarrega a pagina para disparar bm/home
    await tab.get(HOME_URL)
    await asyncio.sleep(random.uniform(1.5, 3.0))

    try:
        await asyncio.wait_for(captured.wait(), timeout=20.0)
    except asyncio.TimeoutError:
        print('[!] bm/home nao capturado na navegacao — usando URL generica')

    try:
        tab.handlers.get(cdp.network.RequestWillBeSent, []).remove(on_request)
    except Exception:
        pass

    # Extrai headers de auth dos cookies
    headers_raw = await tab.evaluate(_COOKIES_JS)
    headers     = json.loads(headers_raw or '{}')

    # Captura User-Agent
    user_agent = await tab.evaluate('navigator.userAgent') or (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )
    headers['user-agent'] = user_agent

    # Captura todos os cookies do browser — inclui _px3, _pxvid, _pxhd, pxcts (HUMAN Security)
    all_cdp_cookies = await tab.send(cdp.network.get_cookies(urls=[
        f'https://{IFOOD_HOST}',
        'https://www.ifood.com.br',
    ]))
    cookie_jar = {c.name: c.value for c in all_cdp_cookies if c.value}
    px_cookies = [k for k in cookie_jar if k.startswith('_px') or k == 'pxcts']
    print(f'[+] Cookies PX capturados: {px_cookies if px_cookies else "nenhum (sensor nao rodou?)"}')

    # iFood envia os cookies PX também no header x-px-cookies (além do cookie jar).
    # Sem esse header, o enforcement server-side detecta anomalia e retorna 403.
    _PX_HEADER_ORDER = ('_px3', '_pxhd', '_pxvid', 'pxcts')
    px_parts = [f'{k}={cookie_jar[k]}' for k in _PX_HEADER_ORDER if k in cookie_jar]
    if px_parts:
        headers['x-px-cookies'] = '; '.join(px_parts)
        print(f'[+] x-px-cookies header construido ({len(px_parts)} cookies)')

    url_template = bm_url[0] or 'https://www.ifood.com.br/site-api/v2/bm/home'

    try:
        browser.stop()
    except Exception:
        pass
    _stop_chrome(browser_pid)

    session = Session(
        headers=headers,
        cookies=cookie_jar,
        bm_url_template=url_template,
        post_body=post_body[0],
        user_agent=user_agent,
    )
    print(f'[+] Sessao capturada. URL base: {url_template.split("?")[0]}')
    print(f'[+] Auth: {"ok" if "Bearer ey" in headers.get("authorization", "") else "VAZIO"}')
    return session


# ---------------------------------------------------------------------------
# Request direto a bm/home
# ---------------------------------------------------------------------------

async def fetch_point(
    session: Session,
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    page_size: int,
) -> dict | None:
    url = session.build_url(lat, lon, page_size)
    try:
        if session.post_body:
            resp = await client.post(url, content=session.post_body,
                                     headers=session.headers, cookies=session.cookies)
        else:
            resp = await client.get(url, headers=session.headers, cookies=session.cookies)

        if resp.status_code in (401, 403):
            # 401 = aAccessToken expirado; 403 = _px3 inválido/ausente (HUMAN Security)
            print(f'  [warn] HTTP {resp.status_code} — sessao invalida (PX ou token)')
            return None

        if resp.status_code != 200:
            print(f'  [warn] HTTP {resp.status_code} em ({lat:.4f},{lon:.4f})')
            return None

        data = resp.json()
        body_s = json.dumps(data)
        if not data or not isinstance(data, dict):
            return None
        if 'sections' not in body_s and 'HOME_FOOD_DELIVERY' not in body_s:
            if len(MERCHANT_UUID_RE.findall(body_s)) < 3:
                return None
        return data

    except (httpx.TimeoutException, httpx.NetworkError) as e:
        print(f'  [erro] {e}')
        return None


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------

def extract_merchants(data: dict) -> list[dict]:
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
                    'id':               mid,
                    'name':             name,
                    'link':             link,
                    'category':         item.get('mainCategory', ''),
                    'rating':           item.get('userRating'),
                    'distance_km':      item.get('distance'),
                    'delivery_fee':     delivery.get('fee'),
                    'delivery_min_min': delivery.get('timeMinMinutes'),
                    'delivery_max_min': delivery.get('timeMaxMinutes'),
                    'image_url':        image_url,
                    'is_new':           item.get('isNew'),
                    'is_super':         item.get('isSuperRestaurant'),
                    'is_ifood_delivery': item.get('isIfoodDelivery'),
                    'available':        item.get('available'),
                })
    return found


def _extract_pagination(data: dict) -> tuple[str | None, str | None, str | None]:
    """Extrai (next_cursor, section_id, alias) do card NEXT_CONTENT para paginação.

    Estrutura real da API:
      section.cards[0] = MERCHANT_LIST_V2  (merchants desta página)
      section.cards[1] = NEXT_CONTENT      (action: "card-content?cursor=<TOKEN>")
    """
    for section in (data or {}).get('sections', []):
        sec_id       = section.get('id')
        has_merchants = False
        cursor        = None
        alias         = 'HOME_FOOD_DELIVERY_V3'

        for card in section.get('cards', []):
            ctype = card.get('cardType')
            if ctype == 'MERCHANT_LIST_V2':
                has_merchants = True
                # alias vive na action de cada merchant
                contents = (card.get('data') or {}).get('contents', [])
                if contents:
                    action = contents[0].get('action', '')
                    if 'alias=' in action:
                        parsed = urllib.parse.parse_qs(action.split('?', 1)[-1])
                        alias = parsed.get('alias', [alias])[0]
            elif ctype == 'NEXT_CONTENT':
                action = (card.get('data') or {}).get('action', '')
                # "card-content?cursor=<TOKEN>"
                if 'cursor=' in action:
                    parsed = urllib.parse.parse_qs(action.split('?', 1)[-1])
                    cursor = parsed.get('cursor', [None])[0]

        if has_merchants and cursor and sec_id:
            return cursor, sec_id, alias

    return None, None, None


async def fetch_all_pages(
    session: Session,
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    page_size: int,
) -> list[dict] | None:
    """Busca todos os restaurantes de um ponto paginando o 'ver mais'.

    Retorna None em caso de falha de auth/rede (sinaliza renovação de sessão).
    Retorna lista vazia se o ponto não tem cobertura.
    """
    # Página inicial
    data = await fetch_point(session, client, lat, lon, page_size)
    if data is None:
        return None

    all_merchants = extract_merchants(data)
    cursor, section_id, alias = _extract_pagination(data)

    page_num = 1
    while cursor and section_id:
        page_num += 1
        url = session.build_paginated_url(lat, lon, section_id, cursor, alias)
        try:
            if session.post_body:
                resp = await client.post(url, content=session.post_body,
                                         headers=session.headers, cookies=session.cookies)
            else:
                resp = await client.get(url, headers=session.headers, cookies=session.cookies)

            if resp.status_code in (401, 403):
                print(f'  [pag] HTTP {resp.status_code} na pág {page_num} — sessao invalida')
                return None
            if resp.status_code != 200:
                print(f'  [pag] HTTP {resp.status_code} na pág {page_num} — encerrando')
                break

            page_data = resp.json()
            page_merchants = extract_merchants(page_data)
            if not page_merchants:
                break  # sem mais resultados

            all_merchants.extend(page_merchants)
            cursor, _, _ = _extract_pagination(page_data)

        except (httpx.TimeoutException, httpx.NetworkError) as e:
            print(f'  [pag] erro na pág {page_num}: {e}')
            break

    if page_num > 1:
        print(f'  [pag] {page_num} páginas → {len(all_merchants)} merchants brutos')

    return all_merchants


def filter_new(merchants: list[dict], seen: set) -> list[dict]:
    new = [m for m in merchants if m['id'] not in seen]
    seen.update(m['id'] for m in new)
    return new


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    city: str,
    step_km: float | None,
    delay: float,
    headless: bool,
    max_points: int = 0,
    page_size: int = 100,
    use_boundary: bool = False,
    proxy: str | None = None,
):
    city_cfg  = CITIES[city]
    points    = generate_city_grid(city, step_km=step_km, use_boundary=use_boundary)
    used_step = step_km if step_km is not None else city_cfg['step_km']
    if max_points:
        points = points[:max_points]
    print(f'[*] Cidade: {city_cfg["name"]}')
    print(f'[*] Grade {used_step} km: {len(points)} pontos')
    print(f'[*] Delay: {delay}s (+-20% jitter)')

    ts             = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir        = Path(__file__).parent.parent / 'captures' / f'crawl_api_{city}_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)
    merchants_path = out_dir / 'merchants.jsonl'
    log_path       = out_dir / 'crawl.log'
    points_path    = out_dir / 'points.jsonl'

    print(f'[*] Salvando em: {out_dir}\n')

    _kill_previous_chrome()
    await asyncio.sleep(0.3)
    CHROME_PROFILE.mkdir(exist_ok=True)

    session = await capture_session(headless=headless, proxy=proxy)

    _start_skip_listener()
    print('[*] Em caso de erro: aguarda e retenta. Digite "s" + Enter para pular o ponto atual.\n')

    seen_ids  = set()
    total_new = 0
    errors    = 0

    transport = httpx.AsyncHTTPTransport(retries=2)
    timeout   = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        with open(merchants_path, 'w', encoding='utf-8') as mf, \
             open(log_path,       'w', encoding='utf-8') as lf, \
             open(points_path,    'w', encoding='utf-8') as pf:

            lf.write(f'Cidade: {city_cfg["name"]}\n')
            lf.write(f'Inicio: {datetime.now()}\n')
            lf.write(f'Pontos: {len(points)}\n\n')

            for i, (lat, lon) in enumerate(points, 1):
                # Renova sessao proativamente se expirou
                if session.is_expired():
                    msg = '[*] Renovando sessao (TTL atingido)...'
                    print(msg); lf.write(msg + '\n'); lf.flush()
                    _kill_previous_chrome()
                    await asyncio.sleep(0.3)
                    session = await capture_session(headless=headless, proxy=proxy)
                    errors  = 0

                # Retry loop — tenta até funcionar ou usuário pular
                retry    = 0
                skipped  = False
                all_here = None
                while True:
                    _skip_event.clear()
                    all_here = await fetch_all_pages(session, client, lat, lon, page_size)
                    if all_here is not None:
                        errors = 0
                        break

                    retry  += 1
                    errors += 1

                    if errors >= 3:
                        msg = f'[!] {errors} erros consecutivos — renovando sessao...'
                        print(msg); lf.write(msg + '\n'); lf.flush()
                        _kill_previous_chrome()
                        await asyncio.sleep(0.3)
                        session = await capture_session(headless=headless, proxy=proxy)
                        errors  = 0

                    backoff = min(5 * retry, 60)
                    msg = (f'  [!] Tentativa {retry} falhou — '
                           f'retentando em {backoff}s... [s + Enter para pular]')
                    print(msg); lf.write(msg + '\n'); lf.flush()
                    skipped = await _interruptible_sleep(backoff)
                    if skipped:
                        msg = f'  [->] Ponto ({lat:.4f},{lon:.4f}) pulado manualmente.'
                        print(msg); lf.write(msg + '\n'); lf.flush()
                        break

                if skipped:
                    line = f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) PULADO'
                    pf.write(json.dumps({
                        'index': i, 'lat': lat, 'lon': lon,
                        'count': 0, 'total': total_new, 'names': [],
                        'error': True, 'skipped': True,
                    }, ensure_ascii=False) + '\n')
                    pf.flush()

                else:
                    new_here = filter_new(all_here, seen_ids)
                    total_new += len(new_here)

                    for m in new_here:
                        mf.write(json.dumps({**m, 'lat': lat, 'lon': lon},
                                            ensure_ascii=False) + '\n')
                    mf.flush()

                    pf.write(json.dumps({
                        'index': i, 'lat': lat, 'lon': lon,
                        'count': len(all_here), 'total': total_new,
                        'names': [m['name'] for m in all_here[:10]],
                        'error': False,
                    }, ensure_ascii=False) + '\n')
                    pf.flush()

                    line = (f'[{i:4d}/{len(points)}] ({lat:.4f},{lon:.4f}) '
                            f'novos={len(new_here)} total={total_new}')
                    if new_here:
                        preview = ', '.join(m['name'] for m in new_here[:3])
                        extra   = f' (+{len(new_here)-3})' if len(new_here) > 3 else ''
                        line   += f'\n  -> {preview}{extra}'

                print(line); lf.write(line + '\n'); lf.flush()
                await asyncio.sleep(delay * random.uniform(0.8, 1.2))

            lf.write(f'\nFim: {datetime.now()}\nTotal merchants unicos: {total_new}\n')

    print(f'\n[+] Concluido. Merchants unicos: {total_new}')
    print(f'[+] Merchants: {merchants_path}')
    print(f'[+] Log:       {log_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Crawler iFood API direto (sem browser por request) — multi-cidade'
    )
    parser.add_argument('--city',        default='sao-paulo',
                        help='Cidade a crawlear (default: sao-paulo)')
    parser.add_argument('--list-cities', action='store_true',
                        help='Lista cidades disponíveis e sai')
    parser.add_argument('--step',        type=float, default=None,
                        help='Espacamento da grade em km (None = usa padrao da cidade)')
    parser.add_argument('--delay',       type=float, default=2.0,
                        help='Delay base entre requests (s) (default 2.0)')
    parser.add_argument('--headless',    action='store_true',
                        help='Captura de sessao sem janela')
    parser.add_argument('--max-points',  type=int,   default=0,
                        help='Limite de pontos (0 = sem limite)')
    parser.add_argument('--page-size',   type=int,   default=100,
                        help='Restaurantes por ponto da grade (default 100)')
    parser.add_argument('--boundary',    action='store_true',
                        help='Filtrar grade pelo poligono real do municipio via OSM')
    parser.add_argument('--proxy',       default=None,
                        help='Proxy HTTP/SOCKS5 (ex: http://user:pass@host:port)')
    args = parser.parse_args()

    if args.list_cities:
        print(f"\n{'Chave':<20} {'Cidade':<28} {'Step padrao':<14} {'Pontos aprox.'}")
        print('-' * 72)
        from configs.cities import generate_city_grid as _gcg
        for key, cfg in CITIES.items():
            n = len(_gcg(key))
            print(f"  {key:<18} {cfg['name']:<28} {cfg['step_km']:<14.1f} ~{n}")
        print()
        sys.exit(0)

    if args.city not in CITIES:
        print(f"[!] Cidade '{args.city}' nao encontrada. Use --list-cities.")
        sys.exit(1)

    uc.loop().run_until_complete(main(
        args.city, args.step, args.delay, args.headless,
        args.max_points, args.page_size, args.boundary, args.proxy,
    ))
