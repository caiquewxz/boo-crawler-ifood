"""
Crawler iFood Web — SP completo via Playwright (navegação natural).

Para cada ponto da grade:
  1. Atualiza cookies de localização (address-latitude, address-longitude, fstr.session)
     antes de navegar — o JS do iFood lê esses cookies para montar a chamada de API.
  2. Navega para /restaurantes (lista completa da região, não curada como /inicio).
  3. Intercepta qualquer /site-api/ e, se a URL tiver lat/lon, substitui as coordenadas.
  4. Captura o primeiro response com merchant UUIDs (sem exigir lat/lon na URL,
     pois /restaurantes pode usar os cookies como fonte de localização).
  5. Simula scroll + mouse para alimentar o sensor PX/Akamai.
  6. Detecta desafio de "segurar botão" do Akamai e tenta resolvê-lo automaticamente.

Uso:
    python scripts/login.py
    python scripts/crawl_sp_web.py [--step 8.0] [--delay 60.0] [--headless]

Saída em captures/crawl_web_TIMESTAMP/:
    requests.jsonl  — um JSON por linha com request + response completos
    crawl.log       — progresso, endpoint detectado e contagem de merchants únicos
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

from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.sp_grid import generate_grid

# Stealth manual — cobre os sinais mais básicos sem precisar do pacote externo
STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver',  {get: () => undefined});
    Object.defineProperty(navigator, 'plugins',    {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages',  {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory',        {get: () => 8});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
"""

# /restaurantes lista todos os estabelecimentos da região (sem curadoria)
HOME_URL     = "https://www.ifood.com.br/restaurantes"
IFOOD_HOST   = "www.ifood.com.br"
SESSION_FILE = Path(__file__).parent.parent / 'configs' / 'session.json'

# Intercepta qualquer chamada /site-api/ — sem fixar endpoint específico
API_ROUTE_PATTERN = "**/site-api/**"
MERCHANT_UUID_RE  = re.compile(
    r'"id"\s*:\s*"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"'
)

# Endpoints de conta/pagamento/benefícios — não são listagem de restaurantes
EXCLUDED_URL_PARTS = [
    'customers/me', 'wallet', 'benefits', 'orders',
    'payment', 'profile', 'address', 'voucher', 'loyalty',
    'fallback', 'cached', 'default',
]

# Seletor do botão de "segurar" do Akamai no iFood
HOLD_BUTTON_SELECTORS = [
    'p:has-text("Pressione e segure")',
    'p:has-text("Press and hold")',
]


def is_restaurant_listing(url: str, body_text: str) -> bool:
    """
    True apenas para respostas que parecem listagem de restaurantes:
    URL fora dos endpoints de conta/wallet e com >= 5 merchant UUIDs.
    """
    if any(part in url.lower() for part in EXCLUDED_URL_PARTS):
        return False
    return len(MERCHANT_UUID_RE.findall(body_text)) >= 5


async def find_hold_button(page):
    """
    Procura o botão de hold em todos os frames da página (incluindo iframes).
    Retorna (locator, frame) ou (None, None).
    """
    for frame in page.frames:
        for selector in HOLD_BUTTON_SELECTORS:
            try:
                btn = frame.locator(selector).first
                if await btn.is_visible(timeout=600):
                    return btn, frame
            except Exception:
                continue
    return None, None


async def is_challenge_present(page) -> bool:
    if any(x in page.url.lower() for x in ['challenge', '/entrar', 'access-denied', 'errors.edgesuite']):
        return True
    btn, _ = await find_hold_button(page)
    return btn is not None


def build_url(url: str, lat: float, lon: float) -> str:
    url = re.sub(r'latitude=[^&]+',  f'latitude={lat}',  url)
    url = re.sub(r'longitude=[^&]+', f'longitude={lon}', url)
    url = re.sub(r'size=\d+',        'size=100',          url)
    return url


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


async def set_location_cookies(context, lat: float, lon: float):
    """
    Atualiza cookies de localização antes de cada navegação.
    O front-end do iFood lê address-latitude / address-longitude para
    determinar a localização. fstr.session é atualizado para eliminar
    a contradição entre o cookie de sessão e as coordenadas injetadas.
    """
    await context.add_cookies([
        {'name': 'address-latitude',  'value': str(lat), 'domain': IFOOD_HOST, 'path': '/'},
        {'name': 'address-longitude', 'value': str(lon), 'domain': IFOOD_HOST, 'path': '/'},
    ])
    cookies = await context.cookies(f'https://{IFOOD_HOST}')
    for c in cookies:
        if c['name'] == 'fstr.session':
            try:
                padded = c['value'] + '=' * (-len(c['value']) % 4)
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
                await context.add_cookies([{
                    'name': 'fstr.session', 'value': new_val,
                    'domain': IFOOD_HOST,   'path': '/',
                }])
            except Exception:
                pass
            break


async def try_auto_hold(page) -> bool:
    """
    Encontra o botão em qualquer frame (incluindo iframes) e simula o hold.
    bounding_box() retorna coordenadas relativas ao viewport, então page.mouse funciona direto.
    """
    btn, _ = await find_hold_button(page)
    if btn is None:
        return False

    box = await btn.bounding_box()
    if not box:
        return False

    cx = box['x'] + box['width']  / 2 + random.uniform(-3, 3)
    cy = box['y'] + box['height'] / 2 + random.uniform(-3, 3)
    await page.mouse.move(cx, cy, steps=random.randint(20, 35))
    await asyncio.sleep(random.uniform(0.3, 0.7))
    await page.mouse.down()
    hold  = random.uniform(4.5, 6.5)
    steps = int(hold / 0.3)
    for _ in range(steps):
        await page.mouse.move(
            cx + random.uniform(-2, 2),
            cy + random.uniform(-2, 2),
            steps=1,
        )
        await asyncio.sleep(0.3)
    await page.mouse.up()
    await asyncio.sleep(2.0)
    print('[*] Desafio: hold simulado')
    return True


async def handle_challenge(page, manual_timeout: int = 120) -> bool:
    """
    Detecta desafio de 'segurar botão' (overlay ou redirect), tenta automação
    e espera resolução manual como fallback. Retorna True quando resolvido.
    """
    if not await is_challenge_present(page):
        return True

    print(f'\n[!] Desafio detectado: {page.url}')

    if await try_auto_hold(page):
        await asyncio.sleep(2)
        if not await is_challenge_present(page):
            print('[+] Desafio resolvido automaticamente.')
            return True

    print(f'[!] Automação falhou — resolva manualmente no browser ({manual_timeout}s)...')
    loop = asyncio.get_event_loop()
    deadline = loop.time() + manual_timeout
    while loop.time() < deadline:
        if not await is_challenge_present(page):
            print('[+] Desafio resolvido manualmente.')
            return True
        await asyncio.sleep(1)

    print('[!] Timeout aguardando resolução do desafio.')
    return False


async def simulate_human(page):
    """Eventos de mouse/scroll aleatórios para alimentar o sensor PX/Akamai."""
    await asyncio.sleep(random.uniform(1.0, 2.5))
    await page.mouse.move(
        random.randint(200, 900), random.randint(100, 400),
        steps=random.randint(10, 25),
    )
    await asyncio.sleep(random.uniform(0.4, 1.0))
    await page.mouse.wheel(0, random.randint(200, 600))
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await page.mouse.move(
        random.randint(100, 800), random.randint(200, 500),
        steps=random.randint(6, 15),
    )
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def crawl_point(page, lat: float, lon: float, timeout: float = 35.0) -> dict | None:
    """
    Navega para HOME_URL com cookies de localização já atualizados.
    Intercepta qualquer /site-api/ com lat/lon na URL (substitui coordenadas).
    Captura o primeiro response que retornar merchant UUIDs — não exige
    lat/lon na URL, pois /restaurantes pode usar cookies como fonte.
    """
    req_info  = {}
    resp_data = {}
    done      = asyncio.Event()

    async def handle_route(route, request):
        url     = request.url
        new_url = build_url(url, lat, lon) if ('latitude=' in url or 'longitude=' in url) else url

        # Captura metadados do primeiro request /site-api/
        if not req_info and 'site-api' in url:
            req_info['url']         = new_url
            req_info['method']      = request.method
            req_info['req_headers'] = dict(request.headers)
            req_info['req_body']    = request.post_data

        if new_url != url:
            await route.continue_(url=new_url)
        else:
            await route.continue_()

    async def handle_response(response):
        if 'site-api' not in response.url or done.is_set():
            return
        try:
            body      = await response.json()
            body_text = json.dumps(body)
            if is_restaurant_listing(response.url, body_text):
                resp_data['status']    = response.status
                resp_data['resp_body'] = body
                resp_data['api_url']   = response.url
                done.set()
        except Exception:
            pass

    page.on('response', handle_response)
    await page.route(API_ROUTE_PATTERN, handle_route)

    try:
        await page.goto(HOME_URL, wait_until='domcontentloaded', timeout=45000)
        await handle_challenge(page)
        await simulate_human(page)
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        resp_data.setdefault('error', str(e))
    finally:
        try:
            await page.unroute(API_ROUTE_PATTERN, handle_route)
        except Exception:
            pass
        page.remove_listener('response', handle_response)

    if not resp_data:
        return None
    return {**req_info, **resp_data}


async def ensure_session(page, context) -> bool:
    """
    Navega para HOME_URL uma vez antes do crawl para que o browser
    use o refresh token e atualize o JWT se estiver expirado.
    Salva a sessão renovada de volta no disco.
    Retorna True se o cookie aAccessToken estiver presente após a navegação.
    """
    print('[*] Verificando/renovando sessao...')
    try:
        await page.goto(HOME_URL, wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(3)
    except Exception as e:
        print(f'[!] Erro ao carregar pagina de verificacao: {e}')

    cookies   = await context.cookies(f'https://{IFOOD_HOST}')
    logged_in = any(c['name'] == 'aAccessToken' and c['value'] for c in cookies)
    await context.storage_state(path=str(SESSION_FILE))
    return logged_in


async def main(step_km: float, delay: float, headless: bool):
    points = generate_grid(step_km=step_km)
    print(f"[*] Grade {step_km} km: {len(points)} pontos")

    ts         = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir    = Path(__file__).parent.parent / 'captures' / f'crawl_web_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / 'requests.jsonl'
    log_path   = out_dir / 'crawl.log'

    seen_ids      = set()
    total_new     = 0
    detected_api  = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            channel='chrome',
            args=['--lang=pt-BR'],
        )
        ctx_kwargs = dict(
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/148.0.0.0 Safari/537.36'
            ),
        )
        if SESSION_FILE.exists():
            ctx_kwargs['storage_state'] = str(SESSION_FILE)
            print(f'[*] Sessao carregada de: {SESSION_FILE}')
        else:
            print(f'[!] session.json nao encontrado — execute login.py primeiro')

        context = await browser.new_context(**ctx_kwargs)
        page    = await context.new_page()

        await page.add_init_script(STEALTH_SCRIPT)
        if HAS_STEALTH:
            await stealth_async(page)
            print('[*] playwright-stealth aplicado')
        else:
            print('[*] Stealth manual aplicado (playwright-stealth nao instalado)')

        logged_in = await ensure_session(page, context)
        if not logged_in:
            print('[!] Sessao invalida ou expirada. Execute login.py e tente novamente.')
            await browser.close()
            return
        print('[+] Sessao valida. Iniciando crawl.')

        print(f'[*] Salvando em: {out_dir}')
        print(f'[*] Delay base: {delay}s (±20% jitter)\n')

        with open(jsonl_path, 'w', encoding='utf-8') as jf, \
             open(log_path,   'w', encoding='utf-8') as lf:

            lf.write(f'Inicio: {datetime.now()}\n')
            lf.write(f'Pontos: {len(points)}\n\n')

            for i, (lat, lon) in enumerate(points, 1):
                try:
                    await set_location_cookies(context, lat, lon)
                    result = await crawl_point(page, lat, lon)

                    if result is None:
                        raise Exception('nenhuma chamada de API com merchants (timeout)')
                    if 'error' in result:
                        raise Exception(result['error'])

                    # Loga o endpoint detectado na primeira captura bem-sucedida
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

                    # Persiste tokens renovados pelo browser (evita expiração durante o crawl)
                    await context.storage_state(path=str(SESSION_FILE))

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

        await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler iFood Web SP')
    parser.add_argument('--step',     type=float, default=8.0,  help='Espaçamento da grade em km')
    parser.add_argument('--delay',    type=float, default=60.0, help='Delay base entre navegações (s)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    args = parser.parse_args()
    asyncio.run(main(args.step, args.delay, args.headless))
