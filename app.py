"""daily report bot - wecom + deepseek"""
import json, sqlite3, re, logging, hashlib, base64, struct, os, time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict, deque

import requests
from Crypto.Cipher import AES
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, Response

# Config
CORPID = os.getenv("CORPID", "ww6b69f2acd44566a0")
AGENT_ID = os.getenv("AGENT_ID", "1000002")
SECRET = os.getenv("SECRET", "3hcIumV_fGG73K_g9ARulKcQHmeKCVDN-V7Hwq7VRqc")
CALLBACK_TOKEN = os.getenv("CALLBACK_TOKEN", "QUBi4ewod7EH")
CALLBACK_AES_KEY = os.getenv("CALLBACK_AES_KEY", "2WYZkle4IWV3p9W0JGzlPZzRl0g5GCAPZv5N2p4X5tI")

# DeepSeek API (using DashScope-compatible endpoint)
AI_KEY = os.getenv("AI_KEY", "sk-53379df8a9944c04b1bd9f01f3a47bc5")
AI_URL = "https://api.deepseek.com/chat/completions"
AI_MODEL = "deepseek-chat"

PORT = int(os.getenv("PORT", "8888"))
DB_PATH = Path("data/reports.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Debug: store last 10 callbacks
CALLBACK_LOG = deque(maxlen=10)

# WX Crypt
class WXBizMsgCrypt:
    def __init__(self, token, key):
        self.token = token; self.key = base64.b64decode(key + "=")
    def _sign(self, ts, nonce, enc):
        return hashlib.sha1("".join(sorted([self.token, ts, nonce, enc])).encode()).hexdigest()
    def verify_url(self, sig, ts, nonce, echostr):
        if self._sign(ts, nonce, echostr) != sig: return None
        c = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = c.decrypt(base64.b64decode(echostr)); pad = plain[-1]; plain = plain[:-pad]
        content = plain[16:]; length = struct.unpack(">I", content[:4])[0]
        return content[4:4+length].decode()
    def decrypt_msg(self, sig, ts, nonce, body):
        root = ET.fromstring(body); enc = root.find("Encrypt").text
        if self._sign(ts, nonce, enc) != sig: return None
        c = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = c.decrypt(base64.b64decode(enc)); pad = plain[-1]; plain = plain[:-pad]
        content = plain[16:]; length = struct.unpack(">I", content[:4])[0]
        return content[4:4+length].decode()

# DB
def get_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, chat_id TEXT DEFAULT '',
        user_name TEXT DEFAULT '', raw_message TEXT,
        workers TEXT DEFAULT '', machines TEXT DEFAULT '',
        materials TEXT DEFAULT '', completed TEXT DEFAULT '',
        tomorrow_plan TEXT DEFAULT '', notes TEXT DEFAULT '',
        image_urls TEXT DEFAULT '', created_at TEXT)""")
    conn.commit(); return conn

# AI
PROMPT = """Parse construction log into JSON. Fields:
- workers: workers count + type (e.g. "5 workers: 2 welders, 3 helpers")
- machines: equipment + count (e.g. "1 excavator, 1 bulldozer")
- materials: materials + amount (e.g. "30m3 concrete, 2T rebar")
- completed: what was accomplished today (concise)
- tomorrow_plan: plan for tomorrow (concise)
- notes: weather, issues, etc. Empty string if not mentioned.

Rules: Extract ALL mentioned info. Numbers must be preserved. Output ONLY JSON.

User message: {message}
JSON:"""

def extract(msg):
    try:
        r = requests.post(AI_URL,
            headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "messages": [{"role": "user", "content": PROMPT.format(message=msg)}],
                  "temperature": 0.1, "max_tokens": 800}, timeout=30)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{[\s\S]*\}', content)
            if m: return json.loads(m.group())
        else: logger.error(f"AI HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e: logger.error(f"AI err: {e}")
    return {}

# WeCom API
AT, AT_EXP = None, 0
def wecom_token():
    global AT, AT_EXP
    if AT and time.time() < AT_EXP: return AT
    r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                     params={"corpid": CORPID, "corpsecret": SECRET}, timeout=10)
    d = r.json()
    if d.get("errcode") == 0:
        AT, AT_EXP = d["access_token"], time.time() + d["expires_in"] - 300
        return AT
    raise Exception(f"Token fail: {d}")

def send_wecom(text):
    try:
        t = wecom_token()
        r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": t},
            json={"touser": "@all", "msgtype": "text", "agentid": AGENT_ID,
                  "text": {"content": text}}, timeout=10)
        logger.info(f"Sent: {r.json()}")
        return r.json()
    except Exception as e:
        logger.error(f"Send fail: {e}")
        return {"errcode": -1, "errmsg": str(e)}

# Reports
def daily_report(date_str):
    conn = get_db()
    rows = conn.execute("SELECT workers,machines,materials,completed,tomorrow_plan,notes FROM daily_logs WHERE date=?", (date_str,)).fetchall(); conn.close()
    if not rows: return
    w = [r[0] for r in rows if r[0]]; ma = [r[1] for r in rows if r[1]]; mt = [r[2] for r in rows if r[2]]
    c = [r[3] for r in rows if r[3]]; p = [r[4] for r in rows if r[4]]; n = [r[5] for r in rows if r[5]]
    rpt = f"[Daily Report {date_str}]\n"
    rpt += f"Workers: {'; '.join(w) if w else 'N/A'}\n"
    rpt += f"Machines: {'; '.join(ma) if ma else 'N/A'}\n"
    rpt += f"Materials: {'; '.join(mt) if mt else 'N/A'}\n"
    rpt += "Completed:\n" + ("\n".join("- "+x for x in c) if c else "- N/A") + "\n"
    rpt += "Plan:\n" + ("\n".join("- "+x for x in p) if p else "- N/A") + "\n"
    rpt += "Notes: " + ("; ".join(n) if n else "N/A")
    send_wecom(rpt)

def weekly_report():
    today = date.today(); mon = today - timedelta(days=today.weekday()); sun = mon + timedelta(days=6)
    conn = get_db()
    rows = conn.execute("SELECT date,workers,machines,materials,completed FROM daily_logs WHERE date>=? AND date<=? ORDER BY date", (mon.isoformat(), sun.isoformat())).fetchall(); conn.close()
    if not rows: return
    bd = defaultdict(list)
    for r in rows: bd[r[0]].append(r[1:])
    daily=""; tw=0; ac=[]
    for d in sorted(bd):
        es=bd[d]
        for e in es:
            if e[0]: tw+=sum(int(n) for n in re.findall(r'(\d+)',e[0]))
            if e[3]: ac.append(e[3])
        done="; ".join(e[3] for e in es if e[3])
        daily+="- "+d+": "+(done or "N/A")+"\n"
    rpt = f"[Weekly Report {mon}~{sun}]\nDaily:\n{daily}Total workers: {tw}\n"
    rpt += "Completed:\n" + ("\n".join("- "+x for x in ac) if ac else "- N/A") + "\n"
    rpt += "Next: optimize resources, safety first"
    send_wecom(rpt)

# Routes
@app.route("/wecom", methods=["GET"])
def wecom_verify():
    try:
        sig = request.args.get("msg_signature",""); ts = request.args.get("timestamp","")
        nonce = request.args.get("nonce",""); echo = request.args.get("echostr","")
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "GET", "body": f"sig={sig[:20]} ts={ts}"})
        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        r = w.verify_url(sig, ts, nonce, echo)
        if r: return Response(r, mimetype="text/plain")
        return "fail", 403
    except Exception as e:
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "GET", "body": f"ERROR: {e}"})
        return "fail", 403

@app.route("/wecom", methods=["POST"])
def wecom_callback():
    try:
        body = request.data.decode()
        sig = request.args.get("msg_signature",""); ts = request.args.get("timestamp","")
        nonce = request.args.get("nonce","")
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "POST", "body": body[:300]})

        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        dec = w.decrypt_msg(sig, ts, nonce, body)
        root = ET.fromstring(dec)

        msg_type = root.find("MsgType")
        msg_text = ""
        if msg_type is not None:
            if msg_type.text == "text":
                tc = root.find("Text/Content")
                if tc is not None: msg_text = tc.text.strip()
            elif msg_type.text == "image":
                # Handle image - store URL
                pic = root.find("Image/PicUrl")
                img_url = pic.text if pic is not None else ""
                msg_text = f"[Image received: {img_url}]"

        logger.info(f"Callback msg: {msg_text[:200]}")

        if len(msg_text) >= 5 and not msg_text.startswith("[Image"):
            s = extract(msg_text)
            logger.info(f"Extracted: {s}")
            if s and any(s.values()):
                conn = get_db()
                conn.execute(
                    "INSERT INTO daily_logs(date,chat_id,user_name,raw_message,workers,machines,materials,completed,tomorrow_plan,notes,image_urls,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (date.today().isoformat(), "", "", msg_text,
                     s.get("workers",""), s.get("machines",""),
                     s.get("materials",""), s.get("completed",""),
                     s.get("tomorrow_plan",""), s.get("notes",""), "",
                     datetime.now().isoformat()))
                conn.commit(); conn.close()
                reply = f"[OK] {s.get('completed', msg_text[:50])}"
                send_wecom(reply)
            else:
                send_wecom("Please provide more details about today's work.")
        elif msg_text.startswith("[Image"):
            send_wecom("Image received. Send text describing the work in this photo.")
    except Exception as e:
        logger.error(f"Callback err: {e}")
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "POST", "body": f"ERROR: {e}"})
    return "success", 200

@app.route("/debug")
def debug():
    """查看最近收到的回调"""
    logs = list(CALLBACK_LOG)
    html = "<h3>Recent Callbacks</h3><pre>"
    for l in reversed(logs):
        html += f"{l['ts']} [{l['type']}] {l['body'][:200]}\n"
    html += "</pre>"
    return html

@app.route("/")
def index():
    return "OK - Daily Report Bot running. <a href=/debug>Debug</a>"

if __name__ == "__main__":
    get_db()
    s = BackgroundScheduler()
    s.add_job(lambda: daily_report(date.today().isoformat()), "cron", hour=20, minute=0)
    s.add_job(weekly_report, "cron", day_of_week="fri", hour=17, minute=0)
    s.start()
    logger.info("Scheduler: daily 20:00, weekly Fri 17:00")
    app.run(host="0.0.0.0", port=PORT)
