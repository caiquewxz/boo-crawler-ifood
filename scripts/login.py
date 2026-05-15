"""
Login manual no iFood via nodriver (CDP direto, sem WebDriver).
Execute uma vez antes do crawler. Salva cookies em configs/session.json
e mantém o perfil Chrome em .chrome-profile/.

Uso:
    python scripts/login.py
"""

import asyncio
import json
import subprocess
from pathlib import Path

import nodriver as uc
from nodriver import cdp

CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
SESSION_FILE   = Path(__file__).parent.parent / 'configs' / 'session.json'
HOME_URL       = 'https://www.ifood.com.br/inicio'
IFOOD_HOST     = 'www.ifood.com.br'

STEALTH_SCRIPT = r"""
(function() {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: () => undefined, configurable: true, enumerable: true,
    });
    Object.keys(window).filter(k => k.startsWith('cdc_')).forEach(k => {
        try { delete window[k]; } catch(e) {}
    });
    window.chrome = {
        app: {isInstalled: false},
        runtime: {connect: () => {}, sendMessage: () => {}, id: undefined},
        loadTimes: function() {}, csi: function() {},
    };
    Object.defineProperty(Navigator.prototype, 'languages', {get: () => ['pt-BR','pt','en-US','en'], configurable: true});
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
})();
"""


_PID_FILE = CHROME_PROFILE / '.crawler_pid'


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


async def main():
    _kill_previous_crawler_chrome()
    CHROME_PROFILE.mkdir(exist_ok=True)
    SESSION_FILE.parent.mkdir(exist_ok=True)

    browser = await uc.start(
        user_data_dir=str(CHROME_PROFILE),
        headless=False,
        lang='pt-BR',
        browser_args=[
            '--lang=pt-BR',
            '--disable-blink-features=AutomationControlled',
            '--no-first-run',
            '--no-default-browser-check',
            '--exclude-switches=enable-automation',
            '--disable-infobars',
            '--window-size=1920,1080',
            '--start-maximized',
        ],
    )

    try:
        browser_pid = browser.process.pid
    except AttributeError:
        browser_pid = None

    tab = await browser.get(HOME_URL)
    await tab.send(cdp.page.add_script_to_evaluate_on_new_document(source=STEALTH_SCRIPT))
    await tab.send(cdp.network.enable())

    print()
    print('=' * 60)
    print('  Faca login na sua conta do iFood no browser aberto.')
    print('  Quando estiver na tela inicial com os restaurantes,')
    print('  volte aqui e pressione ENTER para salvar a sessao.')
    print('=' * 60)
    input()

    all_cookies = await tab.send(cdp.network.get_all_cookies())
    ifood_cookies = [
        {
            'name':     c.name,
            'value':    c.value,
            'domain':   c.domain or IFOOD_HOST,
            'path':     c.path or '/',
            'secure':   bool(c.secure),
            'httpOnly': bool(c.http_only),
            'sameSite': str(c.same_site) if c.same_site else 'Lax',
            'expires':  c.expires if c.expires and c.expires > 0 else -1,
        }
        for c in all_cookies
        if IFOOD_HOST in (c.domain or '')
    ]

    session = {'cookies': ifood_cookies, 'origins': []}
    SESSION_FILE.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[+] Sessao salva: {len(ifood_cookies)} cookies em {SESSION_FILE}')

    try:
        browser.stop()
    except Exception:
        pass
    _stop_our_chrome(browser_pid)
    _clear_profile_locks()


if __name__ == '__main__':
    uc.loop().run_until_complete(main())
