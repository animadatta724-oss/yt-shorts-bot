import os, re, json, glob, random, subprocess, asyncio, datetime, time, traceback, textwrap, urllib.parse
import xml.etree.ElementTree as ET
import requests, edge_tts
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

E = os.environ
W = "work"
os.makedirs(W, exist_ok=True)

VOICE = E.get("VOICE", "hi-IN-MadhurNeural")          # female: hi-IN-SwaraNeural
MODEL = E.get("GEMINI_MODEL", "gemini-2.5-flash")
NICHE = E.get("NICHE", "amazing facts: space, science, human body, animals, history, India, psychology")
DELAY = int(E.get("PUBLISH_DELAY_HOURS", "0"))         # >0 = private + scheduled publish
VISUALS = E.get("VISUALS", "ai")                       # ai | stock | mix
APPROVAL = E.get("TELEGRAM_APPROVAL", "0") == "1"      # ask on Telegram before upload
WAIT_MIN = int(E.get("APPROVAL_WAIT_MIN", "30"))
UA = {"User-Agent": "Mozilla/5.0 (yt-shorts-bot)"}
STYLES = ["question", "number", "shock"]
VOICES = ["hi-IN-MadhurNeural", "hi-IN-SwaraNeural"]
SERIES_NAME = E.get("SERIES_NAME", "रोचक तथ्य")
SERIES_TAG = E.get("SERIES_TAG", "Fact")
SMART_TIME = E.get("SMART_TIME", "1") == "1"            # best posting time finder
SLOTS = [int(x) for x in E.get("POST_SLOTS", "7,9,12,15,18,20,22").split(",")]  # IST hours
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SFX_ON = E.get("SFX", "1") == "1"
SFX_VOL = E.get("SFX_VOL", "0.35")
CAT_HI = {"space": "अंतरिक्ष", "science": "विज्ञान", "body": "इंसानी शरीर", "animals": "जानवर",
          "history": "इतिहास", "india": "भारत", "psychology": "मनोविज्ञान", "tech": "टेक्नोलॉजी", "other": "अनोखे तथ्य"}
TG_TOKEN, TG_CHAT = E.get("TELEGRAM_BOT_TOKEN"), E.get("TELEGRAM_CHAT_ID")


# ================= helpers =================
def run(cmd):
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(e.stderr.decode()[-1500:])
        raise


def dur(path):
    o = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(o.decode().strip())


def load_history():
    try:
        raw = json.load(open("history.json", encoding="utf-8"))
    except Exception:
        raw = []
    return [h if isinstance(h, dict) else {"title": h} for h in raw]


def save_history(h):
    json.dump(h, open("history.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def load_state():
    try:
        return json.load(open("state.json", encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    json.dump(st, open("state.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ================= Feature 10: Telegram =================
def tg(method, files=None, **kw):
    if not (TG_TOKEN and TG_CHAT):
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=kw, files=files, timeout=180)
        return r.json()
    except Exception as ex:
        print("telegram error", ex)
        return None


def notify(text):
    tg("sendMessage", chat_id=TG_CHAT, text=text[:3500])


def ask_approval(path, title):
    if not (APPROVAL and TG_TOKEN and TG_CHAT):
        return True
    last = 0
    r = tg("getUpdates", offset=-1)
    if r and r.get("result"):
        last = r["result"][-1]["update_id"]
    kb = json.dumps({"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "yes"},
                                          {"text": "❌ Reject", "callback_data": "no"}]]})
    with open(path, "rb") as f:
        tg("sendVideo", files={"video": f}, chat_id=TG_CHAT, reply_markup=kb,
           caption=f"{title}\n\nUpload karu? ({WAIT_MIN} min tak wait)")
    end = time.time() + WAIT_MIN * 60
    while time.time() < end:
        r = tg("getUpdates", offset=last + 1)
        for u in (r or {}).get("result", []):
            last = u["update_id"]
            cq = u.get("callback_query")
            if cq:
                tg("answerCallbackQuery", callback_query_id=cq["id"])
                return cq.get("data") == "yes"
        time.sleep(10)
    return E.get("AUTO_ON_TIMEOUT", "0") == "1"


# ================= Gemini =================
def pick_model():
    """Gemini ke available models me se sabse naya 'flash' model dhoondhta hai."""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": E["GEMINI_API_KEY"], "pageSize": 200}, timeout=60)
        names = [m["name"].replace("models/", "") for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as ex:
        print("model list error:", ex)
        return None
    bad = ("lite", "image", "tts", "live", "audio", "embedding", "robotics", "computer", "vision", "thinking")
    cand = [n for n in names if "flash" in n and not any(x in n for x in bad)]
    stable = [n for n in cand if not any(x in n for x in ("preview", "exp"))]
    pool = stable or cand

    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0
    return max(pool, key=lambda n: (ver(n), -len(n))) if pool else None


def gemini(prompt, search=False, temp=1.0):
    global MODEL
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temp}}
    if search:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = None
    for _ in range(4):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={E['GEMINI_API_KEY']}"
        r = requests.post(url, json=body, timeout=180)
        if r.status_code == 404:
            new = pick_model()
            if new and new != MODEL:
                print("Gemini model badla:", MODEL, "->", new)
                MODEL = new
                continue
            break
        if r.status_code in (429, 500, 503):
            time.sleep(25)
            continue
        break
    if r.status_code != 200:
        print("Gemini error", r.status_code, r.text[:300])
    if search and r.status_code != 200:
        return gemini(prompt, search=False, temp=temp)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    txt = "".join(p.get("text", "") for p in parts)
    return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))


# ================= Feature 3: trending topics =================
def trending():
    out = []
    d = datetime.date.today()
    try:
        r = requests.get("https://trends.google.com/trending/rss?geo=IN", headers=UA, timeout=30)
        out += ["Google Trend India: " + t.text for t in ET.fromstring(r.content).iter("title")][1:11]
    except Exception as ex:
        print("trends err", ex)
    try:
        r = requests.get(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{d:%m}/{d:%d}", headers=UA, timeout=30)
        ev = r.json().get("events", [])
        random.shuffle(ev)
        out += ["On this day: " + e["text"] for e in ev[:6]]
    except Exception as ex:
        print("wiki otd err", ex)
    try:
        y = d - datetime.timedelta(days=2)
        r = requests.get(f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{y:%Y}/{y:%m}/{y:%d}", headers=UA, timeout=30)
        arts = r.json().get("mostread", {}).get("articles", [])
        out += ["Wikipedia popular: " + a["title"].replace("_", " ") for a in arts
                if ":" not in a["title"] and a["title"] != "Main_Page"][:8]
    except Exception as ex:
        print("wiki popular err", ex)
    try:
        r = requests.get("https://www.reddit.com/r/todayilearned/top.json?t=day&limit=10", headers=UA, timeout=30)
        out += ["Reddit TIL: " + c["data"]["title"] for c in r.json()["data"]["children"][:6]]
    except Exception as ex:
        print("reddit err", ex)
    return out


# ================= Feature 5 + 6: analytics + title-style bandit =================
def get_creds():
    return Credentials(None, refresh_token=E["YT_REFRESH_TOKEN"], client_id=E["YT_CLIENT_ID"],
                       client_secret=E["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token",
                       scopes=["https://www.googleapis.com/auth/youtube",
                               "https://www.googleapis.com/auth/yt-analytics.readonly"])


def refresh_stats(yt, hist):
    ids = [h["id"] for h in hist if h.get("id")][-50:]
    if not ids:
        return
    try:
        r = yt.videos().list(part="statistics", id=",".join(ids)).execute()
        m = {i["id"]: i["statistics"] for i in r.get("items", [])}
        for h in hist:
            s = m.get(h.get("id"))
            if s:
                h["views"] = int(s.get("viewCount", 0))
                h["likes"] = int(s.get("likeCount", 0))
    except Exception as ex:
        print("stats error (token me 'youtube' scope hai?)", ex)


def refresh_retention(creds, hist):
    """YouTube Analytics API se average view % (retention) nikalta hai."""
    ids = [h["id"] for h in hist if h.get("id")][-50:]
    if not ids:
        return
    try:
        ya = build("youtubeAnalytics", "v2", credentials=creds)
        today = datetime.date.today()
        r = ya.reports().query(ids="channel==MINE", startDate=(today - datetime.timedelta(days=90)).isoformat(),
                               endDate=today.isoformat(), metrics="views,averageViewPercentage",
                               dimensions="video", filters="video==" + ",".join(ids),
                               sort="-views", maxResults=50).execute()
        m = {row[0]: row for row in r.get("rows", [])}
        for h in hist:
            row = m.get(h.get("id"))
            if row:
                h["avg_view_pct"] = round(float(row[2]), 1)
    except Exception as ex:
        print("retention error (Analytics API enable + scope check karo):", ex)


def score(h):
    base = h.get("views", 0) + 20 * h.get("likes", 0)
    pct = h.get("avg_view_pct")
    return base * (0.5 + min(pct, 150) / 100) if pct is not None else base


def avg_by(done, key):
    d = {}
    for h in done:
        d.setdefault(h.get(key) or "?", []).append(score(h))
    return {k: round(sum(v) / len(v)) for k, v in d.items()}


def pick_from(done, key, options):
    sc = {k: v for k, v in avg_by(done, key).items() if k in options}
    unseen = [o for o in options if o not in sc]
    if unseen or random.random() < 0.3:
        return random.choice(unseen or options)
    return max(sc, key=sc.get)


def best_of(done, key):
    d = {k: v for k, v in avg_by(done, key).items() if k != "?"}
    return max(d, key=d.get) if d else "-"


# ---------- best posting time ----------
def slot_order(hist, done):
    opts = [str(x) for x in SLOTS]
    sc = {k: v for k, v in avg_by(done, "slot").items() if k in opts}
    used = {str(h.get("slot")) for h in hist if h.get("slot")}
    unseen = [o for o in opts if o not in used]
    random.shuffle(unseen)
    ranked = sorted(sc, key=lambda o: -sc[o])
    rest = [o for o in opts if o in used and o not in sc]
    order = unseen + ranked + rest
    if random.random() < 0.3:
        random.shuffle(order)
    return order


def choose_publish_time(order, taken):
    now = datetime.datetime.now(IST)
    for o in order:
        t = now.replace(hour=int(o), minute=0, second=0, microsecond=0)
        if t < now + datetime.timedelta(minutes=45):
            t += datetime.timedelta(days=1)
        if all(abs(t.timestamp() - x) >= 2 * 3600 for x in taken):
            return t.timestamp(), o
    return (now + datetime.timedelta(minutes=45)).timestamp(), "now"


# ---------- remake of a winning video ----------
def find_remake(hist, done):
    if E.get("REMAKE", "1") != "1" or len(done) < 5:
        return None
    now = time.time()
    if any(h.get("remake_of") and now - h.get("created", h.get("ts", 0)) < 7 * 86400 for h in hist):
        return None
    days = int(E.get("REMAKE_AFTER_DAYS", "14"))
    med = sorted(score(h) for h in done)[len(done) // 2]
    cands = [h for h in done if now - h.get("ts", now) > days * 86400 and not h.get("remade")
             and h.get("script") and score(h) >= max(1.5 * med, 1)]
    return max(cands, key=score) if cands else None


# ---------- weekly report ----------
def weekly_report(yt, hist, state, done):
    today = datetime.datetime.now(IST)
    key = today.strftime("%Y-%m-%d")
    if today.weekday() != 6 or state.get("last_report") == key:
        return
    now = time.time()
    week = [h for h in hist if h.get("id") and 0 < now - h.get("ts", 0) <= 7 * 86400]
    subs = None
    try:
        subs = int(yt.channels().list(part="statistics", mine=True).execute()["items"][0]["statistics"]["subscriberCount"])
    except Exception as ex:
        print("subs error:", ex)
    top = sorted(week, key=score, reverse=True)[:3]
    lines = [f"📊 Weekly Report ({key})", f"Videos (7 din): {len(week)}",
             f"Inke total views: {sum(h.get('views', 0) for h in week)}"]
    if subs is not None:
        prev = state.get("subs")
        lines.append(f"Subscribers: {subs}" + (f" ({subs - prev:+d} pichhle hafte se)" if prev is not None else ""))
    if top:
        lines.append("Top videos:")
        for i, h in enumerate(top, 1):
            lines.append(f"{i}. {h.get('title')} | {h.get('views', 0)} views | retention {h.get('avg_view_pct', '?')}%")
    lines.append(f"Best title style: {best_of(done, 'style')} | hook style: {best_of(done, 'hook_style')} | "
                 f"time: {best_of(done, 'slot')}:00 IST | category: {best_of(done, 'category')}")
    try:
        summary = {"week_videos": [(h.get("title"), h.get("views", 0), h.get("avg_view_pct")) for h in week],
                   "best_style": best_of(done, "style"), "best_hook": best_of(done, "hook_style"),
                   "best_slot": best_of(done, "slot"), "best_category": best_of(done, "category")}
        tips = gemini('Is weekly YouTube Shorts (Hindi facts channel) report ke basis par agle hafte ke liye 3 chhote practical tips '
                      'Hinglish (Roman Hindi) mein do. Return ONLY JSON {"tips": "..."} . Data: '
                      + json.dumps(summary, ensure_ascii=False), temp=0.5)["tips"]
        lines.append("💡 Tips: " + str(tips))
    except Exception as ex:
        print("tips error:", ex)
    msg = "\n".join(lines)
    print(msg)
    notify(msg)
    state["last_report"] = key
    if subs is not None:
        state["subs"] = subs


def pick_style(done):
    return pick_from(done, "style", STYLES)


def feedback_text(done):
    if len(done) < 3:
        return ""
    top = sorted(done, key=score, reverse=True)
    f = lambda h: (h.get("title"), h.get("views"), "retention %s%%" % h.get("avg_view_pct", "?"))
    return ("PERFORMANCE DATA of our past videos (score combines views, likes and retention) -> best: %s ; worst: %s ; "
            "avg score per category: %s. Make more content like the winners (topic type, hook style, length) and avoid "
            "what the losers did." % ([f(h) for h in top[:4]], [f(h) for h in top[-3:]], avg_by(done, "category")))


# ================= script + Feature 9: fact check =================
def gen_script(avoid, feedback, trend, remake=None):
    if remake:
        task = ("Create a COMPLETELY NEW version of an earlier successful Short about this already verified fact. "
                f"Topic: {remake.get('topic')}. Old script (do NOT reuse its wording): "
                f"{json.dumps(remake.get('script'), ensure_ascii=False)}. Keep the same TRUE core fact but use a different hook, "
                "different angle and structure, different examples and fresh wording.")
        extra = ""
    else:
        task = "Pick ONE surprising, TRUE, verifiable fact that people would share with friends."
        extra = (f"Never repeat or closely resemble these earlier topics: {avoid}\n"
                 f"Trending ideas (use only if one can become a TRUE, interesting fact, otherwise ignore): {trend}")
    schema = """Return ONLY JSON with keys:
"topic": 3-6 word English topic
"category": one of space/science/body/animals/history/india/psychology/tech/other
"hooks": object with keys "question","number","shock": three different Hindi (Devanagari) on-screen opening texts for the SAME fact, max 6 words each ("question" = curious question, "number" = contains a striking number/statistic, "shock" = surprising claim)
"titles": object with keys "question","number","shock": three different Hindi titles for the SAME fact, max 60 chars each, no hashtags, main search keyword within the first 40 chars, curiosity-driven but truthful (no fake clickbait)
"script": list of 6-8 short Hindi sentences (Devanagari), TOTAL 60-80 words (video must be 25-30 seconds). Sentence 1 = strong curiosity hook. No filler. The LAST sentence must be a teaser/cliffhanger that flows naturally back into sentence 1 so the video loops seamlessly when replayed. Do NOT say subscribe/follow in the script.
"image_prompts": list with exactly as many items as script; each an English visual scene description for that sentence (no text, no logos, no real celebrities)
"keywords": 4 English stock-video search terms
"comment": one engaging Hindi question for viewers (max 100 chars) to post as the first comment, related to the fact
"description": Hindi description, 2-3 lines; the FIRST sentence must contain the main keyword phrase; end with a line asking viewers to subscribe for daily facts
"hinglish_keywords": list of 4 short Roman-Hindi (Hinglish) search phrases people would type on YouTube for this fact (e.g. "honey kabhi kharab nahi hota")
"hashtags": list of exactly 3 topic-specific hashtags for this fact (mix of English and Hindi, no spaces, each starting with #)
"tags": 15 tags: mix of Hindi, English and Hinglish search phrases, most specific first"""
    prompt = f"You run a faceless Hindi YouTube Shorts channel. Niche: {NICHE}.\n{task}\n{extra}\n{feedback}\n{schema}"
    return gemini(prompt)


def fact_check(d):
    p = f"""You are a strict fact-checker. Verify EVERY claim in this Hindi Shorts script (use web search if available).
Topic: {d.get('topic')}
Script: {json.dumps(d['script'], ensure_ascii=False)}
Return ONLY JSON: {{"verdict":"ok" or "fix" or "reject","issues":"short English note","script":[corrected Hindi sentences, same count, only when verdict is fix]}}
"ok" only if every claim is accurate. "fix" if small errors can be corrected. "reject" if the main fact is false, unverifiable or misleading."""
    return gemini(p, search=True, temp=0.2)


# ================= voice =================
async def tts(text, out, voice):
    await edge_tts.Communicate(text, voice, rate="+5%").save(out)


# ================= Feature 2: AI images + Ken Burns =================
def make_image(prompt, out, seed):
    full = (prompt[:280] + ", cinematic lighting, vertical composition, highly detailed, no text, no watermark")
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full) +
           f"?width=1080&height=1920&nologo=true&seed={seed}")
    for _ in range(3):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") and len(r.content) > 20000:
                open(out, "wb").write(r.content)
                return True
        except Exception as ex:
            print("image err", ex)
        time.sleep(8)
    return False


def fetch_clips(keywords):
    """Stock videos from Pixabay (free API key). Pixabay me portrait filter nahi hai, isliye center-crop hota hai."""
    key = E.get("PIXABAY_API_KEY")
    clips = []
    if not key:
        return clips
    for kw in keywords:
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": key, "q": kw, "per_page": 10, "safesearch": "true", "min_width": 1280},
                             timeout=60)
            hits = [h for h in r.json().get("hits", []) if h.get("duration", 0) >= 4]
            random.shuffle(hits)
            for v in hits[:2]:
                vf = v.get("videos", {})
                f = next((vf[k] for k in ("large", "medium") if vf.get(k, {}).get("url")), None)
                if not f:
                    continue
                path = f"{W}/clip{len(clips)}.mp4"
                with requests.get(f["url"], stream=True, timeout=180) as dl:
                    dl.raise_for_status()
                    with open(path, "wb") as fh:
                        for ch in dl.iter_content(1 << 20):
                            fh.write(ch)
                clips.append(path)
        except Exception as ex:
            print("pixabay error", kw, ex)
    return clips


def openverse_download(q, out, min_ms, max_ms, cats):
    """Openverse se CC0 audio download (no attribution needed)."""
    for cat in cats:
        try:
            p = {"q": q, "license": "cc0", "page_size": 20}
            if cat:
                p["category"] = cat
            r = requests.get("https://api.openverse.org/v1/audio/", params=p, headers=UA, timeout=45)
            if r.status_code != 200:
                continue
            res = []
            for x in r.json().get("results", []):
                d = x.get("duration")
                if x.get("url") and (d is None or min_ms <= d <= max_ms):
                    res.append(x)
            random.shuffle(res)
            for x in res[:4]:
                try:
                    a = requests.get(x["url"], headers=UA, timeout=90)
                    if a.status_code != 200 or not (5000 < len(a.content) < 25_000_000):
                        continue
                    open(out, "wb").write(a.content)
                    dur(out)  # valid audio hai ya nahi
                    print("Audio:", q, "|", x.get("title"), "|", x.get("license"))
                    return out
                except Exception as ex:
                    print("audio download err", ex)
        except Exception as ex:
            print("openverse err", ex)
    return None


def fetch_music():
    """1) music/ folder me file ho to wo, 2) warna Openverse se CC0 background music."""
    local = glob.glob("music/*.mp3")
    if local:
        return random.choice(local)
    queries = ["ambient instrumental", "cinematic background", "calm piano", "lofi chill", "soft electronic"]
    random.shuffle(queries)
    for q in queries[:3]:
        p = openverse_download(q, f"{W}/music_raw", 30000, 10 ** 9, ("music", None))
        if p:
            return p
    print("Music nahi mili, bina music ke bana raha hu")
    return None


def fetch_sfx():
    """Chhota whoosh sound (Openverse CC0). Nahi mila to None."""
    for q in ("whoosh", "swoosh transition"):
        p = openverse_download(q, f"{W}/sfx_raw", 100, 5000, ("sound_effect", None))
        if p:
            try:
                run(["ffmpeg", "-y", "-i", p, "-t", "1.2", "-af", "afade=t=out:st=0.9:d=0.3", "-ar", "44100", "-ac", "2",
                     f"{W}/sfx.wav"])
                return f"{W}/sfx.wav"
            except Exception as ex:
                print("sfx convert err", ex)
    print("SFX nahi mila, bina SFX ke")
    return None


ENC = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", "30"]


def make_seg(i, d, clip, img):
    out = f"{W}/seg{i}.mp4"
    if img:
        z = "min(zoom+0.0012,1.3)" if i % 2 == 0 else "if(eq(on,0),1.3,max(zoom-0.0012,1.0))"
        vf = ("scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={int(d * 30) + 2}:s=1080x1920:fps=30,setsar=1")
        run(["ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{d:.2f}", *ENC, out])
    elif clip:
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", clip, "-t", f"{d:.2f}", "-vf", vf, *ENC, out])
    else:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x14213d:s=1080x1920:r=30", "-t", f"{d:.2f}", *ENC, out])
    return out


# ================= Feature 1 + 4: animated word subtitles + hook text =================
def ass_t(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def make_ass(sentences, durs, hook):
    HL, NORM = "{\\c&H0000E5FF&\\fscx118\\fscy118}", "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Sub,Noto Sans Devanagari,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,560,1
Style: Hook,Noto Sans Devanagari,96,&H0000E5FF,&H0000E5FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,3,8,60,60,260,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    if hook:
        lines.append(f"Dialogue: 1,{ass_t(0)},{ass_t(2.6)},Hook,,0,0,0,,{hook}")
    t0 = 0.0
    for s, d in zip(sentences, durs):
        words = s.split()
        if words:
            wt = [len(w) + 1 for w in words]
            tot = sum(wt)
            starts, acc = [], t0
            for w in wt:
                starts.append(acc)
                acc += d * w / tot
            ends = starts[1:] + [t0 + d]
            for j in range(len(words)):
                c0 = (j // 3) * 3
                chunk = words[c0:c0 + 3]
                txt = " ".join((HL + w + NORM) if c0 + k == j else w for k, w in enumerate(chunk))
                lines.append(f"Dialogue: 0,{ass_t(starts[j])},{ass_t(ends[j])},Sub,,0,0,0,,{txt}")
        t0 += d
    open(f"{W}/subs.ass", "w", encoding="utf-8").write(head + "\n".join(lines) + "\n")


# ================= build video =================
def build_video(sentences, img_prompts, keywords, hook, voice):
    durs, auds = [], []
    for i, s in enumerate(sentences):
        raw, a = f"{W}/s{i}.mp3", f"{W}/s{i}.m4a"
        asyncio.run(tts(s, raw, voice))
        run(["ffmpeg", "-y", "-i", raw, "-af", "apad=pad_dur=0.15", "-ar", "44100", "-ac", "2", "-c:a", "aac", a])
        auds.append(a)
        durs.append(dur(a))

    imgs, failed = {}, False
    if VISUALS in ("ai", "mix"):
        for i in range(len(sentences)):
            if VISUALS == "mix" and i % 2 == 1:
                continue
            p = f"{W}/img{i}.jpg"
            if make_image(img_prompts[i % len(img_prompts)], p, random.randint(1, 99999)):
                imgs[i] = p
            else:
                failed = True
            time.sleep(3)
    clips = fetch_clips(keywords) if (VISUALS != "ai" or failed or not imgs) else []

    segs, ci = [], 0
    for i, d in enumerate(durs):
        clip = None
        if i not in imgs and clips:
            clip = clips[ci % len(clips)]
            ci += 1
        segs.append(make_seg(i, d, clip, imgs.get(i)))

    make_ass(sentences, durs, textwrap.fill(hook, 14).replace("\n", "\\N") if hook else "")
    open(f"{W}/vlist.txt", "w").write("".join(f"file '{os.path.abspath(p)}'\n" for p in segs))
    open(f"{W}/alist.txt", "w").write("".join(f"file '{os.path.abspath(p)}'\n" for p in auds))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{W}/vlist.txt", "-c", "copy", f"{W}/video.mp4"])
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{W}/alist.txt", "-c:a", "aac", "-b:a", "192k", f"{W}/voice.m4a"])

    music = fetch_music()
    sfx = fetch_sfx() if SFX_ON else None
    cmd = ["ffmpeg", "-y", "-i", f"{W}/video.mp4", "-i", f"{W}/voice.m4a"]
    fc, mix, idx = [f"[0:v]subtitles={W}/subs.ass[v]"], ["[1:a]"], 2
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fc.append(f"[{idx}:a]volume=0.10[m]")
        mix.append("[m]")
        idx += 1
    if sfx:
        cmd += ["-i", sfx]
        times, acc = [0.0], 0.0
        for d in durs[:-1]:
            acc += d
            times.append(acc)
        n = len(times)
        if n == 1:
            fc.append(f"[{idx}:a]volume={SFX_VOL}[x0]")
        else:
            fc.append(f"[{idx}:a]volume={SFX_VOL},asplit={n}" + "".join(f"[x{k}]" for k in range(n)))
        for k, t in enumerate(times):
            ms = int(t * 1000)
            fc.append(f"[x{k}]adelay={ms}|{ms}[y{k}]")
            mix.append(f"[y{k}]")
    if len(mix) == 1:
        fc.append("[1:a]anull[a]")
    else:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", f"{W}/final.mp4"]
    run(cmd)

    # thumbnail = frame with hook text visible
    run(["ffmpeg", "-y", "-ss", "1.2", "-i", f"{W}/final.mp4", "-frames:v", "1", "-vf", "scale=720:1280",
         "-q:v", "3", f"{W}/thumb.jpg"])
    return f"{W}/final.mp4", f"{W}/thumb.jpg"


# ================= SEO helpers =================
def build_desc(data):
    tags = []
    for h in (data.get("hashtags") or [])[:3]:
        h = re.sub(r"[\s#]+", "", str(h))
        if h:
            tags.append("#" + h)
    keys = " | ".join(str(k) for k in (data.get("hinglish_keywords") or [])[:4])
    parts = [data["description"]]
    if keys:
        parts.append("🔎 " + keys)
    parts.append(" ".join(tags + ["#Shorts", "#HindiFacts"]))
    return "\n\n".join(parts)


def build_tags(data):
    out, total = [], 0
    for t in list(data.get("tags") or []) + list(data.get("hinglish_keywords") or []):
        t = str(t).replace("#", "").replace(",", " ").strip()
        if t and t.lower() not in [x.lower() for x in out] and total + len(t) + 1 <= 450:
            out.append(t)
            total += len(t) + 1
    return out[:25]


# ================= upload =================
def upload(yt, path, thumb, title, data, publish_at=None):
    desc = build_desc(data)
    status = {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    if publish_at:
        t = datetime.datetime.fromtimestamp(publish_at, datetime.timezone.utc)
        status = {"privacyStatus": "private", "publishAt": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  "selfDeclaredMadeForKids": False}
    body = {"snippet": {"title": title[:95], "description": desc, "tags": build_tags(data),
                        "categoryId": "27", "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
            "status": status}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(path, chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
    except Exception as ex:
        print("thumbnail skipped:", ex)
    return vid


# ================= first comment =================
def post_comment(yt, vid, text, tries=3):
    for k in range(tries):
        try:
            yt.commentThreads().insert(part="snippet", body={"snippet": {"videoId": vid, "topLevelComment": {
                "snippet": {"textOriginal": text}}}}).execute()
            return True
        except Exception as ex:
            print("comment error:", ex)
            if k < tries - 1:
                time.sleep(20)
    return False


def post_pending_comments(yt, hist):
    """Scheduled videos ka comment tab post hota hai jab wo public ho chuki ho."""
    now = time.time()
    for h in hist:
        if h.get("id") and h.get("comment") and not h.get("commented") and h.get("c_try", 0) < 3 \
                and 0 < h.get("ts", 0) <= now - 300:
            h["c_try"] = h.get("c_try", 0) + 1
            h["commented"] = post_comment(yt, h["id"], h["comment"], tries=1)


# ================= playlist (series) =================
def add_to_playlist(yt, state, vid, category):
    name = f"{SERIES_NAME} | {CAT_HI.get(category, CAT_HI['other'])}"
    try:
        pls = state.setdefault("playlists", {})
        pid = pls.get(category)
        if not pid:
            r = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute()
            pid = next((p["id"] for p in r.get("items", []) if p["snippet"]["title"] == name), None)
        if not pid:
            pid = yt.playlists().insert(part="snippet,status", body={
                "snippet": {"title": name, "description": f"{SERIES_NAME}: roz naye facts", "defaultLanguage": "hi"},
                "status": {"privacyStatus": "public"}}).execute()["id"]
        pls[category] = pid
        yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()
    except Exception as ex:
        print("playlist error:", ex)


# ================= main =================
def main():
    hist = load_history()
    state = load_state()
    creds = get_creds()
    yt = build("youtube", "v3", credentials=creds)
    refresh_stats(yt, hist)
    refresh_retention(creds, hist)
    post_pending_comments(yt, hist)
    save_history(hist)
    save_state(state)
    done = [h for h in hist if h.get("id") and h.get("views") is not None and time.time() - h.get("ts", 0) > 86400]
    weekly_report(yt, hist, state, done)
    save_state(state)

    style = pick_from(done, "style", STYLES)
    hook_style = pick_from(done, "hook_style", STYLES)
    feedback = feedback_text(done)
    remake = find_remake(hist, done)
    trend = [] if remake else trending()
    print("Trending ideas:", len(trend), "| title style:", style, "| hook style:", hook_style,
          "| remake:", remake.get("title") if remake else None)

    avoid = [h.get("topic") or h.get("title") for h in hist][-80:]
    data, used_remake = None, None
    for attempt in range(3):
        src = remake if attempt == 0 else None
        cand = gen_script(avoid, feedback, trend, src)
        fc = fact_check(cand)
        v = fc.get("verdict")
        print("Fact-check:", cand.get("topic"), v, fc.get("issues"))
        if v == "ok" or (v == "fix" and fc.get("script")):
            if v == "fix":
                cand["script"] = fc["script"]
            data, used_remake = cand, src
            break
        avoid.append(cand.get("topic", ""))
        hist.append({"topic": cand.get("topic"), "title": "REJECTED by fact-check", "status": "rejected"})
    if not data:
        save_history(hist)
        notify("⚠️ 3 baar fact-check fail hua, aaj upload skip kiya.")
        raise SystemExit("fact-check failed")

    voice = VOICE
    if used_remake:
        alt = [x for x in VOICES if x != used_remake.get("voice", VOICE)]
        voice = alt[0] if alt else VOICE

    n = sum(1 for h in hist if h.get("id")) + 1
    cat = data.get("category") if data.get("category") in CAT_HI else "other"
    titles = data.get("titles") or {}
    base = titles.get(style) or next(iter(titles.values()), data.get("topic", "Rochak Tathya"))
    title = base[:80]
    if E.get("SERIES_IN_TITLE", "0") == "1":
        title = f"{base[:66]} | {SERIES_TAG} {n}"
    hooks = data.get("hooks") or {}
    hook = hooks.get(hook_style) or next(iter(hooks.values()), data.get("hook", ""))
    data["description"] += f"\n\nSeries: {SERIES_NAME} | {SERIES_TAG} {n}"
    print("Title:", title, "| hook:", hook, "| voice:", voice)

    video, thumb = build_video(data["script"], data.get("image_prompts") or [data.get("topic", "abstract")],
                               data.get("keywords", []), hook, voice)

    publish_at, slot = None, None
    if SMART_TIME:
        taken = [h["ts"] for h in hist if h.get("id") and h.get("ts", 0) > time.time()]
        publish_at, slot = choose_publish_time(slot_order(hist, done), taken)
    elif DELAY > 0:
        publish_at = time.time() + DELAY * 3600

    if not ask_approval(video, title):
        hist.append({"topic": data.get("topic"), "title": title, "status": "not-approved"})
        save_history(hist)
        notify("⏭️ Upload skip (approve nahi hua).")
        return

    vid = upload(yt, video, thumb, title, data, publish_at)
    link = "https://youtube.com/shorts/" + vid
    print("Uploaded:", link)
    rec = {"id": vid, "title": title, "topic": data.get("topic"), "category": cat, "style": style,
           "hook_style": hook_style, "slot": slot, "voice": voice, "script": data["script"],
           "ts": publish_at or time.time(), "created": time.time(), "comment": data.get("comment"), "commented": False}
    if used_remake:
        rec["remake_of"] = used_remake.get("id")
        used_remake["remade"] = True
    hist.append(rec)
    save_history(hist)
    if not publish_at and rec["comment"]:
        rec["commented"] = post_comment(yt, vid, rec["comment"])
        save_history(hist)
    add_to_playlist(yt, state, vid, cat)
    save_state(state)

    when = (datetime.datetime.fromtimestamp(publish_at, IST).strftime("%d %b, %H:%M IST") if publish_at else "abhi")
    notify(f"✅ Upload ho gaya! (Publish: {when})\n{title}\nTitle style: {style} | Hook: {hook_style} | Slot: {slot}"
           f"{' | 🔁 remake' if used_remake else ''}\n{link}"
           + ("\n💬 First comment post ho gaya, Studio mein Pin kar do." if rec["commented"]
              else ("\n💬 Comment video public hone ke baad post hoga (Pin khud karna)." if publish_at else "")))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        notify("❌ Shorts bot fail hua:\n" + traceback.format_exc()[-900:])
        raise
