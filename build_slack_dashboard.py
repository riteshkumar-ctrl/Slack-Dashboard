#!/usr/bin/env python3
"""
Kimbal Slack Adoption Dashboard generator.

Drop Slack admin exports into ./slack-exports/ and run:

    python build_slack_dashboard.py                  # build once -> index.html
    python build_slack_dashboard.py --watch          # rebuild whenever folder changes
    python build_slack_dashboard.py --push           # build, then git commit+push (triggers Azure SWA deploy)

Recognised files (date parsed from filename, e.g. "Jul_8__2026"):
  *Member_Analytics*.csv                 -> per-member activity  (KPIs + trend, one snapshot per date)
  *Channel_Analytics*.csv                -> workspace public-channel metrics (channel table)
  *Private_Limited_Channel_Analytics*.xlsx -> org channel directory (public/private split, creation wave)
  *App_Analytics_Activity*.csv           -> app/bot daily messages (optional)

Keep old export files in the folder: every member export becomes a point on the trend chart.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- config
def _resolve_export_dir() -> Path:
    primary = Path(__file__).parent / "slack-exports"
    sibling = Path(__file__).parent.parent / "slack-exports"
    def has_files(p): return p.exists() and any(f.is_file() for f in p.iterdir())
    if not has_files(primary) and has_files(sibling):
        print(f"NOTE: using {sibling} (found exports there; {primary} is empty)")
        return sibling
    return primary

EXPORT_DIR = _resolve_export_dir()
OUTPUT = Path(__file__).parent / "index.html"
LAUNCH_DATE = date(2026, 7, 3)      # workspace go-live
CUTOVER_DATE = date(2026, 7, 30)    # WhatsApp hard cutover
WINDOW_DAYS = 30                    # export window ("Prior 30 Days")

BENCH = {"adoption": 65, "stickiness": 50, "posting": 50, "engagement": 40}

DATE_RE = re.compile(r"([A-Z][a-z]{2})[ _-]+(\d{1,2})[,_ -]+(\d{4})")


def parse_export_date(filename: str):
    m = DATE_RE.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None


def find(pattern: str, exts=(".csv",)):
    """All matching files with parsed dates, newest last."""
    out = []
    for p in EXPORT_DIR.iterdir():
        if p.suffix.lower() in exts and re.search(pattern, p.name, re.I):
            d = parse_export_date(p.name)
            if d:
                out.append((d, p))
    return sorted(out)


# ---------------------------------------------------------------- metrics
def member_detail(path: Path):
    """Compact per-member records for the client-side drill-down."""
    df = pd.read_csv(path)
    df = df.fillna({"Name": "", "Email": "", "Account type": "", "Last active (UTC)": ""})
    recs = []
    for _, r in df.iterrows():
        recs.append([
            str(r["Name"])[:60],
            str(r["Email"])[:80],
            str(r["Account type"]),
            int(r["Days active"] or 0),
            int(r["Days active (Desktop)"] or 0),
            int(r["Days active (Android)"] or 0),
            int(r["Days active (iOS)"] or 0),
            int(r["Messages posted"] or 0),
            int(r["Reactions added"] or 0),
            str(r["Last active (UTC)"])[:11],
        ])
    return recs


def app_daily(path: Path):
    df = pd.read_csv(path)
    daily = df.groupby("Date")["Messages sent"].sum().sort_index()
    labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%-d %b" if sys.platform != "win32" else "%#d %b")
              for d in daily.index]
    return {"labels": labels, "values": [int(v) for v in daily.values]}


def member_snapshot(d: date, path: Path) -> dict:
    df = pd.read_csv(path)
    joined = df["Account type"].isin(["Member", "Owner", "Admin"]).sum()
    active = int((df["Days active"] > 0).sum())
    live_days = min(max((d - LAUNCH_DATE).days + 1, 1), WINDOW_DAYS)
    dau = round(df["Days active"].sum() / live_days)
    posters = int((df["Messages posted"] > 0).sum())
    reactors = int((df["Reactions added"] > 0).sum())
    engaged = int(((df["Messages posted"] > 0) | (df["Reactions added"] > 0)).sum())
    return {
        "date": d.isoformat(),
        "label": d.strftime("%-d %b") if sys.platform != "win32" else d.strftime("%#d %b"),
        "accounts": int(len(df)),
        "joined": int(joined),
        "invited": int(len(df) - joined),
        "mau": active,
        "dau": int(dau),
        "live_days": live_days,
        "active_days_sum": int(df["Days active"].sum()),
        "messages": int(df["Messages posted"].sum()),
        "posters": posters,
        "reactors": reactors,
        "engaged": engaged,
        "desktop": int((df["Days active (Desktop)"] > 0).sum()),
        "android": int((df["Days active (Android)"] > 0).sum()),
        "ios": int((df["Days active (iOS)"] > 0).sum()),
    }


def channel_table(path: Path):
    df = pd.read_csv(path)
    cols = ["Name", "Created", "Total membership", "Messages posted",
            "Messages posted by members", "Members who posted",
            "Members who viewed", "Reactions added"]
    df = df[cols].sort_values("Messages posted", ascending=False)
    return json.loads(df.to_json(orient="values"))


def _wave(df):
    created = pd.to_datetime(df["Created"], format="%b %d, %Y", errors="coerce").dt.date
    wave = created.value_counts().sort_index()
    fmtd = "%-d %b" if sys.platform != "win32" else "%#d %b"
    return [d.strftime(fmtd) for d in wave.index], [int(v) for v in wave.values]


def org_directory(path: Path):
    df = pd.read_excel(path)
    vis = df["Visibility"].str.lower().value_counts()
    labels, values = _wave(df)
    return {
        "source": "org",
        "total": int(len(df)),
        "private": int(vis.get("private", 0)),
        "public": int(vis.get("public", 0)),
        "wave_labels": labels,
        "wave_values": values,
    }


def org_fallback_from_channels(path: Path):
    """No org XLSX present: build the creation wave from the workspace
    channel CSV (public channels only); private/public split unknown."""
    df = pd.read_csv(path)
    labels, values = _wave(df)
    return {
        "source": "channels",
        "total": int(len(df)),
        "private": None,
        "public": int(len(df)),
        "wave_labels": labels,
        "wave_values": values,
    }


def pct(a, b):
    return round(a / b * 100, 1) if b else 0


# ---------------------------------------------------------------- build
def build() -> bool:
    members = find(r"Member[_ ]Analytics")
    channels = find(r"(?<!Limited[_ ])Channel[_ ]Analytics", exts=(".csv",))
    orgs = find(r"Private[_ ]Limited[_ ]Channel[_ ]Analytics", exts=(".xlsx",))
    apps = find(r"App[_ ]Analytics[_ ]Activity", exts=(".csv",))

    if not members:
        print("!! No *Member_Analytics*.csv found in", EXPORT_DIR)
        files = [p.name for p in EXPORT_DIR.iterdir() if p.is_file()] if EXPORT_DIR.exists() else []
        if files:
            print("   Files present in that folder:")
            for f in files:
                d = parse_export_date(f)
                print(f"     - {f}  ->", "date OK: " + str(d) if d else "NO DATE parsed from name (filename must contain a date like 'Jul 12, 2026')")
        else:
            print("   That folder is EMPTY. Common cause: files were placed in a different")
            print("   'slack-exports' folder. This script uses the one NEXT TO the .py file.")
        return False

    snaps = [member_snapshot(d, p) for d, p in members]
    latest = snaps[-1]
    prev = snaps[-2] if len(snaps) > 1 else None

    ch_rows = channel_table(channels[-1][1]) if channels else []
    org = org_directory(orgs[-1][1]) if orgs else (
        org_fallback_from_channels(channels[-1][1]) if channels else None)
    app = app_daily(apps[-1][1]) if apps else None
    detail = member_detail(members[-1][1])

    stick = pct(latest["dau"], latest["mau"])
    kpi = {
        "adoption": pct(latest["mau"], latest["accounts"]),
        "adoption_joined": pct(latest["mau"], latest["joined"]),
        "stickiness": stick,
        "posting": pct(latest["posters"], latest["mau"]),
        "engagement": pct(latest["engaged"], latest["mau"]),
    }
    days_to_cutover = (CUTOVER_DATE - latest_date(members)).days

    payload = {
        "generated": datetime.now().strftime("%d %b %Y %H:%M"),
        "snaps": snaps, "latest": latest, "prev": prev, "kpi": kpi,
        "bench": BENCH, "channels": ch_rows, "org": org, "app": app,
        "members": detail,
        "cutover": CUTOVER_DATE.strftime("%d %B %Y"),
        "days_to_cutover": days_to_cutover,
        "launch": LAUNCH_DATE.strftime("%-d %b" if sys.platform != "win32" else "%#d %b"),
    }

    OUTPUT.write_text(TEMPLATE.replace("/*__DATA__*/", json.dumps(payload)), encoding="utf-8")
    print(f"OK  index.html rebuilt · latest snapshot {latest['label']} · "
          f"{len(snaps)} trend point(s) · MAU {latest['mau']} · messages {latest['messages']}")
    return True


def latest_date(found):
    return found[-1][0]


def git_push():
    root = Path(__file__).parent
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    r = subprocess.run(["git", "commit", "-m",
                        f"Slack dashboard refresh {datetime.now():%d %b %Y %H:%M}"],
                       cwd=root)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=root, check=True)
        print("OK  pushed - Azure Static Web Apps deploy will trigger")
    else:
        print("--  nothing new to commit")


def watch(push: bool):
    print(f"Watching {EXPORT_DIR} - drop new exports to rebuild (Ctrl+C to stop)")
    seen = None
    while True:
        state = tuple(sorted((p.name, p.stat().st_mtime) for p in EXPORT_DIR.iterdir() if p.is_file()))
        if state != seen:
            seen = state
            time.sleep(1.5)          # let copies finish
            if build() and push:
                git_push()
        time.sleep(3)


# ---------------------------------------------------------------- template
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kimbal Slack Analytics</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
:root{--indigo:#0E0FD9;--blue:#0081FF;--orange:#F96B00;--ink:#14142B;--sub:#5D5D74;
--paper:#F7F8FC;--card:#FFFFFF;--mist:#E6E7F2;--good:#0B9E6C;--radius:14px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,sans-serif;background:var(--paper);color:var(--ink);padding:28px 32px 48px;font-size:14px}
h1,h2,h3,.num{font-family:'Space Grotesk','Inter',sans-serif}
header{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:12px;margin-bottom:6px}
.brand{display:flex;align-items:center;gap:14px}
.brand-mark{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,var(--indigo),var(--blue));display:flex;align-items:center;justify-content:center;color:#fff;font-family:'Space Grotesk';font-weight:700;font-size:20px}
h1{font-size:24px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--sub);font-size:13px;margin-top:2px}
.cutover{background:var(--ink);color:#fff;border-radius:999px;padding:10px 18px;display:flex;align-items:center;gap:10px;font-size:13px}
.cutover b{font-family:'Space Grotesk';font-size:18px;color:#FFB27A}
.updated{font-size:12px;color:var(--sub);margin-bottom:16px}
section{margin-top:30px}
.sec-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sec-title span{font-size:12px;color:var(--sub);font-weight:400}

/* filter bar */
#filterbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:34px;margin-bottom:4px}
#filterbar .fb-label{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--sub);font-weight:600}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--indigo);color:#fff;border-radius:999px;padding:5px 12px;font-size:12px;font-weight:500;cursor:pointer;border:none;font-family:'Inter'}
.chip.plat{background:var(--orange)}
.chip .x{font-weight:700;opacity:.75}
.chip:hover .x{opacity:1}
.chip.ghost{background:transparent;color:var(--sub);border:1px dashed var(--mist);cursor:default}
#clearAll{background:none;border:none;color:var(--blue);font-size:12px;cursor:pointer;font-family:'Inter';font-weight:600;display:none}

/* KPI tiles */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--mist);border-radius:var(--radius);padding:16px 16px 14px;position:relative;overflow:hidden;cursor:pointer;transition:box-shadow .15s,transform .15s}
.kpi:hover{box-shadow:0 4px 14px rgba(14,15,217,.10);transform:translateY(-1px)}
.kpi.static{cursor:default}
.kpi.static:hover{box-shadow:none;transform:none}
.kpi::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--indigo)}
.kpi.orange::before{background:var(--orange)}
.kpi.on{outline:2px solid var(--indigo);outline-offset:-2px;background:#F4F6FF}
.kpi .label{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--sub);font-weight:600;display:flex;justify-content:space-between;gap:6px}
.kpi .label .tap{font-size:9px;color:var(--blue);letter-spacing:.03em;font-weight:600;opacity:0;transition:opacity .15s}
.kpi:hover .label .tap{opacity:1}
.kpi.on .label .tap{opacity:1}
.kpi .num{font-size:30px;font-weight:700;margin:4px 0 2px;color:var(--indigo)}
.kpi.orange .num{color:var(--orange)}
.kpi .delta{font-size:12px;color:var(--good);font-weight:600;min-height:15px}
.kpi .delta.warn{color:var(--orange)}
.kpi .note{font-size:11.5px;color:var(--sub);margin-top:2px}
.kpi .formula{margin-top:9px;padding-top:7px;border-top:1px dashed var(--mist);font-size:10.5px;line-height:1.45;color:var(--sub)}
.kpi .formula code{font-family:'Space Grotesk',monospace;font-size:10.5px;font-weight:600;color:var(--ink);background:#F1F2FA;border-radius:4px;padding:1px 5px;display:inline-block;margin-bottom:2px}

.row{display:grid;gap:14px;margin-top:14px}
.row.two{grid-template-columns:3fr 2fr}
.row.three{grid-template-columns:1fr 1fr 1fr}
@media(max-width:900px){.row.two,.row.three{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--mist);border-radius:var(--radius);padding:18px}
.panel h3{font-size:13.5px;font-weight:600;margin-bottom:2px}
.panel .hint{font-size:11.5px;color:var(--sub);margin-bottom:12px}
.chart-wrap{position:relative;height:250px}
.chart-wrap.tall{height:290px}
.clicky{font-size:10px;color:var(--blue);font-weight:600;letter-spacing:.04em}

/* tables */
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--sub);text-align:right;padding:8px 10px;border-bottom:2px solid var(--mist);cursor:pointer;white-space:nowrap;user-select:none}
th:first-child,td:first-child{text-align:left}
td{padding:7px 10px;border-bottom:1px solid var(--mist);text-align:right;font-variant-numeric:tabular-nums}
td:first-child{font-weight:500}
tr:hover td{background:#F3F5FF}
td.lft{text-align:left;font-weight:400;color:var(--sub)}
.bar-cell{position:relative}
.mini-bar{position:absolute;left:0;top:15%;height:70%;background:#E4EDFF;border-radius:3px;z-index:0}
.bar-cell span{position:relative;z-index:1}
.pico{width:15px;height:15px;vertical-align:-2px;margin:0 3px}
input.search{padding:8px 12px;border:1px solid var(--mist);border-radius:8px;font-size:13px;width:240px;font-family:'Inter'}
input.search:focus{outline:2px solid var(--blue);border-color:transparent}
.pill{display:inline-block;font-size:10px;font-weight:600;border-radius:999px;padding:2px 8px;background:#EAF4FF;color:var(--blue);margin-left:6px;vertical-align:middle}
.btn{padding:8px 14px;border-radius:8px;border:1px solid var(--mist);background:#fff;font-size:12px;font-weight:600;color:var(--indigo);cursor:pointer;font-family:'Inter'}
.btn:hover{background:#F4F6FF}
.tools{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.count-note{font-size:12px;color:var(--sub)}
footer{margin-top:36px;font-size:11px;color:var(--sub);line-height:1.7;border-top:1px solid var(--mist);padding-top:14px}
footer b{color:var(--ink)}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">K</div>
    <div><h1>Kimbal Slack Analytics</h1>
    <div class="sub">WhatsApp → Slack migration · pre-cutover adoption tracking</div></div>
  </div>
  <div class="cutover" id="cutoverChip"></div>
</header>
<div class="updated" id="updated"></div>

<div id="filterbar">
  <span class="fb-label">Filters</span>
  <span id="chips"><span class="chip ghost">Click any tile, funnel bar or platform bar to drill down</span></span>
  <button id="clearAll">✕ Clear all</button>
</div>

<section style="margin-top:10px"><div class="kpis" id="kpis"></div></section>

<section>
  <div class="row two">
    <div class="panel"><h3>Adoption trajectory</h3>
      <div class="hint">One point per member-analytics export · unaffected by filters</div>
      <div class="chart-wrap tall"><canvas id="trend"></canvas></div></div>
    <div class="panel"><h3>Adoption funnel <span class="clicky">· click a bar to filter</span></h3>
      <div class="hint" id="funnelHint">Provisioned → joined → active → engaged → posting</div>
      <div class="chart-wrap tall"><canvas id="funnel"></canvas></div></div>
  </div>
</section>

<section>
  <div class="row three">
    <div class="panel"><h3>Active users by platform <span class="clicky">· click to filter</span></h3>
      <div class="hint">Members active per platform (overlapping)</div>
      <div class="chart-wrap"><canvas id="platform"></canvas></div></div>
    <div class="panel"><h3>Channel creation wave</h3>
      <div class="hint" id="waveHint">New channels per day — org directory</div>
      <div class="chart-wrap"><canvas id="creation"></canvas></div></div>
    <div class="panel"><h3>Public vs private channels</h3>
      <div class="hint">Channel analytics cover public channels only</div>
      <div class="chart-wrap"><canvas id="split"></canvas></div></div>
  </div>
</section>

<section>
  <div class="row two">
    <div class="panel"><h3>Kimbal vs healthy-Slack benchmark</h3>
      <div class="hint">Solid = current filtered view · directional benchmarks, ~90 days post-cutover</div>
      <div class="chart-wrap tall"><canvas id="bench"></canvas></div></div>
    <div class="panel"><h3>App &amp; bot messages per day</h3>
      <div class="hint" id="appHint">From app analytics export · unaffected by filters</div>
      <div class="chart-wrap tall"><canvas id="appDaily"></canvas></div></div>
  </div>
</section>

<!-- MEMBER DRILL-DOWN -->
<section id="memberSec">
  <div class="sec-title">Member drill-down <span id="memberDesc"></span><span class="pill" id="memCount"></span></div>
  <div class="panel">
    <div class="tools">
      <input class="search" id="mq" type="search" placeholder="Search name or email…" aria-label="Search members">
      <button class="btn" id="copyEmails">Copy emails of filtered list</button>
      <button class="btn" id="dlCsv">Download filtered CSV</button>
      <span class="count-note" id="copied"></span>
    </div>
    <div style="overflow-x:auto;max-height:480px;overflow-y:auto">
    <table id="mtbl"><thead><tr>
      <th data-k="0">Name</th><th data-k="1">Email</th><th data-k="2">Type</th>
      <th data-k="3">Days active</th><th data-k="7">Messages</th><th data-k="8">Reactions</th>
      <th data-k="10">Platforms</th><th data-k="9">Last active</th>
    </tr></thead><tbody></tbody></table>
    </div>
  </div>
</section>

<!-- CHANNEL TABLE -->
<section>
  <div class="sec-title">Public channel detail <span>click a column header to sort · independent of member filters</span><span class="pill" id="chCount"></span></div>
  <div class="panel">
    <input class="search" id="q" type="search" placeholder="Filter channels…" aria-label="Filter channels" style="margin-bottom:10px">
    <div style="overflow-x:auto">
    <table id="tbl"><thead><tr>
      <th data-k="0">Channel</th><th data-k="1">Created</th><th data-k="2">Members</th>
      <th data-k="3">Messages</th><th data-k="4">By members</th><th data-k="5">Posters</th>
      <th data-k="6">Viewers</th><th data-k="7">Reactions</th>
    </tr></thead><tbody></tbody></table>
    </div>
  </div>
</section>

<footer id="foot"></footer>

<script>
const D = /*__DATA__*/;
const C = {indigo:'#0E0FD9', blue:'#0081FF', orange:'#F96B00', mist:'#C9CBE8', sub:'#5D5D74'};
Chart.defaults.font.family='Inter, sans-serif'; Chart.defaults.color=C.sub;
const L=D.latest, P=D.prev, M=D.members; // member rec: [name,email,type,days,dDesk,dAnd,dIos,msgs,reacts,lastActive]
const fmt=n=>n.toLocaleString('en-IN');

document.getElementById('cutoverChip').innerHTML='Cutover: '+D.cutover+' · <b>'+D.days_to_cutover+' days</b> to go';
document.getElementById('updated').textContent='Latest member export: '+L.label+' (prior-30-day window) · workspace live since '+D.launch+' · page generated '+D.generated;

/* ------------ filter model ------------ */
const STAGE_DEF={
 invited:{label:'Invites pending', test:m=>m[2]==='Invited Member'},
 joined:{label:'Joined', test:m=>m[2]!=='Invited Member'},
 active:{label:'Active (MAU)', test:m=>m[3]>0},
 dormant:{label:'Joined but inactive', test:m=>m[2]!=='Invited Member'&&m[3]===0},
 engaged:{label:'Engaged', test:m=>m[7]>0||m[8]>0},
 posted:{label:'Posted messages', test:m=>m[7]>0}
};
const PLAT_DEF={desktop:{label:'Desktop', i:4}, android:{label:'Android', i:5}, ios:{label:'iOS', i:6}};
let stage=null, plat=null, mq='';

function filtered(){
 return M.filter(m=>(!stage||STAGE_DEF[stage].test(m))&&(!plat||m[PLAT_DEF[plat].i]>0)
  &&(!mq||m[0].toLowerCase().includes(mq)||m[1].toLowerCase().includes(mq)));
}
function baseFiltered(){ // without search box, for KPIs/charts
 return M.filter(m=>(!stage||STAGE_DEF[stage].test(m))&&(!plat||m[PLAT_DEF[plat].i]>0));
}
function stats(rows){
 const s={accounts:rows.length,joined:0,invited:0,mau:0,daysSum:0,msgs:0,posters:0,reactors:0,engaged:0,desktop:0,android:0,ios:0};
 for(const m of rows){
  if(m[2]==='Invited Member') s.invited++; else s.joined++;
  if(m[3]>0) s.mau++;
  s.daysSum+=m[3]; s.msgs+=m[7];
  if(m[7]>0) s.posters++;
  if(m[8]>0) s.reactors++;
  if(m[7]>0||m[8]>0) s.engaged++;
  if(m[4]>0) s.desktop++; if(m[5]>0) s.android++; if(m[6]>0) s.ios++;
 }
 s.dau=Math.round(s.daysSum/L.live_days);
 s.adoption=s.accounts?+(s.mau/s.accounts*100).toFixed(1):0;
 s.adoptionJoined=s.joined?+(s.mau/s.joined*100).toFixed(1):0;
 s.stick=s.mau?+(s.dau/s.mau*100).toFixed(1):0;
 s.posting=s.mau?+(s.posters/s.mau*100).toFixed(1):0;
 s.engRate=s.mau?+(s.engaged/s.mau*100).toFixed(1):0;
 return s;
}

/* ------------ KPI band ------------ */
function renderKpis(s){
 const noFilter=!stage&&!plat;
 const dlt=(now,key)=>noFilter&&P?((now>=P[key]?'▲ +':'▼ ')+fmt(Math.abs(now-P[key]))+' vs '+P.label):'filtered view';
 const tiles=[
  {id:'k-acc',stageKey:null,label:'Accounts in view',num:fmt(s.accounts),delta:noFilter&&P?dlt(s.accounts,'accounts'):'filtered view',
   note:fmt(s.joined)+' joined · '+fmt(s.invited)+' invites pending',
   code:'rows in member export',expl:'All accounts matching the active filters (Member + Owner + Admin + Invited).',static:true},
  {id:'k-mau',stageKey:'active',label:'Monthly active (MAU)',num:fmt(s.mau),delta:noFilter&&P?dlt(s.mau,'mau'):'filtered view',
   note:s.adoption+'% of view · '+s.adoptionJoined+'% of joined',
   code:'Days active > 0',expl:'Members with ≥1 active day in the 30-day window. Adoption = '+fmt(s.mau)+' ÷ '+fmt(s.accounts)+' = '+s.adoption+'%.'},
  {id:'k-dau',stageKey:null,label:'Est. daily active (DAU)',num:'~'+fmt(s.dau),delta:'stickiness ≈ '+s.stick+'%',
   note:fmt(s.daysSum)+' active-days ÷ '+L.live_days+' live days',
   code:'Σ Days active ÷ live days',expl:fmt(s.daysSum)+' active member-days ÷ '+L.live_days+' days live. Stickiness = DAU ÷ MAU ≈ '+s.stick+'%.',static:true},
  {id:'k-msg',stageKey:'posted',label:'Member messages',num:fmt(s.msgs),delta:noFilter&&P?dlt(s.msgs,'messages'):'filtered view',
   note:fmt(s.posters)+' posters · '+fmt(s.reactors)+' reactors',
   code:'Σ Messages posted',expl:'Sum of per-member “Messages posted” (channels + DMs). Bot/app messages excluded.'},
  {id:'k-eng',stageKey:'engaged',label:'Engagement rate',num:s.engRate+'%',delta:noFilter?(P?'from '+(P.engaged&&P.mau?(P.engaged/P.mau*100).toFixed(1):0)+'% on '+P.label:''):'filtered view',
   note:fmt(s.engaged)+' posted or reacted ÷ '+fmt(s.mau)+' MAU',
   code:'(posters ∪ reactors) ÷ MAU',expl:fmt(s.engaged)+' members with ≥1 message or reaction ÷ '+fmt(s.mau)+' active = '+s.engRate+'%.'},
  {id:'k-inv',stageKey:'invited',label:'Invites pending',num:fmt(s.invited),orange:true,warn:true,
   delta:s.accounts?(s.invited/s.accounts*100).toFixed(0)+'% of view':'',
   note:'accounts never claimed',
   code:"type = 'Invited Member'",expl:'Accounts provisioned but not yet accepted. Click to get the nudge list, then “Copy emails”.'}
 ];
 document.getElementById('kpis').innerHTML=tiles.map(t=>`
  <div class="kpi ${t.orange?'orange':''} ${t.static?'static':''} ${t.stageKey&&stage===t.stageKey?'on':''}" ${t.stageKey?`data-stage="${t.stageKey}"`:''}>
   <div class="label">${t.label}${t.stageKey?'<span class="tap">'+(stage===t.stageKey?'FILTER ON':'CLICK TO FILTER')+'</span>':''}</div>
   <div class="num">${t.num}</div><div class="delta ${t.warn?'warn':''}">${t.delta||''}</div>
   <div class="note">${t.note}</div>
   <div class="formula"><code>${t.code}</code>${t.expl}</div></div>`).join('');
 document.querySelectorAll('.kpi[data-stage]').forEach(el=>el.addEventListener('click',()=>{
  stage=(stage===el.dataset.stage)?null:el.dataset.stage; refresh();}));
}

/* ------------ charts ------------ */
new Chart(trend,{type:'line',data:{labels:D.snaps.map(s=>s.label),datasets:[
 {label:'Provisioned',data:D.snaps.map(s=>s.accounts),borderColor:C.mist,backgroundColor:C.mist,tension:.3,pointRadius:4},
 {label:'Active (MAU)',data:D.snaps.map(s=>s.mau),borderColor:C.blue,backgroundColor:C.blue,tension:.3,pointRadius:4},
 {label:'Member messages',data:D.snaps.map(s=>s.messages),borderColor:C.orange,backgroundColor:C.orange,tension:.3,pointRadius:4,borderDash:[6,3]}]},
 options:{maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}},
 scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});

const FUNNEL_STAGES=[null,'joined','active','engaged','posted'];
const funnelChart=new Chart(funnel,{type:'bar',data:{labels:['Provisioned','Joined','Active (MAU)','Engaged','Posted'],
 datasets:[{data:[],backgroundColor:[C.mist,'#8FA6F5',C.blue,C.indigo,C.orange],borderRadius:6}]},
 options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false}},
 onClick:(e,els)=>{if(!els.length)return;const k=FUNNEL_STAGES[els[0].index];if(k){stage=(stage===k)?null:k;refresh();}},
 onHover:(e,els)=>{e.native.target.style.cursor=els.length&&FUNNEL_STAGES[els[0].index]?'pointer':'default';},
 scales:{x:{grid:{color:'#EEF0F8'}},y:{grid:{display:false}}}}});

const PLAT_KEYS=['desktop','android','ios'];
const platChart=new Chart(platform,{type:'bar',data:{labels:['Desktop','Android','iOS'],
 datasets:[{data:[],backgroundColor:[C.indigo,C.blue,C.orange],borderRadius:5}]},
 options:{maintainAspectRatio:false,plugins:{legend:{display:false}},
 onClick:(e,els)=>{if(!els.length)return;const k=PLAT_KEYS[els[0].index];plat=(plat===k)?null:k;refresh();},
 onHover:(e,els)=>{e.native.target.style.cursor=els.length?'pointer':'default';},
 scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});

if(D.org){
 new Chart(creation,{type:'bar',data:{labels:D.org.wave_labels,datasets:[{data:D.org.wave_values,backgroundColor:C.indigo,borderRadius:5}]},
  options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});
 document.getElementById('waveHint').textContent = D.org.source==='org'
  ? 'New channels per day — org directory ('+D.org.total+' total)'
  : 'New channels per day — public channels only ('+D.org.total+'); add the org directory XLSX for all channels';
 if(D.org.private!==null){
  new Chart(split,{type:'doughnut',data:{labels:['Private ('+D.org.private+')','Public ('+D.org.public+')'],
   datasets:[{data:[D.org.private,D.org.public],backgroundColor:[C.orange,C.blue],borderWidth:2,borderColor:'#fff'}]},
   options:{maintainAspectRatio:false,cutout:'62%',plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}}}});
 }else{
  document.getElementById('split').parentElement.innerHTML=
   '<div style="height:100%;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--sub);font-size:12px;line-height:1.7;padding:0 10px">'+
   'Private vs public split needs the org-level export.<br><b style="color:var(--ink)">Slack Admin → Manage organisation → Channels → Export CSV/XLSX</b><br>'+
   'Drop <i>Kimbal Private Limited Channel Analytics…xlsx</i> into slack-exports and rebuild.</div>';
 }
}

const benchChart=new Chart(bench,{type:'bar',data:{
 labels:['Adoption (MAU/provisioned)','Stickiness (DAU/MAU)','Members posting (of MAU)','Engagement (of MAU)'],
 datasets:[{label:'Kimbal — current view',data:[],backgroundColor:C.indigo,borderRadius:5},
 {label:'Benchmark',data:[D.bench.adoption,D.bench.stickiness,D.bench.posting,D.bench.engagement],backgroundColor:C.mist,borderRadius:5}]},
 options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}},
 scales:{x:{max:100,grid:{color:'#EEF0F8'},ticks:{callback:v=>v+'%'}},y:{grid:{display:false}}}}});

if(D.app){
 new Chart(appDaily,{type:'bar',data:{labels:D.app.labels,datasets:[{data:D.app.values,backgroundColor:C.blue,borderRadius:4}]},
  options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});
}else{
 document.getElementById('appHint').textContent='Add an App Analytics Activity export to populate';
}

/* ------------ member table ------------ */
let msortK=3, msortAsc=false;
const ICO={
 desk:'<svg class="pico" viewBox="0 0 576 512" aria-label="Desktop" role="img"><title>Desktop</title><path fill="#5D5D74" d="M528 0H48C21.5 0 0 21.5 0 48v288c0 26.5 21.5 48 48 48h192l-16 48h-72c-13.3 0-24 10.7-24 24s10.7 24 24 24h272c13.3 0 24-10.7 24-24s-10.7-24-24-24h-72l-16-48h192c26.5 0 48-21.5 48-48V48c0-26.5-21.5-48-48-48zm-16 320H64V64h448v256z"/></svg>',
 and:'<svg class="pico" viewBox="0 0 576 512" aria-label="Android" role="img"><title>Android</title><path fill="#3DDC84" d="M420.55 301.93a24 24 0 1 1 24-24 24 24 0 0 1-24 24m-265.1 0a24 24 0 1 1 24-24 24 24 0 0 1-24 24m273.7-144.48 47.94-83a10 10 0 1 0-17.27-10l-48.54 84.07a301.25 301.25 0 0 0-246.56 0L116.18 64.45a10 10 0 1 0-17.27 10l47.94 83C64.53 202.22 8.24 285.55 0 384h576c-8.24-98.45-64.54-181.78-146.85-226.55"/></svg>',
 ios:'<svg class="pico" viewBox="0 0 384 512" aria-label="Apple" role="img"><title>Apple iOS</title><path fill="#1A1A2E" d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C63.3 141.2 4 184.8 4 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z"/></svg>'};
function platIcons(m){const p=[];if(m[4]>0)p.push(ICO.desk);if(m[5]>0)p.push(ICO.and);if(m[6]>0)p.push(ICO.ios);return p.join('')||'—';}
function renderMembers(){
 const rows=filtered().slice()
  .sort((a,b)=>{let x,y;
   if(msortK===10){x=(a[4]>0)+(a[5]>0)+(a[6]>0);y=(b[4]>0)+(b[5]>0)+(b[6]>0);}
   else{x=a[msortK];y=b[msortK];}
   return (typeof x==='string'?(''+x).localeCompare(y):x-y)*(msortAsc?1:-1);});
 document.getElementById('memCount').textContent=fmt(rows.length)+' members';
 const desc=[stage?STAGE_DEF[stage].label:null,plat?PLAT_DEF[plat].label+' users':null].filter(Boolean).join(' · ');
 document.getElementById('memberDesc').textContent=desc?('showing: '+desc):'showing all accounts — click a metric above to filter';
 document.querySelector('#mtbl tbody').innerHTML=rows.slice(0,800).map(m=>`<tr>
  <td>${m[0]||'—'}</td><td class="lft">${m[1]||'—'}</td><td class="lft">${m[2].replace(' Member','')}</td>
  <td>${m[3]}</td><td>${m[7]}</td><td>${m[8]}</td><td style="text-align:center">${platIcons(m)}</td>
  <td class="lft">${m[9]&&m[9]!=='nan'?m[9]:'—'}</td></tr>`).join('');
}
document.querySelectorAll('#mtbl th').forEach(th=>th.addEventListener('click',()=>{
 const k=+th.dataset.k; if(k===msortK) msortAsc=!msortAsc; else {msortK=k; msortAsc=(k<=2);} renderMembers();}));
document.getElementById('mq').addEventListener('input',e=>{mq=e.target.value.toLowerCase();renderMembers();});

document.getElementById('copyEmails').addEventListener('click',()=>{
 const emails=filtered().map(m=>m[1]).filter(e=>e&&e!=='nan');
 navigator.clipboard.writeText(emails.join('; ')).then(()=>{
  const el=document.getElementById('copied');el.textContent=emails.length+' emails copied';setTimeout(()=>el.textContent='',2500);});
});
document.getElementById('dlCsv').addEventListener('click',()=>{
 const head='Name,Email,Type,Days active,Desktop days,Android days,iOS days,Messages,Reactions,Last active\n';
 const csv=head+filtered().map(m=>m.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\n');
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
 a.download='kimbal_slack_members_filtered.csv';a.click();
});

/* ------------ filter chips + refresh ------------ */
function renderChips(){
 const chips=[];
 if(stage)chips.push(`<button class="chip" data-t="stage">${STAGE_DEF[stage].label} <span class="x">✕</span></button>`);
 if(plat)chips.push(`<button class="chip plat" data-t="plat">${PLAT_DEF[plat].label} <span class="x">✕</span></button>`);
 document.getElementById('chips').innerHTML=chips.length?chips.join(''):
  '<span class="chip ghost">Click any tile, funnel bar or platform bar to drill down</span>';
 document.getElementById('clearAll').style.display=chips.length?'inline':'none';
 document.querySelectorAll('#chips .chip[data-t]').forEach(b=>b.addEventListener('click',()=>{
  if(b.dataset.t==='stage')stage=null;else plat=null;refresh();}));
}
document.getElementById('clearAll').addEventListener('click',()=>{stage=null;plat=null;refresh();});

function refresh(){
 const s=stats(baseFiltered());
 renderKpis(s); renderChips(); renderMembers();
 funnelChart.data.datasets[0].data=[s.accounts,s.joined,s.mau,s.engaged,s.posters]; funnelChart.update();
 platChart.data.datasets[0].data=[s.desktop,s.android,s.ios]; platChart.update();
 benchChart.data.datasets[0].data=[s.adoption,s.stick,s.posting,s.engRate];
 benchChart.data.datasets[0].label='Kimbal — '+((stage||plat)?'filtered view':L.label); benchChart.update();
}

/* ------------ channel table ------------ */
const rows=D.channels;
document.getElementById('chCount').textContent=rows.length+' public channels';
const maxMsg=Math.max(1,...rows.map(r=>r[3]));
let sortK=3,sortAsc=false;
function renderChannels(){
 const q=document.getElementById('q').value.toLowerCase();
 const rs=rows.filter(r=>String(r[0]).toLowerCase().includes(q))
  .sort((a,b)=>{const x=a[sortK],y=b[sortK];return (typeof x==='string'?(''+x).localeCompare(y):x-y)*(sortAsc?1:-1);});
 document.querySelector('#tbl tbody').innerHTML=rs.map(r=>`<tr>
  <td>#${r[0]}</td><td>${String(r[1]).replace(', 2026','')}</td><td>${r[2]}</td>
  <td class="bar-cell"><div class="mini-bar" style="width:${(r[3]/maxMsg*100).toFixed(0)}%"></div><span>${r[3]}</span></td>
  <td>${r[4]}</td><td>${r[5]}</td><td>${r[6]}</td><td>${r[7]}</td></tr>`).join('');
}
document.querySelectorAll('#tbl th').forEach(th=>th.addEventListener('click',()=>{
 const k=+th.dataset.k; if(k===sortK) sortAsc=!sortAsc; else {sortK=k; sortAsc=(k<=1);} renderChannels();}));
document.getElementById('q').addEventListener('input',renderChannels);

document.getElementById('foot').innerHTML=
 '<b>Sources:</b> Slack admin exports in /slack-exports ('+D.snaps.length+' member snapshot(s); latest '+L.label+'). '+
 '<b>Calculations:</b> MAU = members with ≥1 active day. DAU ≈ Σ active member-days ÷ live days since launch (capped at 30). '+
 'Adoption = MAU ÷ accounts in view. Stickiness = DAU ÷ MAU. Engagement = (posted ∪ reacted) ÷ MAU. '+
 'Filters recompute every member metric client-side; trend, channel and app sections come from separate exports and are unaffected. '+
 'Member messages include DMs; channel metrics cover public channels only.';

refresh(); renderChannels();
</script>
</body>
</html>"""

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the Kimbal Slack dashboard from exports")
    ap.add_argument("--watch", action="store_true", help="keep running and rebuild on folder changes")
    ap.add_argument("--push", action="store_true", help="git commit+push after building")
    args = ap.parse_args()

    EXPORT_DIR.mkdir(exist_ok=True)
    if args.watch:
        watch(args.push)
    else:
        if build() and args.push:
            git_push()
