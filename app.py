"""
施工日报助手 �?Fly.io 部署�?企微群接收消�?�?AI提取 �?定时日报周报
"""
import json, sqlite3, re, logging, hashlib, base64, struct, os
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from Crypto.Cipher import AES
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, render_template_string, Response

# ==================== 配置 ====================
CORPID = os.getenv("CORPID", "ww6b69f2acd44566a0")
AGENT_ID = os.getenv("AGENT_ID", "1000002")
SECRET = os.getenv("SECRET", "3hcIumV_fGG73K_g9ARulKcQHmeKCVDN-V7Hwq7VRqc")
CALLBACK_TOKEN = os.getenv("CALLBACK_TOKEN", "QUBi4ewod7EH")
CALLBACK_AES_KEY = os.getenv("CALLBACK_AES_KEY", "2WYZkle4IWV3p9W0JGzlPZzRl0g5GCAPZv5N2p4X5tI")
DASHSCOPE_KEY = os.getenv("DASHSCOPE_KEY", "sk-1344d09e64dc400da78b691bc636c0bc")
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
PORT = int(os.getenv("PORT", "8888"))
DB_PATH = Path("data/reports.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

# ==================== 企微加解�?====================
class WXBizMsgCrypt:
    def __init__(self, token, key):
        self.token = token; self.key = base64.b64decode(key + "=")
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
    def _sign(self, ts, nonce, enc):
        return hashlib.sha1("".join(sorted([self.token, ts, nonce, enc])).encode()).hexdigest()

# ==================== 数据�?====================
def get_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, chat_id TEXT DEFAULT '',
        user_name TEXT DEFAULT '', raw_message TEXT,
        workers TEXT DEFAULT '�?, machines TEXT DEFAULT '�?,
        materials TEXT DEFAULT '�?, completed TEXT DEFAULT '�?,
        tomorrow_plan TEXT DEFAULT '�?, notes TEXT DEFAULT '�?,
        created_at TEXT)""")
    conn.commit(); return conn

# ==================== AI提取 ====================
PROMPT = """从施工口语提取JSON。字段：workers(�?工种)、machines(机械+�?、materials(材料+�?、completed(今日完成)、tomorrow_plan(明日计划)、notes(备注)。没提到的写"�?，只输出JSON，不要解释�?示例�?今天3�?台挖机基坑开挖完�?0%，明天继�?→{"workers":"3�?,"machines":"挖机1�?,"materials":"�?,"completed":"基坑开挖完�?0%","tomorrow_plan":"继续开�?,"notes":"�?}

用户消息：{message}
JSON:"""

def extract(msg):
    try:
        r = requests.post(DASHSCOPE_URL,
            headers={"Authorization": f"Bearer {DASHSCOPE_KEY}", "Content-Type": "application/json"},
            json={"model":"qwen-plus","messages":[{"role":"user","content":PROMPT.format(message=msg)}],"temperature":0.1,"max_tokens":800}, timeout=30)
        if r.status_code==200:
            c = r.json()["choices"][0]["message"]["content"]
            m = re.search(r'\{[\s\S]*\}', c)
            if m: return json.loads(m.group())
    except Exception as e: logger.error(f"AI: {e}")
    return {}

# ==================== 企微API ====================
AT, AT_EXP = None, 0
def wecom_token():
    global AT, AT_EXP
    import time
    if AT and time.time() < AT_EXP: return AT
    r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken", params={"corpid":CORPID,"corpsecret":SECRET}, timeout=10)
    d = r.json()
    if d.get("errcode")==0: AT, AT_EXP = d["access_token"], time.time()+d["expires_in"]-300; return AT
    raise Exception(f"Token: {d}")

def send_wecom(text):
    try:
        t = wecom_token()
        r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send", params={"access_token":t},
            json={"touser":"@all","msgtype":"markdown","agentid":AGENT_ID,"markdown":{"content":text}}, timeout=10)
        logger.info(f"推�? {r.json()}")
    except Exception as e: logger.error(f"推送失�? {e}")

# ==================== 报告生成 ====================
def daily_report(date_str):
    conn = get_db()
    rows = conn.execute("SELECT workers,machines,materials,completed,tomorrow_plan,notes FROM daily_logs WHERE date=?",(date_str,)).fetchall(); conn.close()
    if not rows: return
    w = [r[0] for r in rows if r[0]!="�?]; ma = [r[1] for r in rows if r[1]!="�?]; mt = [r[2] for r in rows if r[2]!="�?]
    c = [r[3] for r in rows if r[3]!="�?]; p = [r[4] for r in rows if r[4]!="�?]; n = [r[5] for r in rows if r[5]!="�?]
    rpt = f"""📅 **施工日报 - {date_str}**
>人材机：工人 {'; '.join(w) if w else '�?} | 机械 {'; '.join(ma) if ma else '�?} | 材料 {'; '.join(mt) if mt else '�?}
>今日完成：{chr(10).join(f'- {x}' for x in c) if c else '- �?}
>明日计划：{chr(10).join(f'- {x}' for x in p) if p else '- �?}
>备注：{'; '.join(n) if n else '�?}"""
    send_wecom(rpt)

def weekly_report():
    today = date.today(); mon = today - timedelta(days=today.weekday()); sun = mon + timedelta(days=6)
    conn = get_db()
    rows = conn.execute("SELECT date,workers,machines,materials,completed FROM daily_logs WHERE date>=? AND date<=? ORDER BY date",(mon.isoformat(),sun.isoformat())).fetchall(); conn.close()
    if not rows: return
    from collections import defaultdict
    bd = defaultdict(list)
    for r in rows: bd[r[0]].append(r[1:])
    daily=""; tw=0; ac=[]
    for d in sorted(bd):
        es=bd[d]
        for e in es:
            if e[0]!="�?: tw+=sum(int(n) for n in re.findall(r'(\d+)�?,e[0]))
            if e[3]!="�?: ac.append(e[3])
        done="; ".join(e[3] for e in es if e[3]!="�?)
        daily+=f"- {d}：{done or '无记�?}\n"
    rpt=f"""📊 **施工周报**（{mon}~{sun}�?>每日摘要
{daily}
>人工统计：用工约{tw}人次
>本周完成：{chr(10).join(f'- {x}' for x in ac) if ac else '- �?}
>下周建议：根据进度合理调配人材机，安全生�?""
    send_wecom(rpt)

# ==================== 回调路由 ====================
@app.route("/wecom", methods=["GET"])
def wecom_verify():
    try:
        logger.info(f"验证请求: sig={request.args.get('msg_signature','')[:20]}... ts={request.args.get('timestamp','')} nonce={request.args.get('nonce','')} echo={request.args.get('echostr','')[:30]}...")
        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        r = w.verify_url(request.args.get("msg_signature",""), request.args.get("timestamp",""),
            request.args.get("nonce",""), request.args.get("echostr",""))
        if r:
            logger.info("URL验证成功")
            return Response(r, mimetype="text/plain")
        logger.error("URL验证失败: 签名不匹配")
        return "fail", 403
    except Exception as e:
        logger.error(f"URL验证异常: {e}")
        return "fail", 403

@app.route("/wecom", methods=["POST"])
def wecom_callback():
    try:
        w = WXBizMsgCrypt(CALLBACK_TOKEN, CALLBACK_AES_KEY)
        dec = w.decrypt_msg(request.args.get("msg_signature",""), request.args.get("timestamp",""),
            request.args.get("nonce",""), request.data.decode())
        root = ET.fromstring(dec)
        msg = root.find("Text/Content").text.strip() if root.find("MsgType").text=="text" and root.find("Text/Content") is not None else ""
        if len(msg)<5: return "success", 200

        s = extract(msg)
        if not s or not any(v!="�? for v in s.values()): return "success", 200

        conn = get_db()
        conn.execute("INSERT INTO daily_logs(date,chat_id,user_name,raw_message,workers,machines,materials,completed,tomorrow_plan,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (date.today().isoformat(), "", "", msg, s.get("workers","�?), s.get("machines","�?),
             s.get("materials","�?), s.get("completed","�?), s.get("tomorrow_plan","�?),
             s.get("notes","�?), datetime.now().isoformat())); conn.commit(); conn.close()

        send_wecom(f"�?已记录\n完成：{s.get('completed','�?)}\n明日：{s.get('tomorrow_plan','�?)}")
    except Exception as e: logger.error(f"回调: {e}")
    return "success", 200

@app.route("/")
def index(): return "施工日报助手运行�?

# ==================== 启动 ====================
if __name__ == "__main__":
    get_db()
    s = BackgroundScheduler()
    s.add_job(lambda: daily_report(date.today().isoformat()), "cron", hour=20, minute=0)
    s.add_job(weekly_report, "cron", day_of_week="fri", hour=17, minute=0)
    s.start()
    logger.info("定时任务: 20:00日报 | 周五17:00周报")
    app.run(host="0.0.0.0", port=PORT)
