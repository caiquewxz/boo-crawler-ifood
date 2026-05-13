"""
Abre o iFood no browser para login manual e salva a sessão.
Execute uma vez antes de rodar o crawler.

Uso:
    python scripts/login.py
"""

import asyncio
import json
from pathlib import Path
from camoufox.async_api import AsyncCamoufox

SESSION_FILE = Path(__file__).parent.parent / 'configs' / 'session.json'
HOME_URL     = 'https://www.ifood.com.br/inicio'


async def main():
    async with AsyncCamoufox(
        headless=False,
        os='windows',
        humanize=True,
        locale=['pt-BR', 'pt'],
    ) as browser:
        context = await browser.new_context(
            timezone_id='America/Sao_Paulo',
        )
        page    = await context.new_page()
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


if __name__ == '__main__':
    asyncio.run(main())
