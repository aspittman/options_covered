"""Restart on failure while retaining the durable order ledger."""
import subprocess
import sys
import time
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    while True:
        result = subprocess.run([sys.executable, str(root / 'main.py')], cwd=root)
        if result.returncode == 0:
            return
        if result.returncode == 2:
            print('Bot configuration or command-line error; fix the reported settings and restart launcher.py.', flush=True)
            raise SystemExit(2)
        print(f'Bot exited with {result.returncode}; restart in 30 seconds', flush=True)
        time.sleep(30)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
