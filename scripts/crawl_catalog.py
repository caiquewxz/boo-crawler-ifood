"""
Crawler de catálogos iFood — captura menu completo de cada restaurante.

Para cada merchant em merchants.jsonl:
  - Navega à página da loja no iFood
  - Intercepta a chamada /merchants/{uuid}/catalog via CDP (3 estratégias)
  - Salva em {capture_dir}/{uuid}-{nome}/
      catalog.json    — resposta bruta da API + metadados
      products.jsonl  — um produto por linha com preço em centavos e BRL

Uso:
    python scripts/crawl_catalog.py --capture-dir captures/crawl_nd_sao-paulo_20260520_120000
    python scripts/crawl_catalog.py --capture-dir captures/...  --delay 8 --headless
    python scripts/crawl_catalog.py --capture-dir captures/...  --max 50

    # Legado: passar arquivos merchants.jsonl diretamente
    python scripts/crawl_catalog.py captures/crawl_nd_*/merchants.jsonl --catalogs-dir catalogs/
"""

import asyncio
import argparse
import base64
import json
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import nodriver as uc
from nodriver import cdp

IFOOD_HOST     = 'www.ifood.com.br'
CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
_PID_FILE      = CHROME_PROFILE / '.crawler_pid'

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
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
})();
"""

CATALOG_CAPTURE_SCRIPT = r"""
(function() {
    if (window.__ifCatInstalled) return;
    window.__ifCatInstalled = true;
    window.__ifCatResult = null;
    const _origFetch = window.fetch;
    window.fetch = function() {
        const args = Array.from(arguments);
        let url = '';
        if (typeof args[0] === 'string') url = args[0];
        else if (args[0] && typeof args[0].url === 'string') url = args[0].url;
        if (url.indexOf('/merchants/') !== -1 && url.indexOf('/catalog') !== -1) {
            var promise = _origFetch.apply(this, args);
            promise.then(function(r) {
                var s = r.status;
                return r.clone().json().then(function(d) {
                    if (!window.__ifCatResult)
                        window.__ifCatResult = JSON.stringify({status: s, data: d, url: url});
                });
            }).catch(function() {});
            return promise;
        }
        return _origFetch.apply(this, args);
    };
})();
"""

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
        'platform':               'Desktop',
        'browser':                'Windows',
        'country':                'BR',
    });
})()
"""

_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Remove caracteres inválidos em nomes de pasta Windows, limita a 80 chars."""
    return _FORBIDDEN_RE.sub('-', name).strip().strip('.')[:80]


def store_folder(base_dir: Path, merchant_id: str, merchant_name: str) -> Path:
    """Retorna o Path da pasta da loja: {base_dir}/{uuid}-{nome}."""
    return base_dir / f"{merchant_id}-{_safe_name(merchant_name)}"


def is_done(folder: Path) -> bool:
    return (folder / 'catalog.json').exists()


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------

def extract_products(catalog_data: dict) -> list[dict]:
    """
    Extrai lista plana de produtos do response de catálogo iFood.

    Suporta o formato marketplace v1/v2:
      catalog[].itens[] — cada item tem unitPrice em centavos

    Retorna lista de dicts prontos para serializar em products.jsonl.
    """
    products = []

    for category in (catalog_data.get('catalog') or []):
        cat_name = (category.get('description') or category.get('name') or '').strip()
        for item in (category.get('itens') or category.get('items') or []):
            price = item.get('unitPrice')
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


def _count_catalog_items(catalog_data: dict) -> int:
    return len(extract_products(catalog_data))


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------

def _clear_profile_locks():
    for f in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
        (CHROME_PROFILE / f).unlink(missing_ok=True)


def _stop_our_chrome(pid: int | None):
    if pid is not None:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
    _PID_FILE.unlink(missing_ok=True)
    _clear_profile_locks()


def _kill_previous_crawler_chrome():
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
# Catalog page capture (3-strategy fallback)
# ---------------------------------------------------------------------------

def is_catalog_url(url: str) -> bool:
    return (
        '/merchants/' in url
        and '/catalog' in url
        and not any(x in url for x in ('customers', 'wallet', 'reviews'))
    )


async def crawl_catalog_page(tab, merchant: dict, timeout: float = 40.0) -> dict | None:
    mid          = merchant['id']
    delivery_url = merchant.get('link', '')
    lat          = merchant.get('lat', -23.5489)
    lon          = merchant.get('lon', -46.6333)

    if not delivery_url:
        return None

    pending_resp: dict[str, tuple[str, int]] = {}
    captured: dict = {}
    cdp_done = asyncio.Event()

    def on_request(event: cdp.network.RequestWillBeSent):
        url = event.request.url
        if is_catalog_url(url):
            pending_resp[str(event.request_id)] = (url, 0)

    def on_response(event: cdp.network.ResponseReceived):
        rid = str(event.request_id)
        if rid in pending_resp:
            url = pending_resp[rid][0]
            pending_resp[rid] = (url, event.response.status)

    async def on_loading_finished(event: cdp.network.LoadingFinished):
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
            if data and isinstance(data, dict):
                captured.update({'url': url, 'data': data})
                cdp_done.set()
        except Exception:
            pass

    tab.add_handler(cdp.network.RequestWillBeSent, on_request)
    tab.add_handler(cdp.network.ResponseReceived, on_response)
    tab.add_handler(cdp.network.LoadingFinished, on_loading_finished)

    try:
        await tab.evaluate('window.__ifCatResult = null;')
        await tab.get(delivery_url)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Estrategia 1: CDP LoadingFinished
        try:
            await asyncio.wait_for(cdp_done.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

        if cdp_done.is_set():
            return {'api_url': captured['url'], 'catalog': captured['data']}

        # Estrategia 2: CATALOG_CAPTURE_SCRIPT (fetch intercept)
        raw = await tab.evaluate('window.__ifCatResult')
        if raw:
            try:
                payload = json.loads(raw)
                data = payload.get('data')
                if data and isinstance(data, dict):
                    return {'api_url': payload.get('url', ''), 'catalog': data}
            except Exception:
                pass

        # Estrategia 3: fetch manual — tenta v1 e v2
        headers_raw = await tab.evaluate(_HEADERS_JS)
        headers_js  = headers_raw or '{}'

        for api_version in ('v1', 'v2'):
            catalog_url = (
                f'https://www.ifood.com.br/site-api/{api_version}'
                f'/merchants/{mid}/catalog'
                f'?latitude={lat}&longitude={lon}&channel=IFOOD'
            )
            await tab.evaluate(f"""
            (function() {{
                window.__catManual = null;
                fetch({json.dumps(catalog_url)}, {{
                    method: 'GET',
                    credentials: 'include',
                    cache: 'no-cache',
                    headers: {headers_js}
                }})
                .then(function(r) {{ return r.json(); }})
                .then(function(d) {{ window.__catManual = JSON.stringify({{ok: true, data: d}}); }})
                .catch(function(e) {{ window.__catManual = JSON.stringify({{ok: false, err: e.message}}); }});
            }})();
            """)

            fetch_end = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < fetch_end:
                raw = await tab.evaluate('window.__catManual')
                if raw:
                    result = json.loads(raw)
                    if result.get('ok') and result.get('data'):
                        data = result['data']
                        if isinstance(data, dict) and data:
                            return {'api_url': catalog_url, 'catalog': data}
                    break
                await asyncio.sleep(0.3)

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
# Merchant loading
# ---------------------------------------------------------------------------

def load_merchants(files: list[Path]) -> list[dict]:
    seen = {}
    for f in files:
        if not f.exists():
            print(f'[!] Arquivo nao encontrado: {f}')
            continue
        for line in f.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                mid = m.get('id')
                if mid and mid not in seen and m.get('link'):
                    seen[mid] = m
            except Exception:
                pass
    return list(seen.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    merchant_files: list[Path],
    output_dir: Path,
    delay: float,
    headless: bool,
    max_merchants: int = 0,
    per_capture: bool = False,
):
    merchants = load_merchants(merchant_files)
    if not merchants:
        print('[!] Nenhum merchant encontrado.')
        return

    if max_merchants:
        merchants = merchants[:max_merchants]

    # Pula lojas cuja pasta já existe e tem catalog.json
    todo = [m for m in merchants if not is_done(store_folder(output_dir, m['id'], m['name']))]

    print(f'[*] {len(merchants)} merchants carregados, {len(todo)} sem catalogo')
    if not todo:
        print('[+] Todos os catalogos ja foram capturados.')
        return

    print(f'[*] Salvando em: {output_dir}')

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
        ],
    )

    browser_pid = None
    try:
        browser_pid = browser.process.pid
        CHROME_PROFILE.mkdir(exist_ok=True)
        _PID_FILE.write_text(str(browser_pid))
        print(f'[*] Chrome PID: {browser_pid}')
    except AttributeError:
        pass

    tab = await browser.get('about:blank')
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=STEALTH_SCRIPT))
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=CATALOG_CAPTURE_SCRIPT))
    await tab.send(cdp.network.enable())

    print('[*] Verificando sessao...')
    await tab.get('https://www.ifood.com.br/restaurantes')
    await asyncio.sleep(3.0)
    cookies = await tab.send(cdp.network.get_cookies(urls=[f'https://{IFOOD_HOST}']))
    if not any(c.name == 'aAccessToken' and c.value for c in cookies):
        print('[!] Sessao invalida. Execute login.py e tente novamente.')
        try:
            browser.stop()
        except Exception:
            pass
        _stop_our_chrome(browser_pid)
        return
    print('[+] Sessao valida.')
    print(f'[*] Delay base: {delay}s (+-30% jitter)\n')

    ok_count   = 0
    fail_count = 0

    for i, merchant in enumerate(todo, 1):
        name_preview = merchant['name'][:50]
        print(f'[{i:4d}/{len(todo)}] {name_preview}')

        result = await crawl_catalog_page(tab, merchant)

        folder = store_folder(output_dir, merchant['id'], merchant['name'])

        if result:
            folder.mkdir(parents=True, exist_ok=True)

            # catalog.json — resposta bruta + metadados
            catalog_out = {
                'merchant_id':   merchant['id'],
                'merchant_name': merchant['name'],
                'merchant_link': merchant.get('link', ''),
                'api_url':       result.get('api_url', ''),
                'crawled_at':    datetime.now().isoformat(),
                'catalog':       result['catalog'],
            }
            (folder / 'catalog.json').write_text(
                json.dumps(catalog_out, ensure_ascii=False, indent=2), encoding='utf-8'
            )

            # products.jsonl — um produto por linha, preço em centavos e BRL
            products = extract_products(result['catalog'])
            with open(folder / 'products.jsonl', 'w', encoding='utf-8') as pf:
                for p in products:
                    pf.write(json.dumps(p, ensure_ascii=False) + '\n')

            ok_count += 1
            print(f'  -> {len(products)} produtos  [{folder.name}]')
        else:
            fail_count += 1
            print('  -> FALHOU (sem catalogo)')

        wait = delay * random.uniform(0.7, 1.3)
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

    print(f'\n[+] Concluido: {ok_count} ok, {fail_count} falhas')
    print(f'[+] Lojas em: {output_dir}')

    try:
        browser.stop()
    except Exception:
        pass
    _stop_our_chrome(browser_pid)
    _clear_profile_locks()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler de catalogos iFood por restaurante')
    parser.add_argument(
        '--capture-dir', type=Path, metavar='DIR',
        help='Diretorio de uma captura existente (lê merchants.jsonl de lá e salva lojas lá dentro)',
    )
    parser.add_argument(
        'merchants', type=Path, nargs='*',
        help='Um ou mais arquivos merchants.jsonl (legado, use --capture-dir)',
    )
    parser.add_argument('--catalogs-dir', type=Path,
                        help='Diretorio de saida para modo legado (default: mesmo dir do primeiro merchants.jsonl)')
    parser.add_argument('--delay',    type=float, default=8.0,  help='Delay base em segundos (default 8)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    parser.add_argument('--max',      type=int,   default=0,     help='Processar no max N lojas (0 = todas)')
    args = parser.parse_args()

    if args.capture_dir:
        merchants_file = args.capture_dir / 'merchants.jsonl'
        if not merchants_file.exists():
            print(f'[!] {merchants_file} nao encontrado.')
            sys.exit(1)
        output_dir = args.capture_dir
        merchant_files = [merchants_file]
    elif args.merchants:
        merchant_files = args.merchants
        output_dir = args.catalogs_dir if args.catalogs_dir else merchant_files[0].parent
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        parser.error('Especifique --capture-dir PATH ou passe arquivos merchants.jsonl')

    uc.loop().run_until_complete(
        main(merchant_files, output_dir, args.delay, args.headless, args.max)
    )
