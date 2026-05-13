"""
Abre o iFood no Chrome real (via CDP) para login manual e salva a sessão.
Execute uma vez antes de rodar o crawler.

Uso:
    python scripts/login.py
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
]

CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
SESSION_FILE   = Path(__file__).parent.parent / 'configs' / 'session.json'
HOME_URL       = 'https://www.ifood.com.br/inicio'
DEBUG_PORT     = 9222


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if p.exists():
            return str(p)
    return None


async def connect_with_retry(playwright, port: int, retries: int = 10):
    for _ in range(retries):
        try:
            return await playwright.chromium.connect_over_cdp(f'http://localhost:{port}')
        except Exception:
            await asyncio.sleep(0.5)
    return None


async def main():
    chrome = find_chrome()
    if not chrome:
        print('[!] Chrome não encontrado. Instale o Google Chrome.')
        sys.exit(1)

    CHROME_PROFILE.mkdir(exist_ok=True)
    proc = subprocess.Popen([
        chrome,
        f'--remote-debugging-port={DEBUG_PORT}',
        f'--user-data-dir={CHROME_PROFILE}',
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        HOME_URL,
    ])

    async with async_playwright() as p:
        browser = await connect_with_retry(p, DEBUG_PORT)
        if not browser:
            print('[!] Não foi possível conectar ao Chrome.')
            proc.terminate()
            sys.exit(1)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        print()
        print('=' * 60)
        print('  Faça login na sua conta do iFood no browser aberto.')
        print('  Quando estiver na tela inicial com os restaurantes,')
        print('  volte aqui e pressione ENTER para salvar a sessão.')
        print('=' * 60)
        input()

        state = await context.storage_state()
        SESSION_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        print(f'[+] Sessão salva em: {SESSION_FILE}')

    proc.terminate()


if __name__ == '__main__':
    asyncio.run(main())
