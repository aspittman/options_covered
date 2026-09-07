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
        print(f'Bot exited with {result.returncode}; restart in 30 seconds', flush=True)
        time.sleep(30)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
