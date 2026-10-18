"""PROTOTYPE ONLY: serve the frontend locally with `python3 prototype/frontend/server.py`."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 5173), partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).parent)))
    print('Frontend prototype: http://127.0.0.1:5173', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
