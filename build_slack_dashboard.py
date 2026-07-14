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
EXPORT_DIR = Path(__file__).parent / "slack-exports"
OUTPUT = Path(__file__).parent / "index.html"
LAUNCH_DATE = date(2026, 7, 3)      # workspace go-live
CUTOVER_DATE = date(2026, 7, 30)    # WhatsApp hard cutover
WINDOW_DAYS = 30                    # export window ("Prior 30 Days")

BENCH = {"adoption": 65, "stickiness": 50, "posting": 50, "engagement": 40}

DATE_RE = re.compile(r"([A-Z][a-z]{2})_+(\d{1,2})__(\d{4})")


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


def org_directory(path: Path):
    df = pd.read_excel(path)
    vis = df["Visibility"].str.lower().value_counts()
    created = pd.to_datetime(df["Created"], format="%b %d, %Y", errors="coerce").dt.date
    wave = created.value_counts().sort_index()
    return {
        "total": int(len(df)),
        "private": int(vis.get("private", 0)),
        "public": int(vis.get("public", 0)),
        "wave_labels": [d.strftime("%-d %b") if sys.platform != "win32" else d.strftime("%#d %b") for d in wave.index],
        "wave_values": [int(v) for v in wave.values],
    }


def pct(a, b):
    return round(a / b * 100, 1) if b else 0


# ---------------------------------------------------------------- build
def build() -> bool:
    members = find(r"Member_Analytics")
    channels = find(r"(?<!Limited_)Channel_Analytics", exts=(".csv",))
    orgs = find(r"Private_Limited_Channel_Analytics", exts=(".xlsx",))

    if not members:
        print("!! No *Member_Analytics*.csv found in", EXPORT_DIR)
        return False

    snaps = [member_snapshot(d, p) for d, p in members]
    latest = snaps[-1]
    prev = snaps[-2] if len(snaps) > 1 else None

    ch_rows = channel_table(channels[-1][1]) if channels else []
    org = org_directory(orgs[-1][1]) if orgs else None

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
        "bench": BENCH, "channels": ch_rows, "org": org,
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
.updated{font-size:12px;color:var(--sub);margin-bottom:22px}
section{margin-top:30px}
.sec-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:baseline;gap:10px}
.sec-title span{font-size:12px;color:var(--sub);font-weight:400}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--mist);border-radius:var(--radius);padding:16px 16px 14px;position:relative;overflow:hidden}
.kpi::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--indigo)}
.kpi.orange::before{background:var(--orange)}
.kpi .label{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--sub);font-weight:600}
.kpi .num{font-size:30px;font-weight:700;margin:4px 0 2px;color:var(--indigo)}
.kpi.orange .num{color:var(--orange)}
.kpi .delta{font-size:12px;color:var(--good);font-weight:600}
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
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--sub);text-align:right;padding:8px 10px;border-bottom:2px solid var(--mist);cursor:pointer;white-space:nowrap;user-select:none}
th:first-child,td:first-child{text-align:left}
td{padding:7px 10px;border-bottom:1px solid var(--mist);text-align:right;font-variant-numeric:tabular-nums}
td:first-child{font-weight:500}
tr:hover td{background:#F3F5FF}
.bar-cell{position:relative}
.mini-bar{position:absolute;left:0;top:15%;height:70%;background:#E4EDFF;border-radius:3px;z-index:0}
.bar-cell span{position:relative;z-index:1}
input.search{padding:8px 12px;border:1px solid var(--mist);border-radius:8px;font-size:13px;width:240px;margin-bottom:10px;font-family:'Inter'}
input.search:focus{outline:2px solid var(--blue);border-color:transparent}
.pill{display:inline-block;font-size:10px;font-weight:600;border-radius:999px;padding:2px 8px;background:#EAF4FF;color:var(--blue);margin-left:6px;vertical-align:middle}
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

<section><div class="kpis" id="kpis"></div></section>

<section>
  <div class="row two">
    <div class="panel"><h3>Adoption trajectory</h3>
      <div class="hint">One point per member-analytics export kept in the folder</div>
      <div class="chart-wrap tall"><canvas id="trend"></canvas></div></div>
    <div class="panel"><h3>Adoption funnel — latest</h3>
      <div class="hint">Provisioned → joined → active → engaged → posting</div>
      <div class="chart-wrap tall"><canvas id="funnel"></canvas></div></div>
  </div>
</section>

<section>
  <div class="row three">
    <div class="panel"><h3>Channel creation wave</h3>
      <div class="hint" id="waveHint">New channels per day — org directory</div>
      <div class="chart-wrap"><canvas id="creation"></canvas></div></div>
    <div class="panel"><h3>Public vs private channels</h3>
      <div class="hint">Channel analytics cover public channels only</div>
      <div class="chart-wrap"><canvas id="split"></canvas></div></div>
    <div class="panel"><h3>Active users by platform</h3>
      <div class="hint">Members active per platform (overlapping)</div>
      <div class="chart-wrap"><canvas id="platform"></canvas></div></div>
  </div>
</section>

<section>
  <div class="row two">
    <div class="panel"><h3>Kimbal vs healthy-Slack benchmark</h3>
      <div class="hint">Directional benchmarks; targets apply ~90 days post-cutover</div>
      <div class="chart-wrap tall"><canvas id="bench"></canvas></div></div>
    <div class="panel"><h3>Snapshot deltas</h3>
      <div class="hint" id="deltaHint"></div>
      <div class="chart-wrap tall"><canvas id="deltas"></canvas></div></div>
  </div>
</section>

<section>
  <div class="sec-title">Public channel detail <span>click a column header to sort</span><span class="pill" id="chCount"></span></div>
  <div class="panel">
    <input class="search" id="q" type="search" placeholder="Filter channels…" aria-label="Filter channels">
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
const L = D.latest, P = D.prev, K = D.kpi;
const fmt = n => n.toLocaleString('en-IN');
const delta = (now, before, suffix='') => before==null ? '' :
  (now>=before?'▲ +':'▼ ')+fmt(Math.abs(now-before))+suffix+' vs '+P.label;

document.getElementById('cutoverChip').innerHTML =
  'Cutover: '+D.cutover+' · <b>'+D.days_to_cutover+' days</b> to go';
document.getElementById('updated').textContent =
  'Latest export: '+L.label+' (prior-30-day window) · workspace live since '+D.launch+' · page generated '+D.generated;

/* KPI tiles with formulas */
const tiles = [
 {label:'Provisioned accounts', num:fmt(L.accounts), delta:delta(L.accounts,P&&P.accounts),
  note:fmt(L.joined)+' joined · '+fmt(L.invited)+' invites pending',
  code:'rows in member export', expl:'Count of all accounts (Member + Owner + Admin + Invited) in the latest member analytics CSV.'},
 {label:'Monthly active (MAU)', num:fmt(L.mau), delta:delta(L.mau,P&&P.mau),
  note:K.adoption+'% of provisioned · '+K.adoption_joined+'% of joined',
  code:'Days active > 0', expl:'Members with ≥1 active day in the 30-day window. Adoption = '+fmt(L.mau)+' ÷ '+fmt(L.accounts)+' = '+K.adoption+'%.'},
 {label:'Est. daily active (DAU)', num:'~'+fmt(L.dau), delta:'stickiness ≈ '+K.stickiness+'%',
  note:fmt(L.active_days_sum)+' active-days ÷ '+L.live_days+' live days',
  code:'Σ Days active ÷ live days', expl:fmt(L.active_days_sum)+' total active member-days ÷ '+L.live_days+' days live. Stickiness = DAU ÷ MAU ≈ '+K.stickiness+'%.'},
 {label:'Member messages', num:fmt(L.messages), delta:delta(L.messages,P&&P.messages),
  note:fmt(L.posters)+' posters · '+fmt(L.reactors)+' reactors',
  code:'Σ Messages posted', expl:'Sum of the per-member “Messages posted” column (channels + DMs). Bot/app messages excluded.'},
 {label:'Engagement rate', num:K.engagement+'%', delta:P?('from '+(P.engaged&&P.mau?Math.round(P.engaged/P.mau*1000)/10:0)+'% on '+P.label):'',
  note:fmt(L.engaged)+' posted or reacted ÷ '+fmt(L.mau)+' MAU',
  code:'(posters ∪ reactors) ÷ MAU', expl:fmt(L.engaged)+' members with ≥1 message or reaction ÷ '+fmt(L.mau)+' active members = '+K.engagement+'%.'}
];
if (D.org) tiles.push({label:'Channels (org-wide)', num:fmt(D.org.total), orange:true,
  delta:D.org.private+' private · '+D.org.public+' public', warn:true,
  note:'from org channel directory export',
  code:'rows in org directory', expl:'Count of channels in the org XLSX; split by the “Visibility” column.'});

document.getElementById('kpis').innerHTML = tiles.map(t=>`
  <div class="kpi ${t.orange?'orange':''}">
    <div class="label">${t.label}</div><div class="num">${t.num}</div>
    <div class="delta ${t.warn?'warn':''}">${t.delta||''}</div>
    <div class="note">${t.note}</div>
    <div class="formula"><code>${t.code}</code>${t.expl}</div>
  </div>`).join('');

/* charts */
new Chart(trend,{type:'line',data:{labels:D.snaps.map(s=>s.label),datasets:[
 {label:'Provisioned',data:D.snaps.map(s=>s.accounts),borderColor:C.mist,backgroundColor:C.mist,tension:.3,pointRadius:4},
 {label:'Active (MAU)',data:D.snaps.map(s=>s.mau),borderColor:C.blue,backgroundColor:C.blue,tension:.3,pointRadius:4},
 {label:'Member messages',data:D.snaps.map(s=>s.messages),borderColor:C.orange,backgroundColor:C.orange,tension:.3,pointRadius:4,borderDash:[6,3]}]},
 options:{maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}},
 scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});

new Chart(funnel,{type:'bar',data:{labels:['Provisioned','Joined','Active (MAU)','Engaged','Posted'],
 datasets:[{data:[L.accounts,L.joined,L.mau,L.engaged,L.posters],
 backgroundColor:[C.mist,'#8FA6F5',C.blue,C.indigo,C.orange],borderRadius:6}]},
 options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false}},
 scales:{x:{grid:{color:'#EEF0F8'}},y:{grid:{display:false}}}}});

if (D.org){
 new Chart(creation,{type:'bar',data:{labels:D.org.wave_labels,
  datasets:[{data:D.org.wave_values,backgroundColor:C.indigo,borderRadius:5}]},
  options:{maintainAspectRatio:false,plugins:{legend:{display:false}},
  scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});
 new Chart(split,{type:'doughnut',data:{labels:['Private ('+D.org.private+')','Public ('+D.org.public+')'],
  datasets:[{data:[D.org.private,D.org.public],backgroundColor:[C.orange,C.blue],borderWidth:2,borderColor:'#fff'}]},
  options:{maintainAspectRatio:false,cutout:'62%',plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}}}});
 document.getElementById('waveHint').textContent='New channels per day — org directory ('+D.org.total+' total)';
}

new Chart(platform,{type:'bar',data:{labels:['Desktop','Android','iOS'],
 datasets:[{data:[L.desktop,L.android,L.ios],backgroundColor:[C.indigo,C.blue,C.orange],borderRadius:5}]},
 options:{maintainAspectRatio:false,plugins:{legend:{display:false}},
 scales:{y:{beginAtZero:true,grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});

new Chart(bench,{type:'bar',data:{
 labels:['Adoption (MAU/provisioned)','Stickiness (DAU/MAU)','Members posting (of MAU)','Engagement (of MAU)'],
 datasets:[{label:'Kimbal — '+L.label,data:[K.adoption,K.stickiness,K.posting,K.engagement],backgroundColor:C.indigo,borderRadius:5},
 {label:'Benchmark',data:[D.bench.adoption,D.bench.stickiness,D.bench.posting,D.bench.engagement],backgroundColor:C.mist,borderRadius:5}]},
 options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{position:'bottom',labels:{boxWidth:10,font:{size:11}}}},
 scales:{x:{max:100,grid:{color:'#EEF0F8'},ticks:{callback:v=>v+'%'}},y:{grid:{display:false}}}}});

/* deltas vs previous snapshot */
if (P){
 document.getElementById('deltaHint').textContent='Change from '+P.label+' to '+L.label;
 new Chart(deltas,{type:'bar',data:{labels:['MAU','Messages','Posters','Engaged','Joined'],
  datasets:[{data:[L.mau-P.mau,L.messages-P.messages,L.posters-P.posters,L.engaged-P.engaged,L.joined-P.joined],
  backgroundColor:C.blue,borderRadius:5}]},
  options:{maintainAspectRatio:false,plugins:{legend:{display:false}},
  scales:{y:{grid:{color:'#EEF0F8'}},x:{grid:{display:false}}}}});
} else {
 document.getElementById('deltaHint').textContent='Add a second member export to see deltas';
}

/* channel table */
const rows = D.channels;
document.getElementById('chCount').textContent = rows.length+' public channels';
const maxMsg = Math.max(1,...rows.map(r=>r[3]));
let sortK=3, sortAsc=false;
function render(){
 const q=document.getElementById('q').value.toLowerCase();
 const rs=rows.filter(r=>String(r[0]).toLowerCase().includes(q))
  .sort((a,b)=>{const x=a[sortK],y=b[sortK];return (typeof x==='string'?(''+x).localeCompare(y):x-y)*(sortAsc?1:-1);});
 document.querySelector('#tbl tbody').innerHTML=rs.map(r=>`<tr>
  <td>#${r[0]}</td><td>${String(r[1]).replace(', 2026','')}</td><td>${r[2]}</td>
  <td class="bar-cell"><div class="mini-bar" style="width:${(r[3]/maxMsg*100).toFixed(0)}%"></div><span>${r[3]}</span></td>
  <td>${r[4]}</td><td>${r[5]}</td><td>${r[6]}</td><td>${r[7]}</td></tr>`).join('');
}
document.querySelectorAll('th').forEach(th=>th.addEventListener('click',()=>{
 const k=+th.dataset.k; if(k===sortK) sortAsc=!sortAsc; else {sortK=k; sortAsc=(k<=1);} render();}));
document.getElementById('q').addEventListener('input',render);
render();

document.getElementById('foot').innerHTML =
 '<b>Sources:</b> Slack admin exports in /slack-exports ('+D.snaps.length+' member snapshot(s); latest '+L.label+'). '+
 '<b>Calculations:</b> MAU = members with ≥1 active day. DAU ≈ Σ active member-days ÷ live days since launch (capped at 30). '+
 'Adoption = MAU ÷ provisioned. Stickiness = DAU ÷ MAU. Engagement = (posted ∪ reacted) ÷ MAU. '+
 'Member messages include DMs; channel metrics cover public channels only.';
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
