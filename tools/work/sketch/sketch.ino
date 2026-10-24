/*
  ESP32 test bench data logger:
    - IBT-2 (BTS7960) motor running an automatic test cycle
    - Motor current measurement
    - Two 10k NTC temperature sensors
    - Real time stamps, SD card logging
    - WiFi: TCP stream of all data + status to a PC program (port 3333)
  The temperature sensors are only logged - they do NOT control the motor.
  -------------------------------------------------------------------------------------
  Motor driver pins:
    D25 -> R_EN + L_EN   (enable)
    D27 -> RPWM          (forward)
    D26 -> LPWM          (reverse)
    D34 <- R_IS          (via 10k/10k divider)
    D35 <- L_IS          (via 10k/10k divider)

  NTC temperature sensors:
    3V3 -> fixed resistor -> D32 -> NTC1 -> GND
    3V3 -> fixed resistor -> D33 -> NTC2 -> GND

  SD card module (SPI):
    D5  -> CS
    D18 -> SCK
    D23 <- MISO   (custom pin mapping)
    D19 -> MOSI   (custom pin mapping)
    3V3/5V -> VCC (check your module), GND -> GND

  WiFi:
    PC program connects to:  <ESP32 IP>:3333   or   motor-logger.local:3333
    Data lines, status and commands work exactly like the USB Serial Monitor.
    If WiFi drops, SD logging continues and WiFi reconnects automatically.

  Commands (USB Serial Monitor at 115200 "Newline", or from the PC program):
    Motor:  f150, r100, s, a
    SD:     list, last, dump <n>, del <n>, sd (reconnect SD card)
    Time:   time, time 2026-10-24 22:30:00
    Other:  ip, status, q (live data on/off)

  Live data line format:
    Time:2026-10-24 22:30:00.123,PWM_pct:58.8,Direction:1,Current_A:1.23,I_R:1.23,I_L:0.00,Temp1_C:31.5,Temp2_C:29.8

  Status line (on connect, then every 5 s over WiFi):
    # Status: time ... | mode AUTO | SD /log_003.csv | WiFi 192.168.1.45 (-58 dBm)

  SD card file format:
    datetime,time_ms,pwm_pct,direction,current_a,i_r_a,i_l_a,temp1_c,temp2_c
*/
#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <time.h>
#include <sys/time.h>
#include <stdarg.h>
#include <math.h>

// ---------- WiFi / time settings ----------
const char* WIFI_SSID   = "Remster HQ";          // 2.4 GHz network only
const char* WIFI_PASS   = "REMsterR&D";
const char* MDNS_NAME   = "motor-logger";        // reachable as motor-logger.local
const uint16_t TCP_PORT = 3333;                  // PC program connects to this port
const char* TZ_INFO     = "IST-5:30";            // India Standard Time
const char* NTP_SERVER1 = "pool.ntp.org";
const char* NTP_SERVER2 = "time.google.com";
const unsigned long WIFI_TIMEOUT_MS = 15000;     // wait at most 15 s at start-up
const unsigned long STATUS_MS       = 5000;      // status line to WiFi every 5 s (0 = off)

// ---------- Cloud dashboard (MQTT over the internet, no library needed) ----------
// The dashboard page (dashboard.html on GitHub Pages) subscribes to the same topic.
// Anyone who knows the topic can read it, so keep it unguessable.
const bool  CLOUD_ON        = true;
const char* MQTT_HOST       = "broker.hivemq.com";
const uint16_t MQTT_PORT    = 1883;
const char* MQTT_TOPIC      = "remster-hq/motor-logger-7f3a9c1e5b24";   // must match dashboard.html
const unsigned long CLOUD_MS = 1000;             // publish once per second

// Put the timestamp at the start of every live data line.
// Set to false if your plotter program can't handle text values.
const bool TIME_IN_LIVE_LINE = true;

// ---------- Pins ----------
#define EN_PIN      25
#define RPWM_PIN    27
#define LPWM_PIN    26
#define R_IS_PIN    34
#define L_IS_PIN    35
#define NTC1_PIN    32
#define NTC2_PIN    33
#define SD_CS_PIN   5
#define SD_SCK_PIN  18
#define SD_MISO_PIN 23
#define SD_MOSI_PIN 19

// ---------- Motor / current settings ----------
const int   PWM_FREQ        = 20000;  // 20 kHz: quiet, above hearing range
const int   PWM_RES         = 8;      // 8-bit -> 0..255
const float DIVIDER         = 2.0;    // 10k/10k divider halves the voltage
const float AMPS_PER_MV     = 0.0085; // rough; calibrate with a multimeter
const unsigned long LOG_MS  = 100;    // one data line every 100 ms

// ---------- NTC temperature settings ----------
const float SERIES_R  = 8000.0;       // fixed resistor - set to your actual resistor value
const float NOMINAL_R = 10000.0;      // NTC resistance at 25°C
const float NOMINAL_T = 25.0;         // °C
const float BETA      = 3950.0;       // from NTC datasheet
const float VCC_MV    = 3300.0;       // supply in millivolts

// ---------- SD logging ----------
const unsigned long FLUSH_MS = 1000;  // write buffered data to card every 1 s
const char* CSV_HEADER = "datetime,time_ms,pwm_pct,direction,current_a,i_r_a,i_l_a,temp1_c,temp2_c";
File logFile;
bool sdMounted = false;               // card detected
bool sdOk      = false;               // log file open and being written
char currentName[16] = "";            // e.g. "/log_003.csv"
unsigned long lastFlush = 0;
bool liveOutput = true;               // send live data lines (USB + WiFi)

// ---------- Network ----------
WiFiServer server(TCP_PORT);
WiFiClient client;                    // the connected PC program (one at a time)
String tcpBuf;                        // incoming command text from the PC
bool wifiEnabled = false;
bool wifiWasConnected = false;
Print* replyTo = &Serial;             // where command replies go (USB or PC)

// ---------- Time ----------
bool timeValid = false;               // true once real time is known

int  pwmValue  = 0;     // 0..255
int  direction = 0;     // 1 = forward, -1 = reverse, 0 = stop
bool autoMode  = true;

// ---------- Auto test cycle: ramp up, hold, ramp down, pause ----------
struct Step { int dir; int fromPwm; int toPwm; unsigned long ms; };
Step steps[] = {
  { 1,   0, 255, 4000},   // forward ramp up
  { 1, 255, 255, 3000},   // forward hold
  { 1, 255,   0, 4000},   // forward ramp down
  { 0,   0,   0, 2000},   // pause
  {-1,   0, 255, 4000},   // reverse ramp up
  {-1, 255, 255, 3000},   // reverse hold
  {-1, 255,   0, 4000},   // reverse ramp down
  { 0,   0,   0, 2000},   // pause
};
const int NUM_STEPS = sizeof(steps) / sizeof(steps[0]);
int stepIndex = 0;
unsigned long stepStart = 0;

void handleCommand(String cmd);       // defined further below

// ================= Output helpers =================

bool clientOk() { return client && client.connected(); }

// Message to everyone (USB + PC)
void say(const char *fmt, ...) {
  char buf[256];
  va_list a; va_start(a, fmt); vsnprintf(buf, sizeof(buf), fmt, a); va_end(a);
  Serial.print(buf);
  if (clientOk()) client.print(buf);
}

// Reply only to whoever sent the command
void reply(const char *fmt, ...) {
  char buf[256];
  va_list a; va_start(a, fmt); vsnprintf(buf, sizeof(buf), fmt, a); va_end(a);
  replyTo->print(buf);
}

void printHelp(Print &p) {
  p.println("# Motor: f<0-255>, r<0-255>, s, a");
  p.println("# SD:    list, last, dump <n>, del <n>, sd = reconnect SD card");
  p.println("# Time:  time   |   time YYYY-MM-DD HH:MM:SS");
  p.println("# Other: ip | status | q = live data on/off");
}

// ================= Motor =================

void applyMotor() {
  if (direction == 1) {
    ledcWrite(LPWM_PIN, 0);
    ledcWrite(RPWM_PIN, pwmValue);
  } else if (direction == -1) {
    ledcWrite(RPWM_PIN, 0);
    ledcWrite(LPWM_PIN, pwmValue);
  } else {
    ledcWrite(RPWM_PIN, 0);
    ledcWrite(LPWM_PIN, 0);
  }
}

void runAuto() {
  unsigned long t = millis() - stepStart;
  if (t >= steps[stepIndex].ms) {
    stepIndex = (stepIndex + 1) % NUM_STEPS;
    stepStart = millis();
    t = 0;
  }
  Step &s = steps[stepIndex];
  direction = s.dir;
  pwmValue  = s.fromPwm + (long)(s.toPwm - s.fromPwm) * (long)t / (long)s.ms;
}

// Average several samples to smooth out the PWM ripple
float readAmps(int pin) {
  long sum = 0;
  for (int i = 0; i < 20; i++) sum += analogReadMilliVolts(pin);
  return (sum / 20.0) * DIVIDER * AMPS_PER_MV;
}

void stopMotor() {
  autoMode = false;
  direction = 0;
  pwmValue = 0;
  applyMotor();
}

// ================= Temperature =================

// Returns °C, or NAN if the sensor wire is loose or shorted.
float readTempC(int pin) {
  long sum = 0;
  for (int i = 0; i < 32; i++) sum += analogReadMilliVolts(pin);
  float v = sum / 32.0;
  if (v <= 5 || v >= VCC_MV - 5) return NAN;

  float r  = SERIES_R * v / (VCC_MV - v);   // NTC resistance
  float tK = 1.0 / (1.0 / (NOMINAL_T + 273.15) + log(r / NOMINAL_R) / BETA);
  return tK - 273.15;
}

// ================= Real time =================

// Fills buf with "2026-10-24 22:30:00.123", or "NO_TIME" if time is unknown
void getTimestamp(char *buf, size_t len) {
  if (!timeValid) { snprintf(buf, len, "NO_TIME"); return; }
  struct timeval tv;
  gettimeofday(&tv, NULL);
  struct tm tm;
  localtime_r(&tv.tv_sec, &tm);
  snprintf(buf, len, "%04d-%02d-%02d %02d:%02d:%02d.%03ld",
           tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
           tm.tm_hour, tm.tm_min, tm.tm_sec, (long)(tv.tv_usec / 1000));
}

// Set the clock by hand: "2026-10-24 22:30:00"
bool setTimeFromString(const String &s) {
  int Y, M, D, h, m, sec;
  if (sscanf(s.c_str(), "%d-%d-%d %d:%d:%d", &Y, &M, &D, &h, &m, &sec) != 6) return false;
  struct tm tm = {};
  tm.tm_year = Y - 1900; tm.tm_mon = M - 1; tm.tm_mday = D;
  tm.tm_hour = h;        tm.tm_min = m;     tm.tm_sec  = sec;
  tm.tm_isdst = 0;
  time_t t = mktime(&tm);                    // uses the IST time zone
  struct timeval tv = { t, 0 };
  settimeofday(&tv, NULL);
  timeValid = true;
  return true;
}

// One-line summary of everything
void printStatus(Print &p) {
  char ts[32];
  getTimestamp(ts, sizeof(ts));
  bool wifiUp = (WiFi.status() == WL_CONNECTED);
  p.printf("# Status: time %s | mode %s | SD %s | WiFi %s (%d dBm)\n",
           ts, autoMode ? "AUTO" : "MANUAL",
           sdOk ? currentName : "NOT logging",
           wifiUp ? WiFi.localIP().toString().c_str() : "not connected",
           wifiUp ? (int)WiFi.RSSI() : 0);
}

// ================= WiFi =================

void onWiFiConnected() {
  wifiWasConnected = true;
  say("# WiFi connected. PC program -> %s port %u  (or %s.local)\n",
      WiFi.localIP().toString().c_str(), TCP_PORT, MDNS_NAME);
  MDNS.end();
  if (MDNS.begin(MDNS_NAME)) MDNS.addService("telnet", "tcp", TCP_PORT);
}

void startWiFi() {
  if (strcmp(WIFI_SSID, "YOUR_WIFI_NAME") == 0 || strlen(WIFI_SSID) == 0) {
    say("# WiFi not configured - USB only. Set clock with 'time YYYY-MM-DD HH:MM:SS'\n");
    return;
  }
  wifiEnabled = true;
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);               // reconnect by itself if WiFi drops
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTzTime(TZ_INFO, NTP_SERVER1, NTP_SERVER2);  // time keeps re-syncing in background
  server.begin();
  server.setNoDelay(true);

  Serial.printf("# Connecting to WiFi '%s'", WIFI_SSID);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_MS) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    onWiFiConnected();
    struct tm tm;
    if (getLocalTime(&tm, 10000)) {
      timeValid = true;
      char ts[32];
      getTimestamp(ts, sizeof(ts));
      say("# Time synced from internet: %s\n", ts);
    } else {
      say("# Time server not answering yet - will keep trying\n");
    }
  } else {
    say("# WiFi not connected yet - will keep trying (SD logging still works)\n");
  }
}

// Runs every loop: watch WiFi, accept the PC, read its commands, send status
void handleWiFi() {
  if (!wifiEnabled) return;

  bool connected = (WiFi.status() == WL_CONNECTED);
  if (connected && !wifiWasConnected) onWiFiConnected();
  if (!connected && wifiWasConnected) {
    wifiWasConnected = false;
    Serial.println("# WiFi lost - SD logging continues, reconnecting...");
  }

  // Time arrives later if WiFi was slow at start-up
  if (!timeValid && time(nullptr) > 1700000000) {
    timeValid = true;
    say("# Time synced from internet\n");
  }

  // A PC program wants to connect
  if (server.hasClient()) {
    WiFiClient newClient = server.accept();
    if (clientOk()) {
      client.println("# Another PC connected - closing this connection");
      client.stop();
    }
    client = newClient;
    client.setNoDelay(true);
    tcpBuf = "";
    client.println("# Connected to ESP32 motor + temperature logger");
    printStatus(client);
    printHelp(client);
    Serial.printf("# PC connected from %s\n", client.remoteIP().toString().c_str());
  }

  // Read command text from the PC, one line at a time
  while (clientOk() && client.available()) {
    char ch = client.read();
    if (ch == '\n') {
      replyTo = &client;
      handleCommand(tcpBuf);
      replyTo = &Serial;
      tcpBuf = "";
    } else if (ch != '\r' && tcpBuf.length() < 100) {
      tcpBuf += ch;
    }
  }

  // Regular status line for the PC program
  static unsigned long lastStatus = 0;
  if (STATUS_MS > 0 && clientOk() && millis() - lastStatus >= STATUS_MS) {
    lastStatus = millis();
    printStatus(client);
  }
}

// ================= Cloud (minimal MQTT 3.1.1 publisher) =================

WiFiClient mqtt;
unsigned long mqttRetryAt = 0;

static size_t putLen(uint8_t *b, size_t n) {       // MQTT variable-length integer
  size_t i = 0;
  do { uint8_t d = n % 128; n /= 128; if (n) d |= 0x80; b[i++] = d; } while (n);
  return i;
}
static size_t putStr(uint8_t *b, const char *s) {
  size_t l = strlen(s);
  b[0] = l >> 8; b[1] = l & 0xFF;
  memcpy(b + 2, s, l);
  return l + 2;
}

bool mqttSend(uint8_t hdr, const uint8_t *body, size_t n) {
  uint8_t head[5]; head[0] = hdr;
  size_t hl = 1 + putLen(head + 1, n);
  return mqtt.write(head, hl) == hl && mqtt.write(body, n) == n;
}

bool mqttPublish(const char *topic, const char *msg) {
  uint8_t b[420];
  size_t n = putStr(b, topic);
  size_t m = strlen(msg);
  if (n + m > sizeof(b)) return false;
  memcpy(b + n, msg, m);
  return mqttSend(0x31, b, n + m);                  // 0x31 = PUBLISH, retained
}

bool mqttConnect() {
  mqtt.stop();
  mqtt.setTimeout(2);
  if (!mqtt.connect(MQTT_HOST, MQTT_PORT, 2000)) return false;
  char id[24], willTopic[96];
  snprintf(id, sizeof(id), "ml-%06x", (unsigned)(ESP.getEfuseMac() & 0xFFFFFF));
  snprintf(willTopic, sizeof(willTopic), "%s/online", MQTT_TOPIC);
  uint8_t b[300];
  size_t n = 0;
  const uint8_t vh[] = {0, 4, 'M', 'Q', 'T', 'T', 4, 0x26, 0, 30};   // clean session + retained will, keep-alive 30 s
  memcpy(b, vh, sizeof(vh)); n = sizeof(vh);
  n += putStr(b + n, id);
  n += putStr(b + n, willTopic);
  n += putStr(b + n, "0");                          // broker publishes this if we vanish
  if (!mqttSend(0x10, b, n)) return false;
  uint8_t ack[4];
  if (mqtt.readBytes(ack, 4) != 4 || ack[0] != 0x20 || ack[3] != 0) return false;
  char t[96];
  snprintf(t, sizeof(t), "%s/online", MQTT_TOPIC);
  mqttPublish(t, "1");
  return true;
}

// One JSON snapshot per second; reconnects by itself if the internet drops
void cloudPublish(float pwmPct, float iR, float iL, float t1, float t2) {
  if (!CLOUD_ON || WiFi.status() != WL_CONNECTED) return;
  while (mqtt.connected() && mqtt.available()) mqtt.read();   // discard anything the broker sends
  if (!mqtt.connected()) {
    if (millis() < mqttRetryAt) return;
    if (!mqttConnect()) { mqttRetryAt = millis() + 10000; return; }
  }
  char ts[32]; getTimestamp(ts, sizeof(ts));
  char t1s[12] = "null", t2s[12] = "null";
  if (!isnan(t1)) snprintf(t1s, sizeof(t1s), "%.1f", t1);
  if (!isnan(t2)) snprintf(t2s, sizeof(t2s), "%.1f", t2);
  char js[360];
  snprintf(js, sizeof(js),
    "{\"time\":\"%s\",\"pwm\":%.1f,\"dir\":%d,\"a\":%.2f,\"ir\":%.2f,\"il\":%.2f,"
    "\"t1\":%s,\"t2\":%s,\"mode\":\"%s\",\"sd\":%d,\"rssi\":%d,\"up\":%lu}",
    ts, pwmPct, direction, iR + iL, iR, iL, t1s, t2s, autoMode ? "AUTO" : "MANUAL",
    sdOk ? 1 : 0, (int)WiFi.RSSI(), millis() / 1000);
  if (!mqttPublish(MQTT_TOPIC, js)) mqtt.stop();
}

// ================= SD setup / reconnect =================

void makeName(int n, char *buf, size_t len) {
  snprintf(buf, len, "/log_%03d.csv", n);
}

void initSD() {
  SPI.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);
  if (!SD.begin(SD_CS_PIN, SPI)) {
    say("# SD card init FAILED - no SD logging. Fix card/wiring, then type 'sd'\n");
    return;
  }
  sdMounted = true;

  for (int n = 1; n < 1000; n++) {
    makeName(n, currentName, sizeof(currentName));
    if (!SD.exists(currentName)) break;
  }
  logFile = SD.open(currentName, FILE_WRITE);
  if (!logFile) {
    say("# Could not create log file\n");
    memset(currentName, 0, sizeof(currentName));
    return;
  }
  logFile.println(CSV_HEADER);
  logFile.flush();
  sdOk = true;
  say("# Logging to SD file %s\n", currentName);
}

// 'sd' command: close everything and start the card again (new log file)
void reconnectSD() {
  say("# Reconnecting SD card...\n");
  if (sdOk) logFile.close();
  sdOk = false;
  sdMounted = false;
  memset(currentName, 0, sizeof(currentName));
  SD.end();
  delay(100);
  initSD();
}

// ================= SD card viewing commands =================

bool sdReady() {
  if (!sdMounted) { reply("# No SD card available (type 'sd' to retry)\n"); return false; }
  return true;
}

void listFiles() {
  if (!sdReady()) return;
  File root = SD.open("/");
  if (!root) { reply("# Could not open card\n"); return; }
  reply("# ---- Files on SD card ----\n");
  int count = 0;
  File f = root.openNextFile();
  while (f) {
    if (!f.isDirectory()) {
      bool isCurrent = (strcmp(f.name(), currentName + 1) == 0);  // name() has no leading '/'
      reply("#   %-14s %10u bytes%s\n", f.name(), (unsigned)f.size(),
            isCurrent ? "   <- current" : "");
      count++;
    }
    f.close();
    f = root.openNextFile();
  }
  root.close();
  reply("# ---- %d file(s) ----\n", count);
}

void dumpFile(int n) {
  if (!sdReady()) return;
  char name[16];
  makeName(n, name, sizeof(name));
  if (!SD.exists(name)) { reply("# %s not found (try 'list')\n", name); return; }
  if (sdOk && strcmp(name, currentName) == 0) logFile.flush();  // include latest data

  File f = SD.open(name, FILE_READ);
  if (!f) { reply("# Could not open %s\n", name); return; }
  reply("# ---- BEGIN %s (%u bytes) ----\n", name, (unsigned)f.size());
  uint8_t buf[512];
  while (f.available()) {
    size_t got = f.read(buf, sizeof(buf));
    replyTo->write(buf, got);
  }
  f.close();
  reply("# ---- END %s ----\n", name);
}

void showLast() {
  if (!sdReady()) return;
  if (!sdOk) { reply("# No active log file\n"); return; }
  logFile.flush();
  File f = SD.open(currentName, FILE_READ);
  if (!f) { reply("# Could not open current log\n"); return; }

  size_t size  = f.size();
  size_t start = (size > 1500) ? size - 1500 : 0;
  f.seek(start);
  if (start > 0) {                       // skip the partial first line
    while (f.available() && f.read() != '\n') {}
  }
  reply("# ---- Last lines of %s ----\n", currentName);
  reply("# %s\n", CSV_HEADER);
  uint8_t buf[256];
  while (f.available()) {
    size_t got = f.read(buf, sizeof(buf));
    replyTo->write(buf, got);
  }
  f.close();
  reply("# ---- END ----\n");
}

void deleteFile(int n) {
  if (!sdReady()) return;
  char name[16];
  makeName(n, name, sizeof(name));
  if (strcmp(name, currentName) == 0) {
    reply("# Cannot delete the file currently being written\n");
    return;
  }
  if (!SD.exists(name)) { reply("# %s not found\n", name); return; }
  if (SD.remove(name)) reply("# Deleted %s\n", name);
  else                 reply("# Could not delete %s\n", name);
}

// ================= Command handling (USB and PC) =================

void handleCommand(String cmd) {
  cmd.trim();
  cmd.toLowerCase();
  if (cmd.length() == 0) return;

  // --- Time ---
  if (cmd == "time") {
    char ts[32];
    getTimestamp(ts, sizeof(ts));
    reply("# Current time: %s\n", ts);
    return;
  }
  if (cmd.startsWith("time ")) {
    if (setTimeFromString(cmd.substring(5))) {
      char ts[32];
      getTimestamp(ts, sizeof(ts));
      reply("# Time set: %s\n", ts);
    } else {
      reply("# Format: time 2026-10-24 22:30:00\n");
    }
    return;
  }

  // --- Network info / status ---
  if (cmd == "ip") {
    if (!wifiEnabled) reply("# WiFi not configured\n");
    else if (WiFi.status() == WL_CONNECTED)
      reply("# WiFi OK: %s port %u, signal %d dBm, PC %s\n",
            WiFi.localIP().toString().c_str(), TCP_PORT, WiFi.RSSI(),
            clientOk() ? "connected" : "not connected");
    else reply("# WiFi not connected (retrying)\n");
    return;
  }
  if (cmd == "status") { printStatus(*replyTo); return; }

  // --- SD card ---
  if (cmd == "sd")   { reconnectSD(); return; }
  if (cmd == "list") { listFiles();   return; }
  if (cmd == "last") { showLast();    return; }
  if (cmd.startsWith("dump")) { dumpFile(cmd.substring(4).toInt());   return; }
  if (cmd.startsWith("del"))  { deleteFile(cmd.substring(3).toInt()); return; }
  if (cmd == "q") {
    liveOutput = !liveOutput;
    reply("# Live data %s (SD logging continues)\n", liveOutput ? "ON" : "OFF");
    return;
  }

  // --- Motor ---
  char c   = cmd[0];
  int  val = constrain(cmd.substring(1).toInt(), 0, 255);
  int  newDir;

  if (c == 's') { stopMotor(); reply("# Motor stopped\n"); return; }
  if (c == 'a') { autoMode = true; stepIndex = 0; stepStart = millis(); reply("# Auto cycle started\n"); return; }
  if (c == 'f') newDir = 1;
  else if (c == 'r') newDir = -1;
  else { reply("# Unknown command\n"); return; }

  autoMode = false;
  // Changing direction while spinning: stop briefly first (protects motor and driver)
  if (newDir != direction && direction != 0) {
    pwmValue = 0;
    applyMotor();
    delay(300);
  }
  direction = newDir;
  pwmValue  = val;
  reply("# Manual: %s at PWM %d\n", newDir == 1 ? "forward" : "reverse", val);
}

void handleSerial() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  replyTo = &Serial;
  handleCommand(cmd);
}

// ================= Main =================

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(20);

  // Motor driver: outputs off while WiFi/time are being set up
  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, HIGH);
  ledcAttach(RPWM_PIN, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_PIN, PWM_FREQ, PWM_RES);
  applyMotor();                          // direction = 0 -> both outputs off
  analogSetAttenuation(ADC_11db);        // full 0-3.3V input range (all ADC pins)

  setenv("TZ", TZ_INFO, 1);              // India time for all date/time functions
  tzset();

  startWiFi();
  initSD();

  printHelp(Serial);
  stepStart = millis();
}

void loop() {
  handleSerial();
  handleWiFi();
  if (autoMode) runAuto();
  applyMotor();

  static unsigned long lastLog = 0;
  if (millis() - lastLog >= LOG_MS) {
    lastLog = millis();
    float iR = readAmps(R_IS_PIN);
    float iL = readAmps(L_IS_PIN);
    float t1 = readTempC(NTC1_PIN);
    float t2 = readTempC(NTC2_PIN);
    float pwmPct = pwmValue * 100.0 / 255.0;

    // One timestamp, used for both the live line and the SD card
    char ts[32];
    getTimestamp(ts, sizeof(ts));

    if (liveOutput) {
      char line[200];
      int n = 0;
      if (TIME_IN_LIVE_LINE) n = snprintf(line, sizeof(line), "Time:%s,", ts);
      snprintf(line + n, sizeof(line) - n,
               "PWM_pct:%.1f,Direction:%d,Current_A:%.2f,I_R:%.2f,I_L:%.2f,Temp1_C:%.1f,Temp2_C:%.1f\n",
               pwmPct, direction, iR + iL, iR, iL, t1, t2);
      Serial.print(line);
      if (clientOk()) client.print(line);
    }

    static unsigned long lastCloud = 0;
    if (millis() - lastCloud >= CLOUD_MS) {
      lastCloud = millis();
      cloudPublish(pwmPct, iR, iL, t1, t2);
    }

    if (sdOk) {
      size_t written = logFile.printf("%s,%lu,%.1f,%d,%.2f,%.2f,%.2f,%.1f,%.1f\n",
                                      ts, lastLog, pwmPct, direction,
                                      iR + iL, iR, iL, t1, t2);
      if (written == 0) {
        sdOk = false;
        say("# SD write failed - card removed? Type 'sd' to reconnect\n");
      } else if (millis() - lastFlush >= FLUSH_MS) {
        lastFlush = millis();
        logFile.flush();
      }
    }
  }
}