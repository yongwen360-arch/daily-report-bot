import json, sqlite3, re, logging, hashlib, base64, struct, os, time
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict, deque

import requests
from Crypto.Cipher import AES
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, Response

CORPID = os.getenv("CORPID", "ww6b69f2acd44566a0")
AGENT_ID = os.getenv("AGENT_ID", "1000002")
SECRET = os.getenv("SECRET", "3hcIumV_fGG73K_g9ARulKcQHmeKCVDN-V7Hwq7VRqc")
CALLBACK_TOKEN = os.getenv("CALLBACK_TOKEN", "QUBi4ewod7EH")
CALLBACK_AES_KEY = os.getenv("CALLBACK_AES_KEY", "2WYZkle4IWV3p9W0JGzlPZzRl0g5GCAPZv5N2p4X5tI")
AI_KEY = os.getenv("AI_KEY", "sk-53379df8a9944c04b1bd9f01f3a47bc5")
AI_URL = "https://api.deepseek.com/chat/completions"
PORT = int(os.getenv("PORT", "8888"))
DB_PATH = Path("data/reports.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)
CALLBACK_LOG = deque(maxlen=20)

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

PROMPT = """Extract construction site daily log from user's spoken description into JSON.
Fields:
- workers: number and types of workers (e.g. "5: 2 welders, 3 general workers")
- machines: equipment and count (e.g. "1 excavator, 1 bulldozer")
- materials: materials and amount used (e.g. "30m3 concrete, 2T rebar")
- completed: what was completed today
- tomorrow_plan: plan for tomorrow
- notes: weather, stoppages, special circumstances

Rules: extract ALL mentioned info, keep exact numbers, output ONLY valid JSON.
User: {message}
JSON:"""

def extract(msg):
    try:
        r = requests.post(AI_URL,
            headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": [{"role": "user", "content": PROMPT.format(message=msg)}],
                  "temperature": 0.1, "max_tokens": 800}, timeout=30)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{[\s\S]*\}', content)
            if m: return json.loads(m.group())
        else: logger.error(f"AI HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e: logger.error(f"AI err: {e}")
    return {}

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
    raise Exception(f"Token failed: {d}")

def send_wecom(text):
    try:
        t = wecom_token()
        requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": t},
            json={"touser": "@all", "msgtype": "text", "agentid": AGENT_ID,
                  "text": {"content": text}}, timeout=10)
    except Exception as e: logger.error(f"Send fail: {e}")

def daily_report(date_str):
    conn = get_db()
    rows = conn.execute(
        "SELECT workers,machines,materials,completed,tomorrow_plan,notes FROM daily_logs WHERE date=?",
        (date_str,)).fetchall(); conn.close()
    if not rows: return
    w = [r[0] for r in rows if r[0]]; ma = [r[1] for r in rows if r[1]]; mt = [r[2] for r in rows if r[2]]
    c = [r[3] for r in rows if r[3]]; p = [r[4] for r in rows if r[4]]; n = [r[5] for r in rows if r[5]]
    rpt = f"\U0001f4c5 Daily Report [{date_str}]\n"
    rpt += f"\U0001f477 Workers: {'; '.join(w) if w else 'N/A'}\n"
    rpt += f"\U0001f69c Machines: {'; '.join(ma) if ma else 'N/A'}\n"
    rpt += f"\U0001f4e6 Materials: {'; '.join(mt) if mt else 'N/A'}\n"
    rpt += f"✅ Completed:\n" + ("\n".join("- "+x for x in c) if c else "- N/A") + "\n"
    rpt += f"\U0001f4cb Tomorrow:\n" + ("\n".join("- "+x for x in p) if p else "- N/A") + "\n"
    rpt += f"\U0001f4dd Notes: " + ("; ".join(n) if n else "N/A")
    send_wecom(rpt)

def weekly_report():
    today = date.today(); mon = today - timedelta(days=today.weekday()); sun = mon + timedelta(days=6)
    conn = get_db()
    rows = conn.execute(
        "SELECT date,workers,machines,materials,completed FROM daily_logs WHERE date>=? AND date<=? ORDER BY date",
        (mon.isoformat(), sun.isoformat())).fetchall(); conn.close()
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
    rpt = f"\U0001f4ca Weekly Report [{mon} ~ {sun}]\n\nDaily:\n{daily}\n"
    rpt += f"Total Workers: {tw}\n"
    rpt += "Completed:\n" + ("\n".join("- "+x for x in ac) if ac else "- N/A") + "\n"
    rpt += "Next Week: Plan resources, ensure safety."
    send_wecom(rpt)

FORM = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>施工日报</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f0f2f5;min-height:100vh}
.hd{background:#07c160;color:#fff;padding:14px;text-align:center;font-size:17px;font-weight:bold}
.ct{padding:12px;max-width:480px;margin:0 auto}
.card{background:#fff;border-radius:10px;padding:14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.card h3{font-size:14px;color:#333;margin-bottom:8px}
textarea{width:100%;height:90px;border:1px solid #ddd;border-radius:8px;padding:10px;font-size:15px;font-family:inherit}
.btn{display:block;width:100%;padding:12px;background:#07c160;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:bold;cursor:pointer}
.btn:active{opacity:.8}
.toast{text-align:center;padding:10px;font-size:14px;display:none}
.toast.ok{color:#07c160}.toast.err{color:#e33}
.hist{margin-top:10px}.hist div{padding:8px 0;border-bottom:1px solid #eee;font-size:13px;color:#666}
.hist .t{color:#999;font-size:12px}
</style></head>
<body>
<div class="hd">施工日报</div>
<div class="ct">
  <div class="card">
    <h3>今天的工作内容</h3>
    <textarea id="msg" placeholder="例如：今天5个工人2台挖机，浇筑了30方混凝土，完成了1号楼基础，明天绑钢筋"></textarea>
    <button class="btn" onclick="submit()">提交</button>
    <div class="toast" id="toast"></div>
  </div>
  <div class="hist" id="history"></div>
</div>
<script>
function toast(m,c){var e=document.getElementById('toast');e.textContent=m;e.className='toast '+c;e.style.display='block';setTimeout(function(){e.style.display='none'},2500)}
async function submit(){
  var m=document.getElementById('msg').value.trim();
  if(m.length<5){toast('请写详细一些','err');return}
  var b=document.querySelector('.btn');b.disabled=true;b.textContent='提交中...';
  try{
    var r=await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});
    var d=await r.json();
    if(d.ok){document.getElementById('msg').value='';toast('已记录','ok');loadHistory()}
    else toast(d.error||'失败','err')
  }catch(e){toast('网络错误','err')}
  b.disabled=false;b.textContent='提交';
}
async function loadHistory(){
  try{
    var r=await fetch('/history');var d=await r.json();
    var h=document.getElementById('history');
    if(!d.items.length){h.innerHTML='<div style=text-align:center;color:#999;padding:20px>今天还没有记录</div>';return}
    h.innerHTML=d.items.map(function(i){return '<div><span class=t>'+i.time+'</span> '+i.text+'</div>'}).join('')
  }catch(e){}
}
loadHistory();
</script>
</body></html>"""

@app.route("/")
def index(): return FORM

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json()
    msg = data.get("message", "").strip()
    if len(msg) < 5: return {"ok": False, "error": "内容太短"}

    s = extract(msg)
    logger.info(f"Extract: {s}")
    if not s or not any(s.values()): return {"ok": False, "error": "未识别施工信息，请写详细些"}

    conn = get_db()
    conn.execute(
        "INSERT INTO daily_logs(date,chat_id,user_name,raw_message,workers,machines,materials,completed,tomorrow_plan,notes,image_urls,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (date.today().isoformat(), "", "", msg,
         s.get("workers",""), s.get("machines",""),
         s.get("materials",""), s.get("completed",""),
         s.get("tomorrow_plan",""), s.get("notes",""), "",
         datetime.now().isoformat()))
    conn.commit(); conn.close()

    reply = f"✅ Recorded\nCompleted: {s.get('completed', msg[:40])}\nTomorrow: {s.get('tomorrow_plan', 'N/A')}"
    send_wecom(reply)
    return {"ok": True}

@app.route("/history")
def history():
    conn = get_db()
    rows = conn.execute(
        "SELECT created_at, completed, raw_message FROM daily_logs WHERE date=? ORDER BY id DESC LIMIT 10",
        (date.today().isoformat(),)).fetchall(); conn.close()
    return {"items": [{"time": r[0][11:16], "text": r[1] or r[2][:40]} for r in rows]}

@app.route("/wecom", methods=["GET"])
def wecom_verify():
    try:
        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        r = w.verify_url(request.args.get("msg_signature",""), request.args.get("timestamp",""),
            request.args.get("nonce",""), request.args.get("echostr",""))
        if r: return Response(r, mimetype="text/plain")
        return "fail", 403
    except: return "fail", 403

@app.route("/wecom", methods=["POST"])
def wecom_callback():
    try:
        body = request.data.decode()
        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        dec = w.decrypt_msg(request.args.get("msg_signature",""), request.args.get("timestamp",""),
            request.args.get("nonce",""), body)
        root = ET.fromstring(dec)
        msg = ""
        if root.find("MsgType") is not None and root.find("MsgType").text == "text":
            tc = root.find("Text/Content")
            if tc is not None: msg = tc.text.strip()
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "POST", "msg": msg[:200]})
        if len(msg) >= 5:
            s = extract(msg)
            if s and any(s.values()):
                conn = get_db()
                conn.execute(
                    "INSERT INTO daily_logs(date,chat_id,user_name,raw_message,workers,machines,materials,completed,tomorrow_plan,notes,image_urls,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (date.today().isoformat(), "", "", msg,
                     s.get("workers",""), s.get("machines",""), s.get("materials",""),
                     s.get("completed",""), s.get("tomorrow_plan",""), s.get("notes",""), "",
                     datetime.now().isoformat()))
                conn.commit(); conn.close()
                send_wecom(f"OK: {s.get('completed', msg[:50])}")
    except Exception as e:
        logger.error(f"Callback ERR: {e}")
        CALLBACK_LOG.append({"ts": datetime.now().isoformat(), "type": "POST", "body": f"ERR: {e}"})
    return "success", 200

@app.route("/debug")
def debug():
    logs = list(CALLBACK_LOG)
    html = "<h3>Callbacks</h3><pre>"
    for l in reversed(logs):
        html += f"{l['ts']} [{l['type']}] {str(l.get('msg', l.get('body', '')))[:200]}\n"
    return html + "</pre>"

if __name__ == "__main__":
    get_db()
    s = BackgroundScheduler()
    s.add_job(lambda: daily_report(date.today().isoformat()), "cron", hour=20, minute=0)
    s.add_job(weekly_report, "cron", day_of_week="fri", hour=17, minute=0)
    s.start()
    logger.info("Scheduler: daily 20:00, weekly Fri 17:00")
    app.run(host="0.0.0.0", port=PORT)
