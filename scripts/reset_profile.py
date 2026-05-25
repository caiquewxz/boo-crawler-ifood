"""
Apaga o perfil do Chrome (.chrome-profile/) e abre o login em seguida.

Uso:
    python scripts/reset_profile.py           # reseta e abre login.py
    python scripts/reset_profile.py --no-login  # só reseta
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
PROFILE_NAME   = CHROME_PROFILE.name


def kill_chrome():
    """Mata todos os processos Chrome usando este perfil."""
    subprocess.run([
        'powershell', '-Command',
        f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{PROFILE_NAME}*'}} | "
        f"ForEach-Object {{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}",
    ], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description='Reseta o perfil Chrome do crawler')
    parser.add_argument('--no-login', action='store_true', help='Não abre o login.py após resetar')
    args = parser.parse_args()

    print(f'[*] Encerrando Chrome com perfil {PROFILE_NAME}...')
    kill_chrome()
    time.sleep(1.0)

    if CHROME_PROFILE.exists():
        print(f'[*] Apagando {CHROME_PROFILE}...')
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
        if CHROME_PROFILE.exists():
            print('[!] Não foi possível apagar completamente — algum processo ainda pode estar usando o perfil.')
            print('    Feche o Chrome manualmente e tente novamente.')
            sys.exit(1)
        print('[+] Perfil apagado.')
    else:
        print('[*] Perfil não encontrado — nada para apagar.')

    if not args.no_login:
        print('[*] Abrindo login.py...\n')
        login_script = Path(__file__).parent / 'login.py'
        subprocess.run([sys.executable, str(login_script)])


if __name__ == '__main__':
    main()
