"""Owner access tool (runs on YOUR machine; the private key never leaves it).

  python tools/owner_access.py            # sign a short-lived login token, copy it, open the login page
  python tools/owner_access.py --init     # first-time key creation only
  python tools/owner_access.py --rotate   # replace the owner key (old tokens/sessions stop working)
  python tools/owner_access.py --print    # print the token instead of using the clipboard

Server side only the PUBLIC key is needed:
  local:   it is read from ~/.medrecover/owner_pk.b64 automatically
  deploy:  set OWNER_PUBKEY_B64 to the contents of that file
"""
import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import security  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--init', action='store_true')
    ap.add_argument('--rotate', action='store_true')
    ap.add_argument('--print', dest='show', action='store_true')
    ap.add_argument('--base', default='http://127.0.0.1:8010')
    ap.add_argument('--ttl', type=int, default=120, help='token lifetime in seconds (10-300)')
    a = ap.parse_args()

    sk = security.keydir() / 'owner_sk.b64'
    if a.rotate or not sk.exists():
        security.generate_keypair('owner', overwrite=a.rotate)
        print(f'[+] Created ML-DSA-65 owner keypair in {security.keydir()}')
        print(f'    Private key: {sk}  (keep secret, back it up offline)')
        print('    Deploy-time public key (OWNER_PUBKEY_B64):')
        print('    ' + (security.keydir() / 'owner_pk.b64').read_text()[:60] + '...')
    if a.init or a.rotate:
        return 0

    token = security.mint_owner_token(ttl=a.ttl)
    print(f'[+] Signed token for {security.OWNER_EMAIL} (valid {a.ttl}s, single use)')
    if a.show:
        print(token)
        return 0
    try:
        subprocess.run(['clip'], input=token.encode(), check=True)  # Windows clipboard
        print('[+] Token copied to clipboard. Click "Sign in with clipboard".')
    except Exception:
        print('[!] Clipboard unavailable. Re-run with --print and paste the token.')
    webbrowser.open(a.base.rstrip('/') + '/owner-login')
    return 0


if __name__ == '__main__':
    sys.exit(main())
