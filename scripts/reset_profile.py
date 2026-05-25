"""
Reseta o perfil do Chrome (.chrome-profile/) e abre o login em seguida.

Com --clone, copia Cookies e History do Chrome real do usuário antes de
abrir o login — isso dá ao perfil do crawler um histórico legítimo de
navegação, reduzindo a chance de detecção de bot pelo HUMAN Security.

Uso:
    python scripts/reset_profile.py            # reseta e abre login.py
    python scripts/reset_profile.py --clone    # clona do Chrome real + login
    python scripts/reset_profile.py --no-login # só reseta, sem abrir login
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHROME_PROFILE = Path(__file__).parent.parent / '.chrome-profile'
PROFILE_NAME   = CHROME_PROFILE.name

# Localização padrão do Chrome do usuário no Windows
REAL_CHROME_DEFAULT = (
    Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data' / 'Default'
)

# Arquivos que vale copiar para dar ao perfil um histórico real
# (Cookies dá cookies de outros sites; History dá histórico de navegação)
CLONE_FILES = ['Cookies', 'History', 'Web Data', 'Favicons']


def kill_chrome():
    """Mata processos Chrome usando o perfil do crawler."""
    subprocess.run([
        'powershell', '-Command',
        f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*{PROFILE_NAME}*'}} | "
        f"ForEach-Object {{Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue}}",
    ], capture_output=True)


def kill_real_chrome():
    """Mata o Chrome real do usuário (necessário para copiar o Cookies)."""
    result = subprocess.run(
        ['powershell', '-Command',
         "Get-Process chrome -ErrorAction SilentlyContinue | "
         "Where-Object {$_.MainWindowTitle -ne ''} | "
         "Select-Object -ExpandProperty Id"],
        capture_output=True, text=True,
    )
    pids = [p.strip() for p in result.stdout.splitlines() if p.strip().isdigit()]
    if pids:
        print(f'[!] Chrome real está aberto (PID {", ".join(pids)}).')
        print('    Para copiar o Cookies o Chrome precisa estar fechado.')
        resp = input('    Fechar o Chrome agora? [s/N] ').strip().lower()
        if resp != 's':
            print('[*] Abortando clone. Rode sem --clone ou feche o Chrome primeiro.')
            sys.exit(0)
        subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], capture_output=True)
        time.sleep(1.5)
        print('[+] Chrome fechado.')


def clone_from_real_profile():
    """Copia arquivos relevantes do perfil real para o perfil do crawler."""
    if not REAL_CHROME_DEFAULT.exists():
        print(f'[!] Perfil real não encontrado em: {REAL_CHROME_DEFAULT}')
        print('    Use --clone somente se o Google Chrome estiver instalado.')
        sys.exit(1)

    kill_real_chrome()

    dest = CHROME_PROFILE / 'Default'
    dest.mkdir(parents=True, exist_ok=True)

    copied = []
    for name in CLONE_FILES:
        src = REAL_CHROME_DEFAULT / name
        if src.exists():
            shutil.copy2(src, dest / name)
            copied.append(name)

    print(f'[+] Clonado do perfil real: {", ".join(copied) if copied else "nenhum arquivo encontrado"}')


def main():
    parser = argparse.ArgumentParser(description='Reseta o perfil Chrome do crawler')
    parser.add_argument('--clone',    action='store_true',
                        help='Copia Cookies/History do Chrome real para o novo perfil')
    parser.add_argument('--no-login', action='store_true',
                        help='Não abre o login.py após resetar')
    args = parser.parse_args()

    print(f'[*] Encerrando Chrome com perfil {PROFILE_NAME}...')
    kill_chrome()
    time.sleep(1.0)

    if CHROME_PROFILE.exists():
        print(f'[*] Apagando {CHROME_PROFILE}...')
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
        if CHROME_PROFILE.exists():
            print('[!] Não foi possível apagar — algum processo ainda está usando o perfil.')
            print('    Feche o Chrome manualmente e tente novamente.')
            sys.exit(1)
        print('[+] Perfil apagado.')
    else:
        print('[*] Perfil não encontrado — nada para apagar.')

    if args.clone:
        print('[*] Clonando arquivos do Chrome real...')
        clone_from_real_profile()

    if not args.no_login:
        print('[*] Abrindo login.py...\n')
        login_script = Path(__file__).parent / 'login.py'
        subprocess.run([sys.executable, str(login_script)])


if __name__ == '__main__':
    main()
