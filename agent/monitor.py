"""AI monitor for the motor logger: watches the live MQTT stream, lets Claude judge it, alerts on Telegram.

    pip install paho-mqtt anthropic
    python monitor.py

No fixed thresholds: every few minutes Claude gets a digest of the recent data, its own saved notes about
what is normal for this rig, and tools to dig deeper. It decides whether anything is wrong, and when to look next.
Telegram commands (from your chat only): /check  /status  /notes  or any free-text question.
"""
import collections, json, os, statistics, sys, threading, time, urllib.parse, urllib.request
import anthropic
import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
HISTORY = os.path.join(DATA, 'history.jsonl')
NOTES = os.path.join(DATA, 'notes.md')
ALERTS = os.path.join(DATA, 'alerts.jsonl')
os.makedirs(DATA, exist_ok=True)


def load_env():
    try:
        for line in open(os.path.join(HERE, '.env'), encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))
    except OSError:
        pass


load_env()
TOPIC = os.environ.get('MQTT_TOPIC', 'remster-hq/motor-logger-7f3a9c1e5b24')
BROKER = os.environ.get('MQTT_BROKER', 'broker.hivemq.com')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')
MODEL = os.environ.get('MONITOR_MODEL', 'claude-sonnet-5-5')
FIRST_CHECK_MIN = float(os.environ.get('CHECK_MINUTES', '10'))
FIELDS = ['pwm', 'dir', 'a', 'ir', 'il', 't1', 't2', 'rssi']

samples = collections.deque(maxlen=6 * 3600 + 600)      # 1 Hz, about 6 h
lock = threading.Lock()
state = {'online': None, 'last_rx': 0.0, 'next_check': time.time() + 60, 'checking': False}
client_ai = anthropic.Anthropic()


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------- storage ----------------

def load_history():
    cutoff = time.time() - 6 * 3600
    try:
        for line in open(HISTORY, encoding='utf-8'):
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s.get('ts', 0) >= cutoff:
                samples.append(s)
    except OSError:
        pass
    log(f'loaded {len(samples)} samples from disk')


def read_text(path, default=''):
    try:
        return open(path, encoding='utf-8').read()
    except OSError:
        return default


def recent_alerts(n=8):
    lines = read_text(ALERTS).splitlines()[-n:]
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


# ---------------- telegram ----------------

def tg_call(method, **params):
    url = f'https://api.telegram.org/bot{TG_TOKEN}/{method}'
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=40) as r:
        return json.load(r)


def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        log('[telegram not configured]', text[:200])
        return
    try:
        tg_call('sendMessage', chat_id=TG_CHAT, text=text[:4000])
    except Exception as e:
        log('telegram send failed:', e)


def tg_listen():
    offset = 0
    while True:
        try:
            r = tg_call('getUpdates', offset=offset, timeout=30)
            for u in r.get('result', []):
                offset = u['update_id'] + 1
                m = u.get('message') or {}
                if str(m.get('chat', {}).get('id')) != str(TG_CHAT):
                    continue                                  # ignore everyone but the owner
                threading.Thread(target=on_message, args=((m.get('text') or '').strip(),), daemon=True).start()
        except Exception as e:
            log('telegram poll error:', e)
            time.sleep(5)


def on_message(text):
    if not text:
        return
    if text.startswith('/check'):
        tg_send('Running a check now...')
        run_check(manual=True)
    elif text.startswith('/status'):
        tg_send(quick_status())
    elif text.startswith('/notes'):
        tg_send(read_text(NOTES, '(no notes yet)'))
    elif text.startswith('/start') or text.startswith('/help'):
        tg_send('Commands: /check (analyse now), /status, /notes (what I have learned is normal). Or just ask a question about the rig.')
    else:
        tg_send(ask_agent(text))


def quick_status():
    with lock:
        last = samples[-1] if samples else None
    if not last:
        return 'No data received yet.'
    age = int(time.time() - last['ts'])
    t = lambda v: 'n/a' if v is None else f'{v:.1f}'
    return (f"Board {'online' if state['online'] else 'offline/unknown'}, last data {age}s ago ({last.get('time')})\n"
            f"PWM {last['pwm']}% dir {last['dir']} | {last['a']} A (R {last['ir']} / L {last['il']})\n"
            f"T1 {t(last['t1'])} C, T2 {t(last['t2'])} C | WiFi {last['rssi']} dBm | SD {'ok' if last['sd'] else 'NOT logging'}\n"
            f"Next automatic check in {max(0, int((state['next_check'] - time.time()) / 60))} min")


# ---------------- data tools for the agent ----------------

def window(start_min_ago, end_min_ago):
    now = time.time()
    lo, hi = now - start_min_ago * 60, now - end_min_ago * 60
    with lock:
        return [s for s in samples if lo <= s['ts'] <= hi]


def tool_get_series(start_min_ago, end_min_ago=0, max_points=60, fields=None):
    rows = window(start_min_ago, end_min_ago)
    fields = [f for f in (fields or ['pwm', 'dir', 'a', 'ir', 'il', 't1', 't2']) if f in FIELDS]
    if not rows:
        return {'error': 'no samples in that window'}
    max_points = max(5, min(int(max_points), 200))
    step = max(1, len(rows) // max_points)
    out = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        row = {'t': time.strftime('%H:%M:%S', time.localtime(chunk[0]['ts'])), 'n': len(chunk)}
        for f in fields:
            vals = [c[f] for c in chunk if c.get(f) is not None]
            row[f] = round(sum(vals) / len(vals), 2) if vals else None
            if len(vals) > 1:
                row[f + '_min'], row[f + '_max'] = round(min(vals), 2), round(max(vals), 2)
        out.append(row)
    return {'samples_in_window': len(rows), 'bucket_seconds_approx': step, 'rows': out}


def stats_of(rows):
    out = {'n': len(rows)}
    if not rows:
        return out
    for f in FIELDS:
        v = [r[f] for r in rows if r.get(f) is not None]
        out[f] = ({'min': round(min(v), 2), 'max': round(max(v), 2), 'mean': round(statistics.fmean(v), 2),
                   'std': round(statistics.pstdev(v), 3), 'distinct_values': len(set(v)), 'missing': len(rows) - len(v)}
                  if v else {'missing': len(rows)})
    for d in (1, -1, 0):
        sub = [r for r in rows if r.get('dir') == d]
        if len(sub) > 5:
            entry = {'n': len(sub), 'mean_a': round(statistics.fmean(r['a'] for r in sub), 2)}
            try:
                entry['corr_current_vs_pwm'] = round(statistics.correlation([r['pwm'] for r in sub], [r['a'] for r in sub]), 3)
            except (statistics.StatisticsError, ZeroDivisionError):
                entry['corr_current_vs_pwm'] = 'undefined (a series is constant)'
            out[f'when_dir_{d}'] = entry
    gaps = [round(b['ts'] - a['ts']) for a, b in zip(rows, rows[1:]) if b['ts'] - a['ts'] > 3]
    out['gaps_over_3s'] = {'count': len(gaps), 'longest_s': max(gaps) if gaps else 0}
    return out


def tool_get_stats(start_min_ago, end_min_ago=0):
    return stats_of(window(start_min_ago, end_min_ago))


def tool_compare(a_start, a_end, b_start, b_end):
    return {'window_a': stats_of(window(a_start, a_end)), 'window_b': stats_of(window(b_start, b_end))}


def tool_save_notes(text):
    with open(NOTES, 'w', encoding='utf-8') as f:
        f.write(text[:6000])
    return 'notes saved'


TOOLS = [
    {'name': 'get_series', 'description': 'Time series from the last hours, bucket-averaged with per-bucket min/max. Times are minutes before now. Fields: pwm, dir, a (total current), ir, il, t1, t2, rssi.',
     'input_schema': {'type': 'object', 'properties': {'start_min_ago': {'type': 'number'}, 'end_min_ago': {'type': 'number'},
                      'max_points': {'type': 'integer'}, 'fields': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['start_min_ago']}},
    {'name': 'get_stats', 'description': 'Summary statistics for a window: per-field min/max/mean/std/distinct values/missing, current-vs-PWM correlation per direction, and gaps in the data.',
     'input_schema': {'type': 'object', 'properties': {'start_min_ago': {'type': 'number'}, 'end_min_ago': {'type': 'number'}}, 'required': ['start_min_ago']}},
    {'name': 'compare_windows', 'description': 'Statistics of two windows side by side (e.g. last 10 min vs the hour before) to spot drift or change.',
     'input_schema': {'type': 'object', 'properties': {k: {'type': 'number'} for k in ('a_start', 'a_end', 'b_start', 'b_end')},
                      'required': ['a_start', 'a_end', 'b_start', 'b_end']}},
    {'name': 'save_notes', 'description': 'Overwrite your persistent notes (shown to you at every check). Keep what you learned about normal behaviour, known quirks, open concerns. Max about 6000 chars.',
     'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
]
REPORT_TOOL = {
    'name': 'report', 'description': 'Finish this check with your verdict. Use status "ok" when nothing new needs the owner\'s attention (nothing is sent). "warn" or "critical" sends a Telegram message.',
    'input_schema': {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['ok', 'warn', 'critical']},
        'title': {'type': 'string', 'description': 'One-line headline'},
        'analysis': {'type': 'string', 'description': 'What you saw, with the actual numbers and times, and your diagnosis'},
        'suggested_checks': {'type': 'string', 'description': 'Concrete things the owner should physically check or do, most likely first'},
        'next_check_minutes': {'type': 'number', 'description': 'When to look again (2-60). Sooner if something is developing.'}},
        'required': ['status', 'title', 'analysis', 'next_check_minutes']}}

HANDLERS = {'get_series': tool_get_series, 'get_stats': tool_get_stats, 'compare_windows': tool_compare, 'save_notes': tool_save_notes}

SYSTEM = """You are the resident monitoring engineer for a motor test bench, acting as data analyst, signal analyst and hardware specialist in one.

The rig (ESP32 firmware): an IBT-2 / BTS7960 H-bridge drives a DC motor. PWM 0-100 %, direction 1 = forward (RPWM), -1 = reverse (LPWM), 0 = stopped.
Current is sensed from the driver's R_IS and L_IS pins, each through a 10k/10k divider into the ESP32 ADC (ADC_11db, about 0-3.1 V usable), scaled by a rough constant; 'a' is ir + il.
Two 10k NTC thermistors (beta 3950, fixed series resistor) give t1 and t2 in deg C; null means the wire is open/shorted. The ESP32 runs an automatic ramp cycle
(forward ramp up, hold, ramp down, pause, then the same in reverse) unless someone sent manual commands. Data arrives about once per second over WiFi via MQTT; 'sd' says whether the SD card is logging.

You have NO fixed thresholds. Decide what is abnormal by reasoning about the physics and the data: learn this rig's normal from the history and your notes, then look for anything wrong or changing.
Think about: values that do not respond when they should (stuck or saturated sensors, zero variance, current that ignores PWM), physically implausible numbers, drift and trends, asymmetry between R and L,
relationships that broke (current vs PWM, temperature vs load and its rate of rise), noise or spikes, dropouts and gaps, WiFi signal degradation, SD card trouble, the motor not following the expected cycle, and anything else a careful engineer would notice.
Also consider that the sensor or calibration may be at fault rather than the motor: say which you suspect and why.

Method: read the digest, pull whatever extra data you need with the tools (use several, compare windows), then call report exactly once. Be concrete: cite numbers and times, give a diagnosis with your confidence, and the most likely physical checks.
Do not repeat an issue already in 'recent alerts' unless it changed, worsened or is newly urgent; if it is unchanged, report ok. Keep your notes up to date with save_notes when you learn something about normal behaviour or open concerns.
If the data is too limited to judge (e.g. the rig has just started), say so and ask for a sooner re-check."""


# ---------------- agent loop ----------------

def digest():
    now = time.time()
    with lock:
        n = len(samples)
        last = samples[-1] if samples else None
    return json.dumps({
        'now_local': time.strftime('%Y-%m-%d %H:%M:%S'),
        'board_online_flag': state['online'],
        'seconds_since_last_sample': round(now - last['ts']) if last else None,
        'samples_held': n,
        'oldest_sample_minutes_ago': round((now - samples[0]['ts']) / 60, 1) if n else None,
        'latest_sample': last,
        'stats_last_10_min': stats_of(window(10, 0)),
        'stats_previous_50_min': stats_of(window(60, 10)),
        'recent_alerts_already_sent': recent_alerts(),
        'your_saved_notes': read_text(NOTES, '(none yet - this is a fresh start)'),
    }, default=str)


def run_tools(content):
    results = []
    for b in content:
        if b.type != 'tool_use' or b.name == 'report':
            continue
        try:
            res = HANDLERS[b.name](**b.input)
        except Exception as e:
            res = {'error': f'{type(e).__name__}: {e}'}
        results.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': json.dumps(res, default=str)})
    return results


def agent(messages, tools, system=SYSTEM, max_steps=10):
    """Run the tool loop. Returns the 'report' input dict if the model reported, else its final text."""
    for _ in range(max_steps):
        resp = client_ai.messages.create(model=MODEL, max_tokens=3000, system=system, tools=tools, messages=messages)
        messages.append({'role': 'assistant', 'content': resp.content})
        rep = next((b for b in resp.content if b.type == 'tool_use' and b.name == 'report'), None)
        if rep:
            return rep.input
        results = run_tools(resp.content)
        if not results:
            return ''.join(b.text for b in resp.content if b.type == 'text')
        messages.append({'role': 'user', 'content': results})
    return 'ran out of analysis steps'


def run_check(manual=False):
    if state['checking']:
        return
    state['checking'] = True
    try:
        with lock:
            have = len(samples)
        if have < 5:
            if manual:
                tg_send('Not enough data yet (is the board online and publishing?).')
            state['next_check'] = time.time() + 120
            return
        log('running check...')
        rep = agent([{'role': 'user', 'content': 'Periodic check. Digest:\n' + digest()}], TOOLS + [REPORT_TOOL])
        if isinstance(rep, str):
            log('no structured report:', rep[:200])
            state['next_check'] = time.time() + FIRST_CHECK_MIN * 60
            if manual:
                tg_send(rep)
            return
        mins = min(60, max(2, float(rep.get('next_check_minutes', FIRST_CHECK_MIN))))
        state['next_check'] = time.time() + mins * 60
        log(f"verdict {rep['status']}: {rep['title']} (next in {mins:.0f} min)")
        if rep['status'] != 'ok' or manual:
            icon = {'ok': 'OK', 'warn': 'WARNING', 'critical': 'CRITICAL'}[rep['status']]
            msg = f"[{icon}] {rep['title']}\n\n{rep['analysis']}"
            if rep.get('suggested_checks'):
                msg += f"\n\nCheck:\n{rep['suggested_checks']}"
            tg_send(msg)
            if rep['status'] != 'ok':
                with open(ALERTS, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'status': rep['status'],
                                        'title': rep['title'], 'summary': rep['analysis'][:400]}) + '\n')
    except Exception as e:
        log('check failed:', type(e).__name__, e)
        state['next_check'] = time.time() + 300
    finally:
        state['checking'] = False


def ask_agent(question):
    try:
        return agent([{'role': 'user', 'content': f'The owner asks via Telegram: {question}\n\nCurrent digest:\n{digest()}\n\nAnswer concisely in plain text.'}],
                     TOOLS, system=SYSTEM + '\n\nNow you are answering a direct question: do not call report; just answer in plain text (under 1500 characters).') or '(no answer)'
    except Exception as e:
        return f'Sorry, analysis failed: {e}'


# ---------------- mqtt ----------------

def on_connect(c, *args):
    log('connected to broker')
    c.subscribe([(TOPIC, 0), (TOPIC + '/online', 0)])


def on_message(c, _, msg):
    if msg.topic.endswith('/online'):
        online = msg.payload.decode() == '1'
        if state['online'] is not None and online != state['online']:
            tg_send('Board is back ONLINE.' if online else 'Board went OFFLINE (the broker reports it dropped off: power or WiFi/internet lost).')
            if online:
                state['next_check'] = min(state['next_check'], time.time() + 120)
        state['online'] = online
        return
    if msg.retain:
        return                                              # old snapshot from the broker, not live
    try:
        s = json.loads(msg.payload)
    except ValueError:
        return
    s['ts'] = time.time()
    state['last_rx'] = s['ts']
    with lock:
        samples.append(s)
    with open(HISTORY, 'a', encoding='utf-8') as f:
        f.write(json.dumps(s) + '\n')


def main():
    if not os.environ.get('ANTHROPIC_API_KEY'):
        sys.exit('Set ANTHROPIC_API_KEY (environment or agent/.env)')
    if not (TG_TOKEN and TG_CHAT):
        log('WARNING: Telegram not configured - alerts will only print here')
    load_history()
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        c = mqtt.Client()
    c.on_connect, c.on_message = on_connect, on_message
    c.reconnect_delay_set(2, 30)
    c.connect_async(BROKER, 1883, 30)
    c.loop_start()
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=tg_listen, daemon=True).start()
        tg_send('Motor monitor started. I will message you when I see something wrong. Send /help for commands.')
    state['next_check'] = time.time() + 90
    log('monitor running; first check in ~90 s')
    while True:
        time.sleep(5)
        if time.time() >= state['next_check']:
            run_check()


if __name__ == '__main__':
    main()
