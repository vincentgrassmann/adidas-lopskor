#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adidas_lopskor.py
Hämtar alla herrlöparskor från https://www.adidas.se/herr-lopning-skor, sparar
huvudbilden för varje sko och bygger en bildsida (adidas_lopskor/index.html)
där skor som dykt upp sedan förra körningen visas överst.

Körning:
  python adidas_lopskor.py              hämta + bygg sidan
  python adidas_lopskor.py --open       ...och öppna sidan i webbläsaren
  python adidas_lopskor.py --install    kör automatiskt varje dag (standard 07:00, macOS/Windows)
  python adidas_lopskor.py --install --time 06:30
  python adidas_lopskor.py --uninstall  ta bort schemat
  python adidas_lopskor.py --push       hämta + bygg + skicka till GitHub (git commit/push)
  python adidas_lopskor.py --install --push   daglig körning som även skickar till GitHub

GitHub: adidas.se blockerar GitHubs servrar (HTTP 403). Därför hämtar din egen dator
        (Mac Mini) skorna och skickar resultatet till repot med --push.
        .github/workflows/adidas-lopskor.yml publicerar sedan sidan med GitHub
        Pages (https://<användarnamn>.github.io/<repo>/) som du öppnar i mobilen.

Krav:   pip install requests
Valfritt (används bara om adidas blockerar vanliga anrop):
        pip install playwright && python -m playwright install chromium
"""
import argparse
import datetime as dt
import html
import json
import os
import platform
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# ------------------------------------------------------------------ inställningar
BASE = "https://www.adidas.se"
LIST_PATH = "/herr-lopning-skor"
PAGE_SIZE = 48
MAX_PAGES = 20
IMG_URL = "https://assets.adidas.com/images/w_600,h_600,f_auto,q_auto,fl_lossy,c_fill,g_auto/"
NEW_DAYS = 30                      # hur länge en sko räknas som "ny" i listan
HERE = Path(__file__).resolve().parent
OUT = HERE / "adidas_lopskor"
DATA = OUT / "data.json"
PAGE = OUT / "index.html"
LOG = OUT / "log.txt"
TASK_LABEL = "se.vincent.adidas-lopskor"
WIN_TASK = "Adidas lopskor"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}

SKU = r"[A-Z0-9]{6}"
HREF_RE = re.compile(r"/([a-z0-9][a-z0-9-]*)/(" + SKU + r")\.html")
IMG_RE = re.compile(
    r"assets\.adidas\.com/images/(?:[^/\"'\s<>]+/)*?"
    r"([0-9a-f]{32}_\d+/([^/\"'\s<>?]+?)\.(?:jpg|jpeg|png|webp))", re.I)
PAGES_RE = re.compile(r"Sida\W{0,20}\d+\W{0,20}av\W{0,20}(\d+)", re.I)

WORD_FIX = {
    "Loparskor": "Löparskor", "LOPARSKOR": "LÖPARSKOR", "loparskor": "löparskor",
    "Terrangloparskor": "Terränglöparskor", "TERRANGLOPARSKOR": "TERRÄNGLÖPARSKOR",
    "Lopning": "Löpning", "Gra": "Grå", "Bla": "Blå", "Rod": "Röd", "Gron": "Grön",
}

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class Blocked(Exception):
    pass


# ------------------------------------------------------------------ logg
def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ hämtning
class RequestsFetcher:
    name = "requests"

    def __init__(self):
        import requests
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def get(self, url):
        last = None
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 200 and len(r.text) > 5000:
                    return r.text
                last = f"HTTP {r.status_code}, {len(r.text)} tecken"
            except Exception as e:
                last = str(e)
            time.sleep(3 * (attempt + 1))
        raise Blocked(last)

    def close(self):
        self.s.close()


class PlaywrightFetcher:
    def __init__(self, headless=True):
        from playwright.sync_api import sync_playwright
        self.name = "playwright (" + ("headless" if headless else "synligt fönster") + ")"
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=headless)
        self.ctx = self.browser.new_context(locale="sv-SE", user_agent=HEADERS["User-Agent"],
                                            viewport={"width": 1400, "height": 1000})
        self.page = self.ctx.new_page()

    def get(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(2500)
        for _ in range(8):                       # scrolla så att alla kort renderas
            self.page.mouse.wheel(0, 2500)
            self.page.wait_for_timeout(400)
        return self.page.content()

    def close(self):
        for fn in (self.ctx.close, self.browser.close, self.pw.stop):
            try:
                fn()
            except Exception:
                pass


# ------------------------------------------------------------------ tolkning
def has_letter_and_digit(s):
    return bool(re.search(r"\d", s)) and bool(re.search(r"[A-Z]", s))


def sku_from_filename(fname, known):
    cands = [c for c in re.findall(r"(?<=_)(" + SKU + r")(?=_|$)", fname) if has_letter_and_digit(c)]
    for c in reversed(cands):
        if c in known:
            return c
    return cands[-1] if cands else None


def fix_words(text):
    return " ".join(WORD_FIX.get(w, w) for w in text.split())


def name_from_filename(fname, sku):
    idx = fname.rfind("_" + sku)
    prefix = fname[:idx] if idx > 0 else fname
    tokens = [t for t in prefix.split("_") if t]
    if len(tokens) > 1:
        return fix_words(" ".join(tokens[:-1])), fix_words(tokens[-1])
    return fix_words(" ".join(tokens)), ""


def parse(page_html):
    h = page_html.replace("\\u002F", "/").replace("\\/", "/")
    order, slugs = [], {}
    for m in HREF_RE.finditer(h):
        slug, sku = m.group(1), m.group(2)
        if has_letter_and_digit(sku) and sku not in slugs:
            slugs[sku] = slug
            order.append(sku)

    images = {}
    for m in IMG_RE.finditer(h):
        path, fname = m.group(1), m.group(2)
        sku = sku_from_filename(fname, slugs)
        if sku and sku not in images:          # första bilden i sidans ordning = huvudbilden
            images[sku] = (path, fname)

    alts = {}
    for t in re.finditer(r"<img\b[^>]*>", h, re.I):
        tag = t.group(0)
        a = re.search(r'\balt="([^"]{3,200})"', tag)
        if not a:
            continue
        for sku in re.findall(r"_(" + SKU + r")_", tag):
            if sku in slugs and sku not in alts:
                alts[sku] = html.unescape(a.group(1)).strip()

    out = {}
    for sku in order:
        if sku not in images:
            continue
        path, fname = images[sku]
        guess, color = name_from_filename(fname, sku)
        out[sku] = {
            "sku": sku,
            "name": alts.get(sku) or guess,
            "color": color,
            "url": f"{BASE}/{slugs[sku]}/{sku}.html",
            "image": path,
        }
    total_pages = None
    m = PAGES_RE.search(re.sub(r"<[^>]+>", " ", h))
    if m:
        total_pages = int(m.group(1))
    return out, total_pages


def crawl(fetcher):
    products, max_pages = {}, MAX_PAGES
    for i in range(MAX_PAGES):
        if i >= max_pages:
            break
        url = BASE + LIST_PATH + (f"?start={i * PAGE_SIZE}" if i else "")
        found, total_pages = parse(fetcher.get(url))
        if i == 0:
            if not found:
                raise Blocked("inga produkter hittades på första sidan")
            if total_pages:
                max_pages = min(MAX_PAGES, total_pages)
        new = [k for k in found if k not in products]
        for k in new:
            products[k] = dict(found[k], rank=len(products))
        log(f"  sida {i + 1}/{max_pages}: {len(found)} skor, {len(new)} ej sedda tidigare i körningen")
        if not new or len(found) < PAGE_SIZE - 8:
            break
        time.sleep(1.5)
    return products


def fetch_all():
    attempts = [("requests", lambda: RequestsFetcher()),
                ("playwright-headless", lambda: PlaywrightFetcher(headless=True)),
                ("playwright-synlig", lambda: PlaywrightFetcher(headless=False))]
    errors = []
    for label, make in attempts:
        fetcher = None
        try:
            fetcher = make()
            log(f"Hämtar med {fetcher.name} …")
            products = crawl(fetcher)
            if products:
                return products, errors
        except ImportError as e:
            errors.append(f"{label}: modul saknas ({e.name})")
        except Exception as e:
            errors.append(f"{label}: {e}")
            log(f"  misslyckades: {e}")
        finally:
            if fetcher:
                fetcher.close()
    return {}, errors


# ------------------------------------------------------------------ lagring
def load_state():
    if DATA.exists():
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Kunde inte läsa data.json ({e}) – sparar kopia och börjar om")
            DATA.rename(DATA.with_suffix(f".trasig-{int(time.time())}.json"))
    return {"items": {}, "runs": [], "last_run": None}


def save_state(state):
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA)


def update_state(state, products, now):
    today = now.strftime("%Y-%m-%d")
    items = state["items"]
    first_run = not items
    prev_active = sum(1 for it in items.values() if it.get("active"))
    new_skus = []
    for sku, p in products.items():
        it = items.get(sku)
        if it is None:
            it = dict(p, first_seen=today, baseline=first_run)
            items[sku] = it
            new_skus.append(sku)
        else:
            it.update(name=p["name"], color=p["color"], url=p["url"], image=p["image"])
        it["rank"] = p["rank"]
        it["last_seen"] = today
        it["active"] = True
    complete = prev_active == 0 or len(products) >= prev_active * 0.6
    if complete:
        for sku, it in items.items():
            if sku not in products:
                it["active"] = False
    else:
        log(f"Bara {len(products)} av tidigare {prev_active} skor hittades – borttagna markeras inte denna gång")
    state["last_run"] = now.strftime("%Y-%m-%d %H:%M")
    state["runs"] = (state.get("runs") or [])[-89:] + [
        {"time": state["last_run"], "found": len(products), "new": len(new_skus), "first_run": first_run}]
    return new_skus, first_run


# ------------------------------------------------------------------ sidan
PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0d0d0d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Löparskor">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' fill='%230d0d0d'/%3E%3Cpath d='M14 44l10-18 6 3-10 18zM28 44l12-24 6 3-12 24zM42 44l8-14 6 3-8 14z' fill='%23fff'/%3E%3C/svg%3E">
<title>Nya löparskor – adidas.se</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{color-scheme:light dark;--bg:light-dark(#fff,#0d0d0d);--ink:light-dark(#0d0d0d,#f2f2f2);
--mut:light-dark(#6b6b6b,#9a9a9a);--card:light-dark(#eceeef,#1b1b1b);--line:light-dark(#dedede,#2a2a2a);--acc:#0050ff}
*{box-sizing:border-box;min-width:0}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Archivo,system-ui,sans-serif}
header,section{padding:28px max(4vw,16px);max-width:1500px;margin:auto}
header{border-bottom:2px solid var(--ink);padding-bottom:18px}
h1{font-size:clamp(30px,5vw,52px);line-height:1.05;margin:0 0 8px;font-weight:800;letter-spacing:-.02em}
h2{font-size:24px;margin:0 0 14px;font-weight:800}
h3{font-size:16px;margin:22px 0 10px;color:var(--mut);font-weight:600}
.meta{color:var(--mut);font-size:15px}
.meta b{color:var(--ink)}
.note{background:var(--card);padding:14px 16px;border-left:4px solid var(--acc);margin:12px 0 0}
.err{border-left-color:#e0281c}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,190px),1fr));gap:14px}
.big .grid{grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr))}
a.card{display:block;color:inherit;text-decoration:none;background:var(--card);position:relative}
a.card:hover .ph img{transform:scale(1.04)}
a.card:focus-visible{outline:3px solid var(--acc);outline-offset:2px}
.ph{aspect-ratio:1;overflow:hidden;background:#eceeef}
.ph img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}
.tx{padding:10px 12px 12px}
.nm{font-weight:600;font-size:15px;line-height:1.3}
.sub{color:var(--mut);font-size:13px;margin-top:3px}
.ny{position:absolute;top:8px;left:8px;background:var(--acc);color:#fff;font-size:13px;font-weight:800;padding:3px 8px}
.empty{color:var(--mut);padding:18px 0}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
input{font:inherit;font-size:16px;padding:10px 12px;border:2px solid var(--ink);background:var(--bg);color:var(--ink);min-height:44px;width:min(100%,340px)}
input:focus{outline:3px solid var(--acc);outline-offset:1px}
details summary{cursor:pointer;font-size:24px;font-weight:800;margin-bottom:14px;min-height:44px}
@media(prefers-reduced-motion:reduce){.ph img{transition:none}}
</style>
</head>
<body>
<header>
  <h1>Nya löparskor</h1>
  <div class="meta" id="meta"></div>
  <div id="notes"></div>
</header>
<section class="big"><h2 id="todayH">Nytt sedan förra körningen</h2><div id="today"></div></section>
<section><h2>Nytt senaste __DAYS__ dagarna</h2><div id="recent"></div></section>
<section>
  <details id="allD"><summary id="allS">Hela sortimentet</summary>
    <div class="bar"><input id="q" type="search" placeholder="Sök modell, färg eller artikelnummer" aria-label="Sök"></div>
    <div id="all"></div>
  </details>
</section>
<script>
{
const S=__DATA__;
const IMG="__IMG__",MON=["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fd=d=>{const [y,m,dd]=d.split("-");return +dd+" "+MON[m-1]+" "+y};
const items=Object.values(S.items);
const lastDay=(S.last_run||"").slice(0,10);
const isNew=it=>!it.baseline;
const card=(it,badge)=>`<a class="card" href="${esc(it.url)}" target="_blank" rel="noopener">
 ${badge?`<span class="ny">${esc(badge)}</span>`:""}
 <div class="ph"><img loading="lazy" src="${IMG+esc(it.image)}" alt="${esc(it.name)}" onerror="this.style.opacity=0"></div>
 <div class="tx"><div class="nm">${esc(it.name)}</div><div class="sub">${esc([it.color,it.sku].filter(Boolean).join(" · "))}${it.active?"":" · ej kvar på sajten"}</div></div></a>`;
const grid=arr=>`<div class="grid">${arr.join("")}</div>`;
const active=items.filter(i=>i.active).sort((a,b)=>a.rank-b.rank);
const today=items.filter(i=>isNew(i)&&i.first_seen===lastDay).sort((a,b)=>a.rank-b.rank);
const cut=new Date(Date.now()-__DAYS__*864e5).toISOString().slice(0,10);
const recent=items.filter(i=>isNew(i)&&i.first_seen>=cut&&i.first_seen!==lastDay);
const run=(S.runs||[]).at(-1)||{};
document.getElementById("meta").innerHTML=
 `Herr · löpning · <a href="__LIST__" target="_blank" rel="noopener" style="color:inherit">adidas.se</a> — uppdaterad <b>${esc(S.last_run||"–")}</b> · <b>${active.length}</b> skor i sortimentet · <b>${today.length}</b> nya idag`;
const notes=[];
if(run.first_run)notes.push(`<div class="note">Första körningen: alla ${active.length} skor är sparade som utgångsläge. Från nästa körning visas nya skor överst.</div>`);
if(S.error)notes.push(`<div class="note err">Senaste hämtningen misslyckades (${esc(S.error_time)}): ${esc(S.error)}. Visar senast sparade data.</div>`);
document.getElementById("notes").innerHTML=notes.join("");
document.getElementById("today").innerHTML=today.length?grid(today.map(i=>card(i,"NY"))):`<div class="empty">Inga nya skor sedan förra körningen.</div>`;
const byDay={};recent.forEach(i=>(byDay[i.first_seen]??=[]).push(i));
const days=Object.keys(byDay).sort().reverse();
document.getElementById("recent").innerHTML=days.length?days.map(d=>`<h3>${fd(d)} · ${byDay[d].length} st</h3>`+grid(byDay[d].map(i=>card(i,"")))).join(""):`<div class="empty">Inga tidigare nyheter än.</div>`;
document.getElementById("allS").textContent=`Hela sortimentet (${active.length})`;
const all=document.getElementById("all"),q=document.getElementById("q");
const draw=()=>{const t=q.value.trim().toLowerCase();
 const list=active.filter(i=>!t||(i.name+" "+i.color+" "+i.sku).toLowerCase().includes(t));
 all.innerHTML=list.length?grid(list.map(i=>card(i,isNew(i)&&i.first_seen>=cut?"NY":""))):`<div class="empty">Inget matchar.</div>`};
q.addEventListener("input",draw);
document.getElementById("allD").addEventListener("toggle",draw,{once:true});
if(!today.length&&!days.length)document.getElementById("allD").open=true;
}
</script>
</body>
</html>
"""


def build_page(state):
    OUT.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE_TEMPLATE.replace("__DATA__", data)
            .replace("__IMG__", IMG_URL)
            .replace("__DAYS__", str(NEW_DAYS))
            .replace("__LIST__", BASE + LIST_PATH))
    PAGE.write_text(page, encoding="utf-8")


# ------------------------------------------------------------------ GitHub
def git_push():
    def g(*a):
        return subprocess.run(["git", *a], cwd=HERE, capture_output=True, text=True)

    if g("rev-parse", "--is-inside-work-tree").returncode != 0:
        log(f"--push: {HERE} är inte ett git-repo – klona repot och lägg skriptet där")
        return False
    ident = []
    if not g("config", "user.email").stdout.strip():
        ident = ["-c", "user.name=adidas-lopskor", "-c", "user.email=adidas-lopskor@localhost"]
    g("add", OUT.name)
    if g("diff", "--cached", "--quiet").returncode == 0:
        log("--push: inga ändringar att skicka")
        return True
    r = g(*ident, "commit", "-m", f"Löparskor {dt.datetime.now():%Y-%m-%d %H:%M}")
    if r.returncode != 0:
        log(f"--push: commit misslyckades: {(r.stderr or r.stdout).strip()}")
        return False
    r = g("pull", "--rebase", "--autostash")
    if r.returncode != 0:
        log(f"--push: pull misslyckades: {(r.stderr or r.stdout).strip()}")
        g("rebase", "--abort")
        return False
    r = g("push")
    if r.returncode != 0:
        log(f"--push: push misslyckades: {(r.stderr or r.stdout).strip()}")
        return False
    log("--push: skickat till GitHub")
    return True


# ------------------------------------------------------------------ schemaläggning
def xml_esc(s):
    return html.escape(str(s), quote=True)


def install(at, push=False):
    hh, mm = (int(x) for x in at.split(":"))
    script = Path(__file__).resolve()
    extra = ["--push"] if push else []
    OUT.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{TASK_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{TASK_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{xml_esc(sys.executable)}</string><string>{xml_esc(script)}</string>{"".join(f"<string>{e}</string>" for e in extra)}</array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>WorkingDirectory</key><string>{xml_esc(HERE)}</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hh}</integer><key>Minute</key><integer>{mm}</integer></dict>
  <key>StandardOutPath</key><string>{xml_esc(OUT / "launchd.log")}</string>
  <key>StandardErrorPath</key><string>{xml_esc(OUT / "launchd.log")}</string>
</dict>
</plist>
""", encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"launchctl load misslyckades: {r.stderr.strip()}")
        print(f"Klart. Körs varje dag {at} via launchd ({plist}).")
    elif system == "Windows":
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            exe = pyw
        q = lambda s: str(s).replace("'", "''")
        ps = (f"$a=New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(script)}\"{' --push' if push else ''}' "
              f"-WorkingDirectory '{q(HERE)}';"
              f"$t=New-ScheduledTaskTrigger -Daily -At '{hh:02d}:{mm:02d}';"
              f"$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries;"
              f"Register-ScheduledTask -TaskName '{WIN_TASK}' -Action $a -Trigger $t -Settings $s -Force | Out-Null")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"Kunde inte skapa schemalagd uppgift: {r.stderr.strip()}")
        print(f"Klart. Körs varje dag {at} via Task Scheduler (\"{WIN_TASK}\").")
    else:
        print("Lägg till detta i crontab -e:")
        print(f"{mm} {hh} * * * cd '{HERE}' && '{sys.executable}' '{script}'{' --push' if push else ''}")


def uninstall():
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{TASK_LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        if plist.exists():
            plist.unlink()
        print("Schemat borttaget.")
    elif system == "Windows":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{WIN_TASK}' -Confirm:$false"])
        print("Schemat borttaget.")
    else:
        print("Ta bort raden manuellt med crontab -e.")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Nya adidas-löparskor (herr) från adidas.se")
    ap.add_argument("--open", action="store_true", help="öppna sidan i webbläsaren efteråt")
    ap.add_argument("--install", action="store_true", help="schemalägg daglig körning")
    ap.add_argument("--uninstall", action="store_true", help="ta bort schemat")
    ap.add_argument("--time", default="07:00", help="klockslag för --install (HH:MM)")
    ap.add_argument("--push", action="store_true", help="skicka resultatet till GitHub (git commit + push)")
    args = ap.parse_args()

    if args.install:
        if not re.fullmatch(r"\d{1,2}:\d{2}", args.time):
            sys.exit("--time ska vara HH:MM")
        install(args.time, args.push)
        return
    if args.uninstall:
        uninstall()
        return

    now = dt.datetime.now()
    state = load_state()
    products, errors = fetch_all()
    code = 0
    if products:
        new_skus, first_run = update_state(state, products, now)
        state.pop("error", None)
        state.pop("error_time", None)
        if first_run:
            log(f"Första körningen: {len(products)} skor sparade som utgångsläge.")
        else:
            log(f"{len(products)} skor i sortimentet, {len(new_skus)} nya: {', '.join(new_skus) or '–'}")
    else:
        state["error"] = " | ".join(errors) or "okänt fel"
        state["error_time"] = now.strftime("%Y-%m-%d %H:%M")
        log(f"Hämtningen misslyckades: {state['error']}")
        code = 1
    save_state(state)
    build_page(state)
    log(f"Sidan sparad: {PAGE}")
    if args.push and not git_push():
        code = 1
    if args.open:
        webbrowser.open(PAGE.as_uri())
    sys.exit(code)


if __name__ == "__main__":
    main()
