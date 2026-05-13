"""
Crawler iFood Web — SP completo via Camoufox (navegação natural).

Para cada ponto da grade:
  1. Atualiza cookies de localização (address-latitude, address-longitude, fstr.session)
     antes de navegar — o JS do iFood lê esses cookies para montar a chamada de API.
  2. Navega para /restaurantes (lista completa da região, não curada como /inicio).
  3. Intercepta qualquer /site-api/ e, se a URL tiver lat/lon, substitui as coordenadas.
  4. Captura o primeiro response com merchant UUIDs (sem exigir lat/lon na URL,
     pois /restaurantes pode usar os cookies como fonte de localização).
  5. Simula scroll + mouse para alimentar o sensor PX/Akamai.

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

from camoufox.async_api import AsyncCamoufox

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.sp_grid import generate_grid

HOME_URL     = "https://www.ifood.com.br/restaurantes"
IFOOD_HOST   = "www.ifood.com.br"
SESSION_FILE = Path(__file__).parent.parent / 'configs' / 'session.json'

API_ROUTE_PATTERN = "**/site-api/**"
MERCHANT_UUID_RE  = re.compile(
    r'"id"\s*:\s*"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"'
)

EXCLUDED_URL_PARTS = [
    'customers/me', 'wallet', 'benefits', 'orders',
    'payment', 'profile', 'address', 'voucher', 'loyalty',
    'fallback', 'cached', 'default',
]


def is_restaurant_listing(url: str, body_text: str) -> bool:
    if any(part in url.lower() for part in EXCLUDED_URL_PARTS):
        return False
    return len(MERCHANT_UUID_RE.findall(body_text)) >= 5


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


async def simulate_human(page):
    """Eventos de mouse/scroll para alimentar o sensor PX/Akamai."""
    await asyncio.sleep(random.uniform(1.5, 3.0))
    for _ in range(random.randint(2, 4)):
        await page.mouse.move(
            random.randint(200, 900), random.randint(100, 400),
            steps=random.randint(15, 30),
        )
        await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.mouse.wheel(0, random.randint(300, 700))
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await page.mouse.move(
        random.randint(100, 800), random.randint(200, 500),
        steps=random.randint(8, 20),
    )
    await asyncio.sleep(random.uniform(0.4, 1.0))
    await page.mouse.wheel(0, random.randint(200, 500))
    await asyncio.sleep(random.uniform(0.3, 0.7))


async def crawl_point(page, lat: float, lon: float, timeout: float = 35.0) -> dict | None:
    req_info  = {}
    resp_data = {}
    done      = asyncio.Event()

    async def handle_route(route, request):
        url     = request.url
        new_url = build_url(url, lat, lon) if ('latitude=' in url or 'longitude=' in url) else url

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

    seen_ids     = set()
    total_new    = 0
    detected_api = None

    async with AsyncCamoufox(
        headless=headless,
        os='windows',
        humanize=True,
        locale=['pt-BR', 'pt'],
        timezone='America/Sao_Paulo',
    ) as browser:
        ctx_kwargs = {}
        if SESSION_FILE.exists():
            ctx_kwargs['storage_state'] = str(SESSION_FILE)
            print(f'[*] Sessao carregada de: {SESSION_FILE}')
        else:
            print('[!] session.json nao encontrado — execute login.py primeiro')

        context = await browser.new_context(**ctx_kwargs)
        page    = await context.new_page()

        logged_in = await ensure_session(page, context)
        if not logged_in:
            print('[!] Sessao invalida ou expirada. Execute login.py e tente novamente.')
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawler iFood Web SP')
    parser.add_argument('--step',     type=float, default=8.0,  help='Espaçamento da grade em km')
    parser.add_argument('--delay',    type=float, default=60.0, help='Delay base entre navegações (s)')
    parser.add_argument('--headless', action='store_true',       help='Rodar sem janela')
    args = parser.parse_args()
    asyncio.run(main(args.step, args.delay, args.headless))
