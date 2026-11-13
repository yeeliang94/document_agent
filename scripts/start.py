"""Bootstrap once, then serve the local application and open its ready UI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APP_ID = 'document-intelligence'
INSTANCE_ID = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16]


def bootstrap() -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError('Python 3.12 or newer is required.')
    folder = ROOT / '.venv'
    python = folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        print('Creating local Python environment…', flush=True)
        venv.create(folder, with_pip=True)
    identity = subprocess.check_output(
        [
            str(python),
            '-c',
            "import sys; assert sys.version_info >= (3,12), 'Remove .venv and restart with Python 3.12+'; print(sys.version); print(sys.executable)",
        ],
        text=True,
    )
    fingerprint = hashlib.sha256((ROOT / 'requirements.txt').read_bytes() + identity.encode()).hexdigest()
    stamp = folder / '.requirements-stamp'
    if not stamp.exists() or stamp.read_text().strip() != fingerprint:
        print('Installing application dependencies…', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
        stamp.write_text(fingerprint)
    env_file = ROOT / '.env'
    if not env_file.exists():
        shutil.copyfile(ROOT / '.env.example', env_file)
        print('Created .env. Add your OpenAI key there to process documents.', flush=True)
    args = [arg for arg in sys.argv[1:] if arg != '--bootstrap']
    command = [str(python), str(Path(__file__).resolve()), *args]
    if os.name == 'nt':
        # Windows execv does not quote arguments containing spaces.
        raise SystemExit(subprocess.call(command))
    os.execv(str(python), command)


def fetch(url: str) -> tuple[int, bytes]:
    # Local readiness probes must bypass enterprise HTTP proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=1) as response:
        return response.status, response.read(1024 * 1024)


def ready(url: str) -> bool:
    try:
        status, data = fetch(url + '/health/readiness')
        payload = json.loads(data)
        if status != 200 or payload.get('app') != APP_ID:
            return False
        status, html = fetch(url + '/')
        return status == 200 and b'<html' in html.lower()
    except (OSError, ValueError, urllib.error.URLError):
        return False


def owned_process(url: str) -> int | None:
    try:
        status, data = fetch(url + '/health/readiness')
        payload = json.loads(data)
        pid = payload.get('pid')
        if status == 200 and payload.get('app') == APP_ID and payload.get('instance_id') == INSTANCE_ID:
            if type(pid) is int and pid > 0 and pid != os.getpid():
                return pid
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def bind_listener(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--restart', action='store_true', help="Restart this project's existing server to reload .env")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.bootstrap:
        bootstrap()

    # Load operating-system certificate trust before importing HTTP/model clients.
    import truststore

    truststore.inject_into_ssl()
    from dotenv import load_dotenv

    load_dotenv(ROOT / '.env')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    from app.config import settings

    settings.validate()
    import uvicorn

    host, port = settings.host, settings.port
    browser_host = '127.0.0.1' if host == '0.0.0.0' else '::1' if host == '::' else host
    url = f'http://{"[" + browser_host + "]" if ":" in browser_host else browser_host}:{port}'
    # Keep the bound socket through uvicorn startup to avoid a check/bind race.
    try:
        listener = bind_listener(host, port)
    except OSError as exc:
        pid = owned_process(url) if args.restart else None
        if args.restart and pid is not None:
            print("Restarting this project's application to reload configuration…", flush=True)
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while True:
                try:
                    listener = bind_listener(host, port)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        print(
                            'The existing server did not release its port within 10 seconds. Stop it and retry.',
                            file=sys.stderr,
                        )
                        return 1
                    time.sleep(0.2)
        elif not args.restart and ready(url):
            print(f'Application already running: {url}')
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        else:
            reason = (
                ' Existing server identity does not match this project; no process was stopped.' if args.restart else ''
            )
            print(
                f'Cannot start on {host}:{port}: {exc}.{reason} Set DI_PORT in .env to another port.', file=sys.stderr
            )
            return 1
    server = uvicorn.Server(uvicorn.Config('app.main:app', host=host, port=port, log_level='info', workers=1))
    stopped = threading.Event()
    became_ready = threading.Event()

    def open_when_ready() -> None:
        deadline = time.monotonic() + 60
        while not stopped.is_set() and time.monotonic() < deadline:
            if ready(url):
                became_ready.set()
                print(f'Ready: {url}', flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return
            stopped.wait(0.3)
        if not stopped.is_set():
            print('Startup readiness timed out. Inspect server output above.', file=sys.stderr)
            server.should_exit = True

    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        stopped.set()
        listener.close()
    return 0 if became_ready.is_set() else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Startup failed: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
