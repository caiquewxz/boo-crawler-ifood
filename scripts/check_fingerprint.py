"""
Diagnóstico de fingerprint — abre o browser com as mesmas configurações
do crawler e visita sites de teste de bot detection.

Uso:
    python scripts/check_fingerprint.py

Salva screenshots em captures/fingerprint_TIMESTAMP/.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

STEALTH_SCRIPT = """
(function() {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });
    Object.defineProperty(navigator, 'plugins', {get: () => [
        {name: 'PDF Viewer',               filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
        {name: 'Chrome PDF Viewer',        filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
        {name: 'Chromium PDF Viewer',      filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
        {name: 'Microsoft Edge PDF Viewer',filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
        {name: 'WebKit built-in PDF',      filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
    ]});
    Object.defineProperty(navigator, 'languages',           {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory',        {get: () => 8});
    window.chrome = {
        app: {isInstalled: false},
        runtime: {connect: () => {}, sendMessage: () => {}},
        loadTimes: function() {},
        csi: function() {},
    };
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
})();
"""

CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
CHROME_ARGS    = [
    '--lang=pt-BR',
    '--disable-blink-features=AutomationControlled',
    '--no-first-run',
    '--no-default-browser-check',
]

async def main():
    ts      = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = Path(__file__).parent.parent / 'captures' / f'fingerprint_{ts}'
    out_dir.mkdir(parents=True, exist_ok=True)

    CHROME_PROFILE.mkdir(exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(CHROME_PROFILE),
            headless=False,
            channel='chrome',
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/136.0.0.0 Safari/537.36'
            ),
            args=CHROME_ARGS,
            ignore_default_args=['--enable-automation'],
        )

        # --- sannysoft: extrai resultados como texto ---
        page = await context.new_page()
        await page.add_init_script(STEALTH_SCRIPT)
        if HAS_STEALTH:
            await stealth_async(page)

        print('[*] Abrindo bot.sannysoft.com ...')
        await page.goto('https://bot.sannysoft.com/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(3000)

        await page.screenshot(path=str(out_dir / 'sannysoft.png'), full_page=True)

        # Extrai todas as linhas da tabela de resultados
        rows = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('table tr').forEach(tr => {
                const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                if (cells.length >= 2) results.push(cells.join(' | '));
            });
            return results;
        }""")

        print('\n=== SANNYSOFT RESULTS ===')
        for row in rows:
            print(row)

        report_path = out_dir / 'sannysoft_results.txt'
        report_path.write_text('\n'.join(rows), encoding='utf-8')
        print(f'\n[+] Resultados salvos em: {report_path}')

        print('\n[*] Pressione ENTER para fechar...')
        input()
        await context.close()


if __name__ == '__main__':
    asyncio.run(main())
