"""Local server for Dev Board: serves the page and wraps arduino-cli for compile / upload.

    python server.py [port]        (default 8080, bound to 127.0.0.1 only)
"""
import csv, datetime, glob, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.error, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(ROOT, 'tools')
SKETCH = os.path.join(TOOLS, 'work', 'sketch')
LOCAL_CLI = os.path.join(TOOLS, 'arduino-cli.exe' if os.name == 'nt' else 'arduino-cli')
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
PAGES = {'/': 'dev-board.html', '/dev-board.html': 'dev-board.html', '/serial-studio.html': 'serial-studio.html'}
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
busy = threading.Lock()
LOGS = os.path.join(ROOT, 'logs')
WIFI = {}                               # session id -> {'sock', 'stop'} for open WiFi streams

KEY_FILE = os.path.join(TOOLS, 'anthropic.key')
AI_MODEL = os.environ.get('DEVBOARD_AI_MODEL', 'claude-sonnet-5-5')
AI_SYSTEM = (
    "You write firmware for hobbyist development boards inside a serial monitor / plotter tool. "
    "Reply with exactly ONE fenced code block containing the complete program (no prose before or after); "
    "explain non-obvious choices in short code comments. "
    "To make values appear in the live plotter, print one line per sample as space-separated name:value pairs, "
    "e.g. 'temp:23.5 hum:40'. Use Serial.begin(115200) unless told otherwise. "
    "Keep to the board and language given. If existing code is supplied, modify it rather than starting over, "
    "and fix any compile errors or problems visible in the supplied output."
)
_ai_key = None


def ai_key():
    global _ai_key
    if _ai_key:
        return _ai_key
    if os.environ.get('ANTHROPIC_API_KEY'):
        return os.environ['ANTHROPIC_API_KEY']
    try:
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None

# extra package indexes, only ever chosen by core id (never taken from the client)
CORE_URLS = {
    'esp32:esp32': 'https://espressif.github.io/arduino-esp32/package_esp32_index.json',
    'esp8266:esp8266': 'https://arduino.esp8266.com/stable/package_esp8266com_index.json',
    'rp2040:rp2040': 'https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json',
}
FQBN_RE = re.compile(r'^[\w.\-]+:[\w.\-]+:[\w.\-]+(:[\w.=,\-]+)?$')
PORT_RE = re.compile(r'^[\w./\\:\-]+$')


def cli_path():
    # order: ARDUINO_CLI env var, ./tools, PATH, then unpacked downloads like C:\arduino-cli_*\
    env = os.environ.get('ARDUINO_CLI')
    if env and os.path.exists(env):
        return env
    if os.path.exists(LOCAL_CLI):
        return LOCAL_CLI
    found = shutil.which('arduino-cli')
    if found:
        return found
    name = 'arduino-cli.exe' if os.name == 'nt' else 'arduino-cli'
    for base in (os.path.abspath(os.sep), os.path.expanduser('~'), os.path.expanduser('~/Downloads'), os.path.expanduser('~/Desktop')):
        for hit in sorted(glob.glob(os.path.join(base, 'arduino-cli*', name)), reverse=True):
            return hit
    return None


def run_json(args):
    cli = cli_path()
    if not cli:
        return None
    out = subprocess.run([cli, *args, '--format', 'json'], capture_output=True, text=True, creationflags=NOWIN, timeout=60)
    return json.loads(out.stdout or 'null')


def norm_id(v):
    return (v or '').replace('0x', '').replace('0X', '').upper().zfill(4) if v else ''


def wifi_scan(ports):
    # look for boards listening on the given TCP ports: every host of this PC's /24 networks, plus the mDNS name
    nets = set()
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(('10.255.255.255', 1))            # no packet is sent; just picks the LAN interface
        nets.add(u.getsockname()[0].rsplit('.', 1)[0])
        u.close()
    except OSError:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            nets.add(ai[4][0].rsplit('.', 1)[0])
    except OSError:
        pass
    targets = [(f'{n}.{i}', p) for n in nets if not n.startswith(('127.', '169.254.')) for i in range(1, 255) for p in ports]
    for name in ('motor-logger.local',):
        try:
            targets.append((socket.gethostbyname(name), ports[0]))
        except OSError:
            pass
    # boards usually accept one client only: probing one we already stream from would kick that stream off
    busy_hosts = set()
    for c in list(WIFI.values()):
        try:
            busy_hosts.add((socket.gethostbyname(c['host']), c['port']))
        except OSError:
            pass
    targets = [t for t in targets if t not in busy_hosts]
    found = set()

    def probe(t):
        try:
            socket.create_connection(t, timeout=0.4).close()
            found.add(t)
        except OSError:
            pass
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(128) as ex:
        list(ex.map(probe, targets))
    return [{'host': h, 'port': p} for h, p in sorted(found)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'      # response ends when the connection closes, so output can stream

    def log_message(self, *a):
        pass

    def trusted(self):
        # only our own page may call the API: blocks other websites and DNS-rebinding tricks
        return self.headers.get('Host', '') in (f'localhost:{PORT}', f'127.0.0.1:{PORT}')

    def reply(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def stream(self, cmds):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        rc = 0
        try:
            for cmd in cmds:
                self.wfile.write(('$ ' + ' '.join([os.path.basename(cmd[0])] + cmd[1:]) + '\n').encode())
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT, creationflags=NOWIN)
                try:
                    while True:
                        chunk = p.stdout.read1(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    p.kill()
                    return
                rc = p.wait()
                if rc:
                    break
            self.wfile.write(f'\n::exit {rc}\n'.encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {'error': 'forbidden'})
        path = self.path.split('?')[0]
        if path in PAGES:
            with open(os.path.join(ROOT, PAGES[path]), 'rb') as f:
                return self.reply(200, f.read(), 'text/html; charset=utf-8')
        if path == '/api/status':
            cli = cli_path()
            st = {'cli': None, 'version': '', 'cores': [], 'ai': bool(ai_key()), 'model': AI_MODEL}
            if cli:
                try:
                    v = run_json(['version'])
                    st['cli'] = cli
                    st['version'] = (v or {}).get('VersionString', '') if isinstance(v, dict) else ''
                    c = run_json(['core', 'list']) or []
                    plats = c.get('platforms', []) if isinstance(c, dict) else c
                    st['cores'] = [p.get('id') for p in plats]
                except Exception as e:
                    st['error'] = str(e)
            return self.reply(200, st)
        if path == '/api/wifi-scan':
            ports = [int(x) for x in re.findall(r'\d+', self.path.partition('?ports=')[2])[:5] if 0 < int(x) < 65536] or [3333]
            return self.reply(200, {'boards': wifi_scan(ports)})
        if path == '/api/ports':
            try:
                d = run_json(['board', 'list']) or []
            except Exception as e:
                return self.reply(200, {'error': str(e), 'ports': []})
            items = d.get('detected_ports', []) if isinstance(d, dict) else d
            ports = []
            for it in items:
                p = it.get('port', {})
                if p.get('protocol') not in (None, 'serial'):
                    continue
                pr = p.get('properties') or {}
                ports.append({'address': p.get('address'), 'vid': norm_id(pr.get('vid')), 'pid': norm_id(pr.get('pid')),
                              'boards': [b.get('name') for b in it.get('matching_boards', [])]})
            return self.reply(200, {'ports': ports})
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.trusted() or self.headers.get('X-Dev-Board') != '1':
            return self.reply(403, {'error': 'forbidden'})
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            return self.reply(400, {'error': 'bad request'})
        path = self.path.split('?')[0]
        if path.startswith('/api/ai'):
            return self.ai_post(path, body)
        if path.startswith('/api/wifi'):
            return self.wifi_post(path, body)
        if not busy.acquire(blocking=False):
            return self.reply(409, {'error': 'another job is running'})
        try:
            if path == '/api/install-cli':
                return self.install_cli()
            cli = cli_path()
            if not cli:
                return self.reply(400, {'error': 'arduino-cli is not installed'})
            if path == '/api/core':
                cid = str(body.get('id', ''))
                if not re.fullmatch(r'[\w.\-]+:[\w.\-]+', cid):
                    return self.reply(400, {'error': 'bad core id'})
                extra = ['--additional-urls', CORE_URLS[cid]] if cid in CORE_URLS else []
                return self.stream([[cli, 'core', 'update-index', *extra], [cli, 'core', 'install', cid, *extra]])
            if path == '/api/run':
                fqbn, port, code = str(body.get('fqbn', '')), body.get('port'), body.get('code', '')
                if not FQBN_RE.match(fqbn) or not isinstance(code, str) or len(code) > 2_000_000:
                    return self.reply(400, {'error': 'bad request'})
                if port is not None and not PORT_RE.match(str(port)):
                    return self.reply(400, {'error': 'bad port'})
                os.makedirs(SKETCH, exist_ok=True)
                with open(os.path.join(SKETCH, 'sketch.ino'), 'w', encoding='utf-8', newline='\n') as f:
                    f.write(code)
                cmd = [cli, 'compile', '--fqbn', fqbn, '--export-binaries', SKETCH]
                if port:
                    cmd += ['--upload', '--port', str(port)]
                return self.stream([cmd])
            self.reply(404, {'error': 'not found'})
        finally:
            busy.release()

    def ai_post(self, path, body):
        global _ai_key
        if path == '/api/ai-key':
            key = str(body.get('key', '')).strip()
            if not re.fullmatch(r'[\w\-]{20,300}', key):
                return self.reply(400, {'error': 'that does not look like an API key'})
            _ai_key = key
            if body.get('remember'):
                os.makedirs(TOOLS, exist_ok=True)
                with open(KEY_FILE, 'w') as f:
                    f.write(key)
            elif os.path.exists(KEY_FILE):
                os.remove(KEY_FILE)
            return self.reply(200, {'ok': True})
        if path == '/api/ai-key/clear':
            _ai_key = None
            if os.path.exists(KEY_FILE):
                os.remove(KEY_FILE)
            return self.reply(200, {'ok': True})
        # /api/ai : stream generated code back as plain text
        key = ai_key()
        if not key:
            return self.reply(400, {'error': 'no API key set'})
        parts = []
        for name in ('board', 'lang'):
            parts.append(f"{name}: {str(body.get(name, ''))[:200]}")
        if body.get('code'):
            parts.append('Current code:\n```\n' + str(body['code'])[:60000] + '\n```')
        if body.get('log'):
            parts.append('Recent serial monitor / compiler output:\n```\n' + str(body['log'])[-6000:] + '\n```')
        parts.append('Request: ' + str(body.get('prompt', ''))[:4000])
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', method='POST', data=json.dumps({
            'model': AI_MODEL, 'max_tokens': 8000, 'stream': True, 'system': AI_SYSTEM,
            'messages': [{'role': 'user', 'content': '\n\n'.join(parts)}],
        }).encode(), headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        w = lambda s: (self.wfile.write(s.encode()), self.wfile.flush())
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                for raw in r:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line.startswith('data:'):
                        continue
                    ev = json.loads(line[5:])
                    if ev.get('type') == 'content_block_delta' and ev['delta'].get('type') == 'text_delta':
                        w(ev['delta']['text'])
                    elif ev.get('type') == 'error':
                        raise RuntimeError(ev.get('error', {}).get('message', 'API error'))
            w('\n::exit 0\n')
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get('error', {}).get('message', '')
            except Exception:
                detail = ''
            w(f'\nError: Anthropic API {e.code} {detail}\n::exit 1\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            w(f'\nError: {e}\n::exit 1\n')

    def wifi_post(self, path, body):
        # TCP bridge for boards that print over WiFi (e.g. an ESP32 listening on port 3333)
        sid = str(body.get('id', ''))
        if not re.fullmatch(r'[\w\-]{1,40}', sid):
            return self.reply(400, {'error': 'bad id'})
        if path == '/api/wifi-send':
            conn = WIFI.get(sid)
            if not conn or not conn['sock']:
                return self.reply(409, {'error': 'not connected'})
            try:
                conn['sock'].sendall(str(body.get('data', '')).encode())
            except OSError as e:
                return self.reply(409, {'error': str(e)})
            return self.reply(200, {'ok': True})
        if path == '/api/wifi-close':
            conn = WIFI.pop(sid, None)
            if conn:
                conn['stop'] = True
                try:
                    conn['sock'].close()
                except Exception:
                    pass
            return self.reply(200, {'ok': True})
        host, port = str(body.get('host', '')), body.get('port')
        if not re.fullmatch(r'[\w.\-]{1,253}', host) or not isinstance(port, int) or not 0 < port < 65536:
            return self.reply(400, {'error': 'bad host or port'})
        # /api/wifi : stream everything the board sends; reconnects by itself, like the CSV logger script
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        conn = {'sock': None, 'stop': False, 'host': host, 'port': port}
        WIFI[sid] = conn
        w = lambda b: (self.wfile.write(b), self.wfile.flush())
        csv_file = csv_writer = None
        try:
            while not conn['stop']:
                try:
                    s = socket.create_connection((host, port), timeout=10)
                    s.settimeout(30)                # no data for 30 s = treat as lost
                    conn['sock'] = s
                    w(f'# Connected to {host} port {port}\n'.encode())
                    buf = b''
                    while True:
                        data = s.recv(4096)
                        if not data:
                            raise ConnectionError('closed by board')
                        w(data)
                        buf += data
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            text = line.decode(errors='replace').strip()
                            if not text.startswith('PWM_pct:'):
                                continue
                            try:
                                v = dict(p.split(':', 1) for p in text.split(','))
                            except ValueError:
                                continue
                            if not csv_writer:
                                os.makedirs(LOGS, exist_ok=True)
                                name = os.path.join(LOGS, f'wifi_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv')
                                csv_file = open(name, 'w', newline='')
                                csv_writer = csv.writer(csv_file)
                                csv_writer.writerow(['pc_time', 'pwm_pct', 'direction', 'current_a', 'i_r_a', 'i_l_a'])
                                w(f'# Saving data to {name}\n'.encode())
                            now = datetime.datetime.now().isoformat(sep=' ', timespec='milliseconds')
                            csv_writer.writerow([now, v.get('PWM_pct'), v.get('Direction'),
                                                 v.get('Current_A'), v.get('I_R'), v.get('I_L')])
                            csv_file.flush()
                except (BrokenPipeError, ConnectionResetError) as e:
                    if conn['stop'] or isinstance(e, BrokenPipeError):
                        return
                    w(f'\n# Connection problem ({e}) - retrying in 5 s\n'.encode())
                except OSError as e:
                    if conn['stop']:
                        return
                    w(f'\n# Connection problem ({e}) - retrying in 5 s\n'.encode())
                conn['sock'] = None
                for _ in range(50):
                    if conn['stop']:
                        return
                    time.sleep(0.1)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            if WIFI.get(sid) is conn:
                del WIFI[sid]
            if csv_file:
                csv_file.close()
            try:
                if conn['sock']:
                    conn['sock'].close()
            except Exception:
                pass

    def install_cli(self):
        # download the official arduino-cli release into ./tools (nothing outside this folder is touched)
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        w = lambda t: (self.wfile.write(t.encode()), self.wfile.flush())
        try:
            os.makedirs(TOOLS, exist_ok=True)
            w('Looking up latest arduino-cli release…\n')
            rel = json.load(urllib.request.urlopen('https://api.github.com/repos/arduino/arduino-cli/releases/latest', timeout=30))
            want = 'Windows_64bit.zip' if os.name == 'nt' else ('macOS_ARM64.tar.gz' if sys.platform == 'darwin' else 'Linux_64bit.tar.gz')
            asset = next(a for a in rel['assets'] if a['name'].endswith(want))
            w(f"Downloading {asset['name']}…\n")
            archive = os.path.join(TOOLS, asset['name'])
            urllib.request.urlretrieve(asset['browser_download_url'], archive)
            if archive.endswith('.zip'):
                with zipfile.ZipFile(archive) as z:
                    with z.open('arduino-cli.exe') as src, open(LOCAL_CLI, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
            else:
                import tarfile
                with tarfile.open(archive) as t:
                    t.extract('arduino-cli', TOOLS)
                os.chmod(LOCAL_CLI, 0o755)
            os.remove(archive)
            w('Installed ' + LOCAL_CLI + '\n::exit 0\n')
        except Exception as e:
            w(f'Install failed: {e}\n::exit 1\n')


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Dev Board: http://localhost:{PORT}/')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
