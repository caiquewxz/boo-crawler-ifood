"""
Abre o iFood no browser para login manual e salva a sessão.
Execute uma vez antes de rodar o crawler.

Uso:
    python scripts/login.py
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US']});
    window.chrome = { runtime: {} };
"""

SESSION_FILE = Path(__file__).parent.parent / 'configs' / 'session.json'
HOME_URL     = 'https://www.ifood.com.br/inicio'


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel='chrome',  # usa Chrome instalado, fingerprint mais legítimo
            args=['--lang=pt-BR'],
        )
        context = await browser.new_context(
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/148.0.0.0 Safari/537.36'
            ),
        )
        page = await context.new_page()
        await page.add_init_script(STEALTH_SCRIPT)
        if HAS_STEALTH:
            await stealth_async(page)
        await page.goto(HOME_URL, wait_until='domcontentloaded', timeout=45000)

        print()
        print('=' * 60)
        print('  Faça login na sua conta do iFood no browser aberto.')
        print('  Quando estiver na tela inicial com os restaurantes,')
        print('  volte aqui e pressione ENTER para salvar a sessão.')
        print('=' * 60)
        input()

        state = await context.storage_state()
        SESSION_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[+] Sessão salva em: {SESSION_FILE}')

        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
