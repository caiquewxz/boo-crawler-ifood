"""
Crawler de catálogos iFood — lê merchants.jsonl e captura o menu completo
de cada restaurante (itens, preços em centavos, complementos).

Uso:
    python scripts/crawl_catalog.py captures/crawl_nd_TIMESTAMP/merchants.jsonl
    python scripts/crawl_catalog.py captures/crawl_nd_*/merchants.jsonl --delay 10
    python scripts/crawl_catalog.py captures/crawl_nd_*/merchants.jsonl --headless

Saída:
    catalogs/{merchant_uuid}.json  — um arquivo por restaurante
    Resume automático: pula arquivos já existentes.
"""

import asyncio
import argparse
import base64
import json
import random
import subprocess
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

        # Estrategia 1: CDP
        try:
            await asyncio.wait_for(cdp_done.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            pass

        if cdp_done.is_set():
            return {'api_url': captured['url'], 'catalog': captured['data']}

        # Estrategia 2: CATALOG_CAPTURE_SCRIPT
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


async def main(merchant_files: list[Path], catalogs_dir: Path, delay: float, headless: bool):
    merchants = load_merchants(merchant_files)
    if not merchants:
        print('[!] Nenhum merchant encontrado nos arquivos fornecidos.')
        return

    catalogs_dir.mkdir(parents=True, exist_ok=True)
    todo = [m for m in merchants if not (catalogs_dir / f"{m['id']}.json").exists()]

    print(f'[*] {len(merchants)} merchants carregados, {len(todo)} sem catalogo')
    if not todo:
        print('[+] Todos os catalogos ja foram capturados.')
        return

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

    # Verificar sessao
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
    print(f'[*] Delay base: {delay}s (+-30% jitter)')
    print(f'[*] Salvando em: {catalogs_dir}\n')

    ok_count   = 0
    fail_count = 0

    for i, merchant in enumerate(todo, 1):
        name_preview = merchant['name'][:50]
        print(f'[{i:4d}/{len(todo)}] {name_preview}')

        result = await crawl_catalog_page(tab, merchant)

        if result:
            out = {
                'merchant_id':   merchant['id'],
                'merchant_name': merchant['name'],
                'api_url':       result.get('api_url', ''),
                'crawled_at':    datetime.now().isoformat(),
                'catalog':       result['catalog'],
            }
            out_path = catalogs_dir / f"{merchant['id']}.json"
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
            ok_count += 1
            # contar itens no catalog
            n_items = _count_catalog_items(result['catalog'])
            print(f'  -> {n_items} itens')
        else:
            fail_count += 1
            print('  -> FALHOU')

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
    print(f'[+] Catalogos: {catalogs_dir}')

    try:
        browser.stop()
    except Exception:
        pass
    _stop_our_chrome(browser_pid)
    _clear_profile_locks()


def _count_catalog_items(catalog: dict) -> int:
    count = 0
    # formato catalogGroup
    for group in catalog.get('catalogGroup', []):
        count += len(group.get('items', []))
    # formato categories
    for cat in catalog.get('categories', []):
        count += len(cat.get('items', []))
        for sub in cat.get('subcategories', []):
            count += len(sub.get('items', []))
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler de catalogos iFood por restaurante')
    parser.add_argument('merchants', type=Path, nargs='+',
                        help='Um ou mais arquivos merchants.jsonl')
    parser.add_argument('--catalogs-dir', type=Path,
                        default=Path(__file__).parent.parent / 'catalogs',
                        help='Diretorio de saida (default: catalogs/)')
    parser.add_argument('--delay',    type=float, default=8.0,  help='Delay base em segundos (default 8)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    args = parser.parse_args()
    uc.loop().run_until_complete(main(args.merchants, args.catalogs_dir, args.delay, args.headless))
