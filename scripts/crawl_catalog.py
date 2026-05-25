"""
Crawler de catálogos iFood — API direta (sem browser por restaurante).

Lê merchants.jsonl de uma captura anterior (crawl_api.py), captura sessão
uma única vez via browser e busca o catálogo de cada restaurante via httpx.

Endpoint: GET /site-api/v1/merchants/restaurant/{UUID}/catalog?latitude=...&longitude=...
Headers extras: access_key, secret_key (interceptados do browser, fallback hardcoded)

Uso:
    python scripts/crawl_catalog.py --capture-dir captures/crawl_api_sao-joao-del-rei_20260522_120000
    python scripts/crawl_catalog.py captures/crawl_api_*/merchants.jsonl
    python scripts/crawl_catalog.py --capture-dir captures/... --delay 2 --max 50 --headless

Saida em {capture_dir}/{nome}-{uuid}/:
    catalog.json    — resposta bruta da API + metadados
    products.jsonl  — um produto por linha com preco em centavos e BRL
"""

import asyncio
import argparse
import base64
import ctypes
import json
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import nodriver as uc
from nodriver import cdp

IFOOD_HOST     = 'www.ifood.com.br'
CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
_PID_FILE      = CHROME_PROFILE / '.crawler_pid'
HOME_URL       = 'https://www.ifood.com.br/restaurantes'

SESSION_TTL = 45 * 60

# Fallback hardcoded — capturados via DevTools em 2026-05-22
_DEFAULT_ACCESS_KEY = '69f181d5-0046-4221-b7b2-deef62bd60d5'
_DEFAULT_SECRET_KEY = '9ef4fb4f-7a1d-4e0d-a9b1-9b82873297d8'

STEALTH_SCRIPT = r"""
(function() {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: true,
    });
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });
    Object.defineProperty(Navigator.prototype, 'languages',           {get: () => ['pt-BR','pt','en-US','en'], configurable: true});
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {get: () => 8,       configurable: true});
    Object.defineProperty(Navigator.prototype, 'deviceMemory',        {get: () => 8,       configurable: true});
    Object.defineProperty(Navigator.prototype, 'platform',            {get: () => 'Win32', configurable: true});
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
    window.chrome = {
        app: {isInstalled: false},
        runtime: {connect: () => {}, sendMessage: () => {}, id: undefined, OnInstalledReason: {}},
        loadTimes: function() { return {}; },
        csi: function() { return {}; },
    };
    try { Object.defineProperty(Notification, 'permission', {get: () => 'default'}); } catch(e) {}
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
        authorization:              'Bearer ' + d(c['aAccessToken']),
        'x-ifood-device-id':        d(c['aDeviceId']),
        'x-ifood-session-id':       d(c['aSessionId']),
        'x-ifood-user-id':          d(c['aAccountId']),
        'x-client-application-key': d(c['aFasterAppKey']),
        account_id:                 d(c['aAccountId']),
        app_version:                d(c['aAppVersion']) || '9.141.4',
        platform:                   'Desktop',
        browser:                    'Windows',
        country:                    'BR',
        'content-type':             'application/json',
        'accept':                   'application/json, text/plain, */*',
        'accept-language':          'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'origin':                   'https://www.ifood.com.br',
        'referer':                  'https://www.ifood.com.br/restaurantes',
    });
})()
"""

_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------

@dataclass
class CatalogSession:
    headers: dict
    cookies: dict
    access_key: str
    secret_key: str
    user_agent: str
    captured_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return (time.time() - self.captured_at) > SESSION_TTL

    def build_url(self, merchant_id: str, lat: float, lon: float) -> str:
        return (
            f'https://www.ifood.com.br/site-api/v1/merchants/restaurant'
            f'/{merchant_id}/catalog?latitude={lat}&longitude={lon}&channel=IFOOD'
        )

    def request_headers(self, referer: str = '') -> dict:
        h = {**self.headers, 'access_key': self.access_key, 'secret_key': self.secret_key}
        if referer:
            h['referer'] = referer
        return h


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return _FORBIDDEN_RE.sub('-', name).strip().strip('.')[:80]


def store_folder(base_dir: Path, merchant_id: str, merchant_name: str) -> Path:
    return base_dir / f"{_safe_name(merchant_name)}-{merchant_id}"


def is_done(folder: Path) -> bool:
    return (folder / 'catalog.json').exists()


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------

def extract_products(catalog_data: dict) -> list[dict]:
    products = []
    for category in (catalog_data.get('catalog') or []):
        cat_name = (category.get('description') or category.get('name') or '').strip()
        for item in (category.get('itens') or category.get('items') or []):
            price     = item.get('unitPrice')
            price_min = item.get('unitMinPrice') if item.get('unitMinPrice') is not None else price
            products.append({
                'id':              item.get('code') or item.get('id'),
                'name':            (item.get('description') or item.get('name') or '').strip(),
                'description':     (item.get('details') or '').strip(),
                'category':        cat_name,
                'price_cents':     price,
                'price_brl':       round(price / 100, 2) if price is not None else None,
                'price_min_cents': price_min,
                'price_min_brl':   round(price_min / 100, 2) if price_min is not None else None,
                'serving':         (item.get('serving') or '').strip(),
                'available':       item.get('available'),
                'need_choices':    item.get('needChoices'),
                'logo_url':        item.get('logoUrl') or item.get('imageUrl') or '',
            })
    return products


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------

def _clear_profile_locks():
    for f in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        (CHROME_PROFILE / f).unlink(missing_ok=True)


def _stop_chrome(pid: int | None):
    if pid is not None:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    _PID_FILE.unlink(missing_ok=True)
    _clear_profile_locks()


# ---------------------------------------------------------------------------
# Leitura de cookies do Chrome real (Windows DPAPI + AES-GCM)
# ---------------------------------------------------------------------------

def _dpapi_decrypt(data: bytes) -> bytes:
    class _BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    b_in = _BLOB(len(data), buf)
    b_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(b_in), None, None, None, None, 0, ctypes.byref(b_out)
    )
    if not ok:
        raise RuntimeError('DPAPI decrypt failed')
    result = ctypes.string_at(b_out.pbData, b_out.cbData)
    ctypes.windll.kernel32.LocalFree(b_out.pbData)
    return result


def _read_real_chrome_cookies() -> list[dict]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return []
    user_data = Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data'
    local_state_path = user_data / 'Local State'
    cookies_db = user_data / 'Default' / 'Network' / 'Cookies'
    if not cookies_db.exists():
        cookies_db = user_data / 'Default' / 'Cookies'
    if not local_state_path.exists() or not cookies_db.exists():
        return []
    try:
        local_state = json.loads(local_state_path.read_text(encoding='utf-8'))
        enc_key = base64.b64decode(local_state['os_crypt']['encrypted_key'])[5:]
        aes_key = _dpapi_decrypt(enc_key)
    except Exception:
        return []
    def _decrypt(enc: bytes) -> str:
        if not enc:
            return ''
        if enc[:3] == b'v10':
            try:
                return AESGCM(aes_key).decrypt(enc[3:15], enc[15:], None).decode('utf-8', errors='replace')
            except Exception:
                return ''
        try:
            return _dpapi_decrypt(enc).decode('utf-8', errors='replace')
        except Exception:
            return ''
    _SAMESITE = {-1: 'Lax', 0: 'None', 1: 'Lax', 2: 'Strict'}
    tmp = Path(tempfile.mktemp(suffix='.db'))
    try:
        shutil.copy2(cookies_db, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            'SELECT name, value, encrypted_value, host_key, path, '
            'is_secure, is_httponly, samesite, expires_utc '
            'FROM cookies WHERE host_key LIKE "%ifood.com.br%"'
        ).fetchall()
        conn.close()
    except Exception:
        return []
    finally:
        tmp.unlink(missing_ok=True)
    result = []
    for name, value, enc_value, host, path, secure, http_only, samesite, expires_utc in rows:
        v = value or _decrypt(enc_value)
        if not v:
            continue
        expires = int(expires_utc / 1_000_000 - 11_644_473_600) if expires_utc > 0 else -1
        result.append({
            'name': name, 'value': v,
            'domain': host, 'path': path,
            'secure': bool(secure), 'httpOnly': bool(http_only),
            'sameSite': _SAMESITE.get(samesite, 'Lax'),
            'expires': expires,
        })
    return result


async def _inject_real_chrome_cookies(tab) -> None:
    cookies = _read_real_chrome_cookies()
    if not cookies:
        print('[*] Sem cookies do Chrome real para injetar')
        return
    injected = 0
    for c in cookies:
        try:
            sm = c['sameSite']
            same_site_val = cdp.network.CookieSameSite(sm) if sm in ('Strict', 'Lax', 'None') else None
            kwargs = dict(
                name=c['name'], value=c['value'],
                domain=c['domain'], path=c['path'],
                secure=c['secure'], http_only=c['httpOnly'],
            )
            if same_site_val is not None:
                kwargs['same_site'] = same_site_val
            if c['expires'] > 0:
                kwargs['expires'] = cdp.network.TimeSinceEpoch(c['expires'])
            await tab.send(cdp.network.set_cookie(**kwargs))
            injected += 1
        except Exception:
            pass
    px = [c['name'] for c in cookies if c['name'].startswith('_px') or c['name'] == 'pxcts']
    print(f'[+] {injected}/{len(cookies)} cookies do Chrome real injetados '
          f'(PX: {px if px else "nenhum"})')


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


# ---------------------------------------------------------------------------
# Session capture (browser uma vez)
# ---------------------------------------------------------------------------

def _is_catalog_url(url: str) -> bool:
    return '/merchants/' in url and '/catalog' in url and 'customers' not in url


async def capture_session(
    headless: bool = False,
    proxy: str | None = None,
    first_merchant: dict | None = None,
) -> CatalogSession:
    """Lança browser, verifica sessão, intercepta access_key/secret_key, fecha browser."""
    print('[*] Iniciando browser para captura de sessão...')

    chrome_args = [
        '--lang=pt-BR',
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        '--exclude-switches=enable-automation',
        '--disable-infobars',
        '--window-size=1280,900',
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

    # Injeta cookies do Chrome real antes de navegar — _pxvid ja conhecido pelo HUMAN Security
    await _inject_real_chrome_cookies(tab)

    print('[*] Verificando sessão...')
    try:
        await asyncio.wait_for(tab.get(HOME_URL), timeout=30.0)
    except asyncio.TimeoutError:
        print('[*] Timeout na navegação — verificando cookies mesmo assim...')

    token_found = False
    for _attempt in range(10):  # máx 10 × 2s = 20s
        try:
            cookies = await asyncio.wait_for(
                tab.send(cdp.network.get_cookies(urls=[f'https://{IFOOD_HOST}'])),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            cookies = []
        if any(c.name == 'aAccessToken' and c.value for c in cookies):
            token_found = True
            break
        print('[*] Aguardando token...')
        await asyncio.sleep(2.0)

    if not token_found:
        try:
            browser.stop()
        except Exception:
            pass
        _stop_chrome(browser_pid)
        raise RuntimeError('Sessão inválida. Execute login.py primeiro.')

    print('[+] Token encontrado.')

    # Tenta interceptar access_key/secret_key navegando para uma loja real
    access_key: list[str] = [_DEFAULT_ACCESS_KEY]
    secret_key: list[str] = [_DEFAULT_SECRET_KEY]
    keys_captured = asyncio.Event()

    def on_request(event: cdp.network.RequestWillBeSent):
        url = event.request.url
        if _is_catalog_url(url):
            hdrs = {k.lower(): v for k, v in (event.request.headers or {}).items()}
            ak = hdrs.get('access_key')
            sk = hdrs.get('secret_key')
            if ak:
                access_key[0] = ak
            if sk:
                secret_key[0] = sk
            if ak or sk:
                keys_captured.set()

    tab.add_handler(cdp.network.RequestWillBeSent, on_request)

    warmup_url = first_merchant.get('link', '') if first_merchant else ''
    if warmup_url:
        print(f'[*] Navegando para loja para interceptar access_key...')
        try:
            await asyncio.wait_for(tab.get(warmup_url), timeout=30.0)
        except asyncio.TimeoutError:
            print('[*] Timeout na navegação da loja — continuando...')
        await asyncio.sleep(random.uniform(2.0, 4.0))
        try:
            await asyncio.wait_for(keys_captured.wait(), timeout=15.0)
            print(f'[+] access_key/secret_key interceptados via browser')
        except asyncio.TimeoutError:
            print('[!] Timeout — usando access_key/secret_key hardcoded')
    else:
        await asyncio.sleep(random.uniform(1.5, 3.0))
        print('[!] Sem URL de loja para warm-up — usando access_key/secret_key hardcoded')

    try:
        tab.handlers.get(cdp.network.RequestWillBeSent, []).remove(on_request)
    except Exception:
        pass

    headers_raw = await tab.evaluate(_COOKIES_JS)
    headers     = json.loads(headers_raw or '{}')

    user_agent = await tab.evaluate('navigator.userAgent') or (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )
    headers['user-agent'] = user_agent

    all_cdp_cookies = await tab.send(cdp.network.get_cookies(urls=[
        f'https://{IFOOD_HOST}',
        'https://www.ifood.com.br',
    ]))
    cookie_jar = {c.name: c.value for c in all_cdp_cookies if c.value}
    px_cookies = [k for k in cookie_jar if k.startswith('_px') or k == 'pxcts']
    print(f'[+] Cookies PX: {px_cookies if px_cookies else "nenhum (sensor não rodou?)"}')

    _PX_HEADER_ORDER = ('_px3', '_pxhd', '_pxvid', 'pxcts')
    px_parts = [f'{k}={cookie_jar[k]}' for k in _PX_HEADER_ORDER if k in cookie_jar]
    if px_parts:
        headers['x-px-cookies'] = '; '.join(px_parts)
        print(f'[+] x-px-cookies construído ({len(px_parts)} cookies)')

    try:
        browser.stop()
    except Exception:
        pass
    _stop_chrome(browser_pid)

    session = CatalogSession(
        headers=headers,
        cookies=cookie_jar,
        access_key=access_key[0],
        secret_key=secret_key[0],
        user_agent=user_agent,
    )
    print(f'[+] Sessão pronta. access_key={session.access_key[:8]}...')
    print(f'[+] Auth: {"ok" if "Bearer ey" in headers.get("authorization", "") else "VAZIO"}')
    return session


# ---------------------------------------------------------------------------
# Fetch catalog via httpx
# ---------------------------------------------------------------------------

_EMPTY = object()  # sentinela — 404/loja inativa

async def fetch_catalog(
    session: CatalogSession,
    client: httpx.AsyncClient,
    merchant: dict,
) -> dict | None:
    """
    Retorna:
      dict  — catálogo com dados
      _EMPTY — 404 ou loja inativa (não deve ser retentado)
      None  — falha de auth ou rede (indica renovação de sessão)
    """
    mid = merchant['id']
    lat = merchant.get('lat', -23.5489)
    lon = merchant.get('lon', -46.6333)

    url     = session.build_url(mid, lat, lon)
    referer = merchant.get('link', '')
    hdrs    = session.request_headers(referer=referer)

    try:
        resp = await client.get(url, headers=hdrs, cookies=session.cookies)

        if resp.status_code in (401, 403):
            print(f'  [warn] HTTP {resp.status_code} — sessão inválida (PX ou token)')
            return None

        if resp.status_code == 404:
            return _EMPTY

        if resp.status_code != 200:
            print(f'  [warn] HTTP {resp.status_code}')
            return None

        data = resp.json()
        return data if (data and isinstance(data, dict)) else _EMPTY

    except (httpx.TimeoutException, httpx.NetworkError) as e:
        print(f'  [erro] {e}')
        return None


# ---------------------------------------------------------------------------
# Merchant loading
# ---------------------------------------------------------------------------

def load_merchants(files: list[Path]) -> list[dict]:
    seen = {}
    for f in files:
        if not f.exists():
            print(f'[!] Arquivo não encontrado: {f}')
            continue
        for line in f.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                mid = m.get('id')
                if mid and mid not in seen:
                    seen[mid] = m
            except Exception:
                pass
    return list(seen.values())


# ---------------------------------------------------------------------------
# Skip listener (retry loop)
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


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def _save_catalog_meta(output_dir: Path, merchant_files: list[Path],
                        delay: float, max_merchants: int, proxy: str | None) -> None:
    meta = {
        'merchant_files': [str(f.resolve()) for f in merchant_files],
        'output_dir':     str(output_dir.resolve()),
        'delay':          delay,
        'max_merchants':  max_merchants,
        'proxy':          proxy,
        'started_at':     datetime.now().isoformat(),
    }
    (output_dir / 'catalog_meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def _load_catalog_meta(resume_dir: Path) -> tuple[list[Path], Path, float, int, str | None]:
    meta_file = resume_dir / 'catalog_meta.json'
    if not meta_file.exists():
        raise FileNotFoundError(
            f'catalog_meta.json não encontrado em {resume_dir}.\n'
            'Apenas capturas iniciadas após esta versão suportam retomada.'
        )
    meta = json.loads(meta_file.read_text(encoding='utf-8'))
    return (
        [Path(f) for f in meta['merchant_files']],
        Path(meta['output_dir']),
        meta['delay'],
        meta['max_merchants'],
        meta.get('proxy'),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    merchant_files: list[Path],
    output_dir: Path,
    delay: float,
    headless: bool,
    max_merchants: int = 0,
    proxy: str | None = None,
    resuming: bool = False,
):
    merchants = load_merchants(merchant_files)
    if not merchants:
        print('[!] Nenhum merchant encontrado.')
        return

    if max_merchants:
        merchants = merchants[:max_merchants]

    todo = [m for m in merchants if not is_done(store_folder(output_dir, m['id'], m['name']))]
    done_count = len(merchants) - len(todo)

    print(f'[*] {len(merchants)} merchants, {len(todo)} sem catálogo'
          + (f' ({done_count} já feitos)' if done_count else ''))
    if not todo:
        print('[+] Todos os catálogos já foram capturados.')
        return

    if not resuming:
        _save_catalog_meta(output_dir, merchant_files, delay, max_merchants, proxy)

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f'[*] Salvando em: {output_dir}')
    print(f'[*] Delay: {delay}s (+-20% jitter)')

    _start_skip_listener()
    print('[*] Em caso de erro: aguarda e retenta. Digite "s" + Enter para pular a loja.\n')

    _kill_previous_chrome()
    await asyncio.sleep(0.3)
    CHROME_PROFILE.mkdir(exist_ok=True)

    session = await capture_session(
        headless=headless,
        proxy=proxy,
        first_merchant=todo[0],
    )

    ok_count    = 0
    empty_count = 0
    fail_count  = 0
    errors      = 0

    transport = httpx.AsyncHTTPTransport(retries=2)
    timeout   = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        for i, merchant in enumerate(todo, done_count + 1):
            name_preview = merchant['name'][:50]
            print(f'[{i:4d}/{len(merchants)}] {name_preview}')

            if session.is_expired():
                print('[*] Renovando sessão (TTL atingido)...')
                _kill_previous_chrome()
                await asyncio.sleep(0.3)
                session = await capture_session(headless=headless, proxy=proxy)
                errors = 0

            folder = store_folder(output_dir, merchant['id'], merchant['name'])

            # Retry loop — tenta até funcionar ou usuário pular
            retry   = 0
            skipped = False
            result  = None
            while True:
                _skip_event.clear()
                result = await fetch_catalog(session, client, merchant)
                if result is not None:
                    errors = 0
                    break

                retry  += 1
                errors += 1

                if errors >= 3:
                    print(f'[!] {errors} erros consecutivos — renovando sessão...')
                    _kill_previous_chrome()
                    await asyncio.sleep(0.3)
                    session = await capture_session(headless=headless, proxy=proxy)
                    errors = 0

                backoff = min(5 * retry, 60)
                print(f'  [!] Tentativa {retry} falhou — '
                      f'retentando em {backoff}s... [s + Enter para pular]')
                skipped = await _interruptible_sleep(backoff)
                if skipped:
                    print(f'  [->] {merchant["name"][:50]} pulado manualmente.')
                    break

            if skipped:
                fail_count += 1

            elif result is _EMPTY:
                empty_count += 1
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'catalog.json').write_text(
                    json.dumps({
                        'merchant_id':   merchant['id'],
                        'merchant_name': merchant['name'],
                        'crawled_at':    datetime.now().isoformat(),
                        'catalog':       None,
                    }, ensure_ascii=False, indent=2), encoding='utf-8'
                )
                print('  -> vazio (404 / loja inativa)')

            else:
                ok_count += 1
                folder.mkdir(parents=True, exist_ok=True)

                (folder / 'catalog.json').write_text(
                    json.dumps({
                        'merchant_id':   merchant['id'],
                        'merchant_name': merchant['name'],
                        'merchant_link': merchant.get('link', ''),
                        'crawled_at':    datetime.now().isoformat(),
                        'catalog':       result,
                    }, ensure_ascii=False, indent=2), encoding='utf-8'
                )

                products = extract_products(result)
                if products:
                    with open(folder / 'products.jsonl', 'w', encoding='utf-8') as pf:
                        for p in products:
                            pf.write(json.dumps(p, ensure_ascii=False) + '\n')

                print(f'  -> {len(products)} itens  [{folder.name[:60]}]')

            await asyncio.sleep(delay * random.uniform(0.8, 1.2))

    print(f'\n[+] Concluído: {ok_count} ok, {empty_count} vazios, {fail_count} falhas')
    print(f'[+] Lojas em: {output_dir}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler de catálogos iFood — API direta')
    parser.add_argument(
        '--capture-dir', type=Path, metavar='DIR',
        help='Diretório de uma captura existente (lê merchants.jsonl e salva lojas lá dentro)',
    )
    parser.add_argument(
        'merchants', type=Path, nargs='*',
        help='Um ou mais arquivos merchants.jsonl',
    )
    parser.add_argument('--out',      type=Path,  help='Diretório de saída (padrão: mesmo do merchants.jsonl)')
    parser.add_argument('--delay',    type=float, default=2.0,  help='Delay base em segundos (default 2)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    parser.add_argument('--max',      type=int,   default=0,     help='Máx N lojas (0 = todas)')
    parser.add_argument('--proxy',    default=None,              help='Proxy HTTP/SOCKS5')
    parser.add_argument('--resume',   type=Path,  default=None, metavar='DIR',
                        help='Retoma uma captura de catálogos anterior a partir do diretório informado')
    args = parser.parse_args()

    if args.resume:
        if not args.resume.exists():
            print(f'[!] Diretório não encontrado: {args.resume}')
            sys.exit(1)
        merchant_files, output_dir, delay, max_merchants, proxy = _load_catalog_meta(args.resume)
        print(f'[*] Retomando catálogo: {args.resume}')
        uc.loop().run_until_complete(
            main(merchant_files, output_dir, delay, args.headless, max_merchants, proxy, resuming=True)
        )
    elif args.capture_dir:
        merchants_file = args.capture_dir / 'merchants.jsonl'
        if not merchants_file.exists():
            print(f'[!] {merchants_file} não encontrado.')
            sys.exit(1)
        output_dir     = args.capture_dir
        merchant_files = [merchants_file]
        uc.loop().run_until_complete(
            main(merchant_files, output_dir, args.delay, args.headless, args.max, args.proxy)
        )
    elif args.merchants:
        merchant_files = args.merchants
        output_dir     = args.out if args.out else merchant_files[0].parent
        output_dir.mkdir(parents=True, exist_ok=True)
        uc.loop().run_until_complete(
            main(merchant_files, output_dir, args.delay, args.headless, args.max, args.proxy)
        )
    else:
        parser.error('Especifique --resume DIR, --capture-dir PATH ou passe arquivos merchants.jsonl')
