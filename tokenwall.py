#!/usr/bin/env python3
"""token-wallpaper: your local AI coding token usage as a macOS dashboard wallpaper.

Subcommands:
  update     collect with ccusage, render one image per screen, set the wallpaper
  render     render one image to a file (for previews; --demo uses made-up data)
  collect    only refresh history.json
  import     merge an existing `ccusage daily --json` report into history.json
  install    write and load the LaunchAgent (every 30 min)
  uninstall  unload and remove the LaunchAgent (history is kept)

Environment overrides: TOKENWALL_NPX, TOKENWALL_BROWSER, TOKENWALL_CCUSAGE.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
DATA = HOME / "Library/Application Support/token-wallpaper"
HISTORY = DATA / "history.json"
LOG = DATA / "update.log"
LABEL = "io.github.lycfyi.token-wallpaper"
PLIST = HOME / f"Library/LaunchAgents/{LABEL}.plist"
MONO_FONT = Path("/System/Library/Fonts/SFNSMono.ttf")
# pinned so a new ccusage release cannot change the report format under us
CCUSAGE = os.environ.get("TOKENWALL_CCUSAGE", "ccusage@20.0.26")
# Chromium-family browsers that take the same headless flags, in order of preference
BROWSERS = ["Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Google Chrome Canary"]

WINDOW = 30  # deltas compare the last WINDOW days with the WINDOW days before
HEAT_WEEKS = 26
DESIGN_W, DESIGN_H = 1600, 900  # the dashboard is laid out at this size, then zoomed to fit


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- collect

def search_path() -> str:
    """PATH plus the usual node install locations; launchd starts jobs with a bare PATH."""
    extra = [HOME / ".local/share/fnm/aliases/default/bin", HOME / ".volta/bin",
             *sorted((HOME / ".nvm/versions/node").glob("*/bin"), reverse=True),
             Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    dirs = os.environ.get("PATH", "").split(":") + [str(d) for d in extra] + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(d for d in dirs if d))


def find_npx() -> str:
    npx = os.environ.get("TOKENWALL_NPX") or shutil.which("npx", path=search_path())
    if not npx:
        raise RuntimeError("npx not found; install Node.js or set TOKENWALL_NPX")
    return npx


def find_browser() -> Path:
    if os.environ.get("TOKENWALL_BROWSER"):
        return Path(os.environ["TOKENWALL_BROWSER"])
    for name in BROWSERS:
        for apps in (Path("/Applications"), HOME / "Applications"):
            exe = apps / f"{name}.app/Contents/MacOS/{name}"
            if exe.exists():
                return exe
    raise RuntimeError("no Chromium-family browser found; install Google Chrome or set TOKENWALL_BROWSER")


def run_ccusage() -> dict:
    env = dict(os.environ, PATH=search_path())
    base = [find_npx(), "-y", CCUSAGE, "daily", "--json"]
    for extra in (["--by-agent"], []):
        r = subprocess.run(base + extra, capture_output=True, text=True, env=env, timeout=600)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    raise RuntimeError(f"ccusage failed: {r.stderr.strip()[-300:]}")


def load_history() -> dict:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return {"days": {}, "collected_at": None}


def merge(history: dict, report: dict) -> int:
    """Newer report wins, except where logs were pruned and a day shrank."""
    changed = 0
    for day in report.get("daily", []):
        key = day.get("period") or day.get("date")
        old = history["days"].get(key)
        if old and day.get("totalTokens", 0) < old.get("totalTokens", 0):
            continue
        if old != day:
            history["days"][key] = day
            changed += 1
    return changed


def save_history(history: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, separators=(",", ":")))
    tmp.replace(HISTORY)


def collect() -> tuple[dict, bool]:
    """Returns (history, fresh). On ccusage failure the old history is kept."""
    history = load_history()
    try:
        n = merge(history, run_ccusage())
        history["collected_at"] = dt.datetime.now().isoformat(timespec="seconds")
        save_history(history)
        log(f"collect ok, {n} days changed, {len(history['days'])} days total")
        return history, True
    except Exception as e:  # keep drawing from what we have
        log(f"collect failed: {e}")
        return history, False


# ---------------------------------------------------------------- metrics

TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens")


def model_name(raw: str) -> str:
    # by-agent reports prefix some names with "[agent] "; merge them
    return re.sub(r"^\[[^\]]*\]\s*", "", raw)


def compute(history: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    days = {dt.date.fromisoformat(k): v for k, v in history["days"].items() if dt.date.fromisoformat(k) <= today}
    active = sorted(d for d, v in days.items() if v.get("totalTokens", 0) > 0)

    def day(d, key):
        v = days.get(d, {})
        if key == "cache":
            return v.get("cacheReadTokens", 0) + v.get("cacheCreationTokens", 0)
        return v.get(key, 0)

    def window_sum(key, end_offset):
        return sum(day(today - dt.timedelta(days=i), key) for i in range(end_offset, end_offset + WINDOW))

    def delta(key):
        cur, prev = window_sum(key, 0), window_sum(key, WINDOW)
        return (cur - prev) / prev if prev else None

    tot = defaultdict(float)
    models = defaultdict(float)
    agents = defaultdict(float)
    for d in active:
        v = days[d]
        for k in (*TOKEN_KEYS, "totalTokens", "totalCost"):
            tot[k] += v.get(k, 0)
        for m in v.get("modelBreakdowns", []):
            name = model_name(m["modelName"])
            if not name.startswith("<"):
                models[name] += sum(m.get(k, 0) for k in TOKEN_KEYS)
        for a in v.get("agents", []):
            agents[a["agent"]] += a.get("totalTokens", 0)

    # streaks: the current one may end today or yesterday
    longest = run = 0
    prev = None
    for d in active:
        run = run + 1 if prev and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d
    current = run if active and (today - active[-1]).days <= 1 else 0

    def active_in(end_offset):
        return sum(1 for i in range(end_offset, end_offset + WINDOW)
                   if day(today - dt.timedelta(days=i), "totalTokens") > 0)

    act_cur, act_prev = active_in(0), active_in(WINDOW)
    avg30_cost = window_sum("totalCost", 0) / WINDOW
    avg30_tok = window_sum("totalTokens", 0) / WINDOW
    best = max(active, key=lambda d: days[d].get("totalCost", 0)) if active else None
    trend = [today - dt.timedelta(days=i) for i in range(WINDOW - 1, -1, -1)]
    return {
        "today": today,
        "since": active[0] if active else today,
        "cost": tot["totalCost"], "tokens": tot["totalTokens"],
        "input": tot["inputTokens"], "output": tot["outputTokens"],
        "cache": tot["cacheReadTokens"] + tot["cacheCreationTokens"],
        "d_cost": delta("totalCost"), "d_tokens": delta("totalTokens"),
        "d_input": delta("inputTokens"), "d_output": delta("outputTokens"), "d_cache": delta("cache"),
        "active_days": len(active), "d_active": (act_cur - act_prev) / act_prev if act_prev else None,
        "streak": current, "longest": longest,
        "today_cost": day(today, "totalCost"), "today_tokens": day(today, "totalTokens"),
        "d_today_cost": day(today, "totalCost") / avg30_cost - 1 if avg30_cost else None,
        "d_today_tokens": day(today, "totalTokens") / avg30_tok - 1 if avg30_tok else None,
        "best_day": best, "best_cost": days[best].get("totalCost", 0) if best else 0,
        "trend": [(d, day(d, "outputTokens"), day(d, "inputTokens"), day(d, "cache")) for d in trend],
        "heat": {d: days[d].get("totalTokens", 0) for d in active},
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
        "agents": sorted(((k, v) for k, v in agents.items() if v > 0), key=lambda kv: -kv[1]),
    }


def demo_history(today: dt.date) -> dict:
    """Plausible made-up usage for screenshots, so nobody has to publish their own numbers."""
    rnd = random.Random(7)
    agents = [("claude", .68), ("codex", .22), ("opencode", .07), ("gemini", .03)]
    models = [("claude-opus-5-5", .52, 15e-7), ("claude-sonnet-5-5", .21, 6e-7), ("gpt-5.5", .14, 4e-7),
              ("claude-haiku-5-5", .07, 2e-7), ("claude-fable-5-1", .04, 12e-7), ("gemini-3-pro", .02, 3e-7)]
    days = {}
    for i in range(150, -1, -1):
        d = today - dt.timedelta(days=i)
        if rnd.random() < (.25 if d.weekday() >= 5 else .12):
            continue
        total = rnd.lognormvariate(math.log(90e6 * (1 + 2.2 * (150 - i) / 150)), .55)
        if i == 0:
            total *= .55  # today is not over yet
        j = lambda: rnd.uniform(.55, 1.6)  # noqa: E731 - day-to-day wobble so the ratios differ
        parts = {"inputTokens": total * .005 * j(), "outputTokens": total * .006 * j(),
                 "cacheCreationTokens": total * .029 * j()}
        parts["cacheReadTokens"] = total - sum(parts.values())
        price = j()
        mb = [{"modelName": n, "cost": total * share * rate * price, **{k: v * share for k, v in parts.items()}}
              for n, share, rate in models]
        days[d.isoformat()] = {
            "period": d.isoformat(), **{k: round(v) for k, v in parts.items()}, "totalTokens": round(total),
            "totalCost": sum(m["cost"] for m in mb), "modelBreakdowns": mb,
            "agents": [{"agent": a, "totalTokens": round(total * share)} for a, share in agents],
        }
    return {"days": days}


# ---------------------------------------------------------------- formatting

def fmt_tokens(n: float) -> str:
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def fmt_delta(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}%"


def esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------- html

PALETTE = ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]
ICONS = {  # 16px line icons, stroke = currentColor
    "trend": '<path d="M1.5 8.5h3l2-5 3 9 2-4h3"/>',
    "calendar": '<rect x="2" y="3" width="12" height="11" rx="1.5"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3"/>',
    "terminal": '<path d="M2.5 4l4 4-4 4M8 12.5h5.5"/>',
    "chip": '<rect x="4" y="4" width="8" height="8" rx="1"/><path d="M6.5 1.5v2.5M9.5 1.5v2.5M6.5 12v2.5M9.5 12v2.5M1.5 6.5H4M1.5 9.5H4M12 6.5h2.5M12 9.5h2.5"/>',
}

CSS = """
@font-face { font-family: "SFMonoEmbed"; src: url(data:font/ttf;base64,__MONO__) format("truetype"); font-weight: 300 900; }
:root {
  --bg: #f4f4f5; --card: #ffffff; --border: #e4e4e7; --text: #18181b; --muted: #71717a; --faint: #a1a1aa;
  --green: #16a34a; --blue: #2563eb;
  --bar-out: #18181b; --bar-in: #71717a; --bar-cache: #d4d4d8; --grid: #f0f0f2;
  --h0: #f0f0f2; --h1: #e4e4e7; --h2: #c4c4c9; --h3: #a1a1aa; --h4: #71717a; --h5: #3f3f46; --h6: #18181b;
  --ring-track: #f0f0f2;
}
:root.dark {
  --bg: #09090b; --card: #18181b; --border: #27272a; --text: #fafafa; --muted: #a1a1aa; --faint: #71717a;
  --green: #22c55e; --blue: #3b82f6;
  --bar-out: #fafafa; --bar-in: #a1a1aa; --bar-cache: #3f3f46; --grid: #222225;
  --h0: #1f1f22; --h1: #2a2a2e; --h2: #3f3f46; --h3: #52525b; --h4: #8b8b93; --h5: #c4c4c9; --h6: #fafafa;
  --ring-track: #232326;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--text); overflow: hidden;
  font-family: -apple-system, "SF Pro Text", "PingFang SC", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased; }
.mono { font-family: "SFMonoEmbed", Menlo, "PingFang SC", monospace; font-variant-numeric: tabular-nums; }
.stage { position: fixed; display: flex; align-items: center; justify-content: center; }
.dash { width: 1600px; display: grid; gap: 16px; }
.kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; }
.kpi { padding: 22px 26px 24px; display: grid; gap: 14px; }
.kh { display: flex; justify-content: space-between; align-items: baseline; }
.lab { font-size: 17px; color: var(--muted); letter-spacing: .02em; }
.dl { font-size: 13px; color: var(--faint); }
.val { font-size: 34px; font-weight: 600; letter-spacing: -.01em; line-height: 1; white-space: nowrap; }
.val.green { color: var(--green); } .val.blue { color: var(--blue); } .val.dim { color: var(--faint); }
.val small { font-size: 18px; color: var(--muted); font-weight: 500; margin-left: 6px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.panel { padding: 22px 26px; display: grid; grid-template-rows: auto 1fr; gap: 14px; min-height: 0; }
.ph { display: flex; align-items: center; gap: 10px; }
.ph svg.ic { width: 18px; height: 18px; fill: none; stroke: var(--muted); stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
.ph h3 { margin: 0; font-size: 18px; font-weight: 600; color: var(--text); opacity: .86; }
.ph .sp { flex: 1; }
.legend { display: flex; gap: 16px; font-size: 14px; color: var(--muted); align-items: center; }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.cap { font-size: 14px; color: var(--faint); }
.legend + .cap { margin-left: 22px; }
svg.chart { width: 100%; height: 100%; display: block; overflow: visible; }
svg text { fill: var(--faint); font-size: 13px; }
.donut { display: grid; grid-template-columns: 190px 1fr; gap: 28px; align-items: center; }
.items { display: grid; gap: 10px; font-size: 15px; }
.item { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 64px; gap: 12px; align-items: center; }
.item i { width: 10px; height: 10px; border-radius: 50%; }
.item .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .v { text-align: right; }
.item .p { text-align: right; color: var(--faint); }
.foot { display: flex; justify-content: flex-end; gap: 18px; font-size: 13px; color: var(--faint); padding-right: 4px; }
.foot .stale { color: #f59e0b; }
"""


def kpi(label, value, delta=None, cls="", unit=""):
    unit_html = f"<small>{esc(unit)}</small>" if unit else ""
    return (f'<div class="card kpi"><div class="kh"><span class="lab">{esc(label)}</span>'
            f'<span class="dl mono">{esc(delta or "")}</span></div>'
            f'<div class="val mono {cls}">{esc(value)}{unit_html}</div></div>')


def panel_head(icon, title, right=""):
    return (f'<div class="ph"><svg class="ic" viewBox="0 0 16 16">{ICONS[icon]}</svg>'
            f'<h3>{esc(title)}</h3><span class="sp"></span>{right}</div>')


def caption(text):
    # plain text, not a control: the wallpaper must not look clickable
    return f'<span class="cap">{esc(text)}</span>'


def nice_max(v):
    if v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for f in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if f * mag >= v:
            return f * mag
    return 10 * mag


def trend_svg(trend):
    w, h, left, bottom, top = 700, 236, 46, 30, 8
    peak = nice_max(max((o + i + c for _, o, i, c in trend), default=0))
    n = len(trend)
    slot = (w - left) / n
    bw = slot * 0.74
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" preserveAspectRatio="none">']
    for frac in (0, .5, 1):
        y = top + (h - bottom - top) * (1 - frac)
        out.append(f'<line x1="{left}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" style="stroke:var(--grid)" stroke-width="1"/>')
        out.append(f'<text class="mono" x="0" y="{y + 4:.1f}">{fmt_tokens(peak * frac) if frac else "0"}</text>')
    for k, (d, o, i, c) in enumerate(trend):
        x = left + k * slot + (slot - bw) / 2
        y = h - bottom
        for val, color in ((c, "--bar-cache"), (i, "--bar-in"), (o, "--bar-out")):
            hh = (h - bottom - top) * val / peak
            if hh > 0:
                hh = max(hh, 1.5)
                y -= hh
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{hh:.1f}" style="fill:var({color})"/>')
        if k % 4 == 1 or k == n - 1:
            out.append(f'<text class="mono" x="{x + bw / 2:.1f}" y="{h - 6}" text-anchor="middle">{d.month}/{d.day}</text>')
    out.append("</svg>")
    return "".join(out)


def heat_svg(heat, today):
    cell, gap, left, top = 19, 6, 52, 6
    end = today + dt.timedelta(days=(5 - today.weekday()) % 7)  # Saturday of this week
    start = end - dt.timedelta(days=HEAT_WEEKS * 7 - 1)  # a Sunday
    vals = sorted(v for d, v in heat.items() if start <= d <= today)
    cuts = [vals[int(len(vals) * f)] for f in (.2, .4, .6, .8, .95)] if vals else []
    w = left + HEAT_WEEKS * (cell + gap)
    h = top + 7 * (cell + gap) + 54
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}">']
    for r, lab in enumerate(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]):
        out.append(f'<text x="0" y="{top + r * (cell + gap) + 14}">{lab}</text>')
    last_month = None
    for i in range(HEAT_WEEKS * 7):
        d = start + dt.timedelta(days=i)
        c, r = divmod(i, 7)
        x, y = left + c * (cell + gap), top + r * (cell + gap)
        if r == 0 and d.month != last_month and c < HEAT_WEEKS - 1:
            last_month = d.month
            out.append(f'<text x="{x}" y="{top + 7 * (cell + gap) + 16}">{d:%b}</text>')
        if d > today:
            continue
        v = heat.get(d, 0)
        lvl = 0 if v <= 0 else 1 + sum(v > t for t in cuts)
        out.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="4" style="fill:var(--h{lvl})"/>')
    ly = h - 14
    lx = w - 7 * (14 + 5) - 40
    out.append(f'<text x="{lx - 8}" y="{ly + 11}" text-anchor="end">Less</text>')
    for k in range(7):
        out.append(f'<rect x="{lx + k * 19}" y="{ly}" width="14" height="14" rx="4" style="fill:var(--h{k})"/>')
    out.append(f'<text x="{lx + 7 * 19 + 4}" y="{ly + 11}">More</text>')
    out.append("</svg>")
    return "".join(out)


def donut(items, limit=6):
    total = sum(v for _, v in items) or 1
    top = list(items[:limit])
    rest = sum(v for _, v in items[limit:])
    if rest > 0:
        top.append(("Other", rest))
    r, sw, c = 72, 22, 2 * math.pi * 72
    segs, acc = [], 0.0
    for k, (_, v) in enumerate(top):
        frac = v / total
        color = PALETTE[k] if k < len(PALETTE) and top[k][0] != "Other" else "var(--faint)"
        length = max(frac * c - 1.5, 0.8)
        segs.append(f'<circle cx="95" cy="95" r="{r}" fill="none" style="stroke:{color}" stroke-width="{sw}" '
                    f'stroke-dasharray="{length:.2f} {c:.2f}" stroke-dashoffset="{-acc * c:.2f}" transform="rotate(-90 95 95)"/>')
        acc += frac
    ring = (f'<svg viewBox="0 0 190 190" width="190" height="190">'
            f'<circle cx="95" cy="95" r="{r}" fill="none" style="stroke:var(--ring-track)" stroke-width="{sw}"/>'
            + "".join(segs)
            + f'<text x="95" y="88" text-anchor="middle" style="fill:var(--muted);font-size:15px">Tokens</text>'
            f'<text class="mono" x="95" y="114" text-anchor="middle" style="fill:var(--text);font-size:21px;font-weight:600">{fmt_tokens(total)}</text></svg>')
    rows = []
    for k, (name, v) in enumerate(top):
        color = PALETTE[k] if k < len(PALETTE) and name != "Other" else "var(--faint)"
        rows.append(f'<div class="item mono"><i style="background:{color}"></i><span class="n">{esc(name)}</span>'
                    f'<span class="v">{v:,.0f}</span><span class="p">{v / total * 100:.1f}%</span></div>')
    return f'<div class="donut">{ring}<div class="items">{"".join(rows)}</div></div>'


def build_html(m: dict, dark: bool, w_pt: int, h_pt: int, insets, stale: bool) -> str:
    l, t, r, b = insets
    zoom = min((w_pt - l - r) * 0.88 / DESIGN_W, (h_pt - t - b) * 0.94 / DESIGN_H)
    best = m["best_day"]
    kpis = [
        kpi("Est. Cost", f"${m['cost']:,.2f}", fmt_delta(m["d_cost"]), "green"),
        kpi("Total Tokens", fmt_tokens(m["tokens"]), fmt_delta(m["d_tokens"])),
        kpi("Input Tokens", fmt_tokens(m["input"]), fmt_delta(m["d_input"])),
        kpi("Output Tokens", fmt_tokens(m["output"]), fmt_delta(m["d_output"])),
        kpi("Cache Tokens", fmt_tokens(m["cache"]), fmt_delta(m["d_cache"]), "dim"),
        kpi("Active Days", f"{m['active_days']}", fmt_delta(m["d_active"]), "blue", "days"),
        kpi("Streak", f"{m['streak']}", f"best {m['longest']}d", unit="days"),
        kpi("Today's Cost", f"${m['today_cost']:,.2f}", fmt_delta(m["d_today_cost"]) + " vs avg"),
        kpi("Today's Tokens", fmt_tokens(m["today_tokens"]), fmt_delta(m["d_today_tokens"]) + " vs avg"),
        kpi("Best Day", f"${m['best_cost']:,.2f}", f"{best:%b %-d}" if best else ""),
    ]
    legend = ('<div class="legend"><span><i style="background:var(--bar-out)"></i>Output</span>'
              '<span><i style="background:var(--bar-in)"></i>Input</span>'
              '<span><i style="background:var(--bar-cache)"></i>Cache</span></div>')
    now = dt.datetime.now()
    foot = [f'<span>Updated {now:%b %-d, %H:%M}</span>',
            f'<span>Since {m["since"]:%b %-d, %Y} · change = last {WINDOW} days vs previous {WINDOW} · local ccusage</span>']
    if stale:
        foot.insert(0, '<span class="stale">not refreshed</span>')
    body = f"""
<div class="stage" style="left:{l}px;top:{t}px;right:{r}px;bottom:{b}px">
 <div class="dash" style="zoom:{zoom:.4f}">
  <div class="kpis">{''.join(kpis)}</div>
  <div class="row" style="height:330px">
   <div class="card panel">{panel_head("trend", "Daily Trend", legend + caption(f"Last {WINDOW} days"))}{trend_svg(m["trend"])}</div>
   <div class="card panel">{panel_head("calendar", "Daily Activity", caption(f"Last {HEAT_WEEKS} weeks · tokens per day"))}{heat_svg(m["heat"], m["today"])}</div>
  </div>
  <div class="row">
   <div class="card panel">{panel_head("terminal", "Tools", caption("By tokens"))}{donut(m["agents"])}</div>
   <div class="card panel">{panel_head("chip", "Models", caption("By tokens"))}{donut(m["models"])}</div>
  </div>
  <div class="foot mono">{''.join(foot)}</div>
 </div>
</div>"""
    mono = base64.b64encode(MONO_FONT.read_bytes()).decode() if MONO_FONT.exists() else ""
    css = CSS.replace("__MONO__", mono)
    return (f'<!doctype html><html class="{"dark" if dark else ""}"><head><meta charset="utf-8">'
            f'<style>{css}</style></head><body>{body}</body></html>')


# ---------------------------------------------------------------- render

def safe_insets() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) in points kept clear for the menu bar and the Dock."""
    left = right = bottom = 16
    top = 40
    try:
        side = subprocess.run(["defaults", "read", "com.apple.dock", "orientation"],
                              capture_output=True, text=True).stdout.strip() or "bottom"
        tile = int(subprocess.run(["defaults", "read", "com.apple.dock", "tilesize"],
                                  capture_output=True, text=True).stdout.strip() or 64)
    except ValueError:
        side, tile = "bottom", 64
    dock = tile + 24
    if side == "left":
        left += dock
    elif side == "right":
        right += dock
    else:
        bottom += dock
    return left, top, right, bottom


def render_png(m: dict, w: int, h: int, scale: float, dark: bool, out: Path, stale: bool = False) -> None:
    """Lay the dashboard out in points and let headless Chrome rasterize it at the screen's pixel size."""
    w_pt, h_pt = round(w / scale), round(h / scale)
    page = build_html(m, dark, w_pt, h_pt, safe_insets(), stale)
    with tempfile.TemporaryDirectory(dir=DATA) as tmp:
        src = Path(tmp) / "wall.html"
        src.write_text(page)
        r = subprocess.run([
            str(find_browser()), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={DATA / 'chrome-profile'}",
            f"--force-device-scale-factor={scale}", f"--window-size={w_pt},{h_pt}",
            "--virtual-time-budget=1500", f"--screenshot={out}", src.as_uri(),
        ], capture_output=True, text=True, timeout=120)
    if not out.exists():
        raise RuntimeError(f"chrome produced no image: {r.stderr.strip()[-300:]}")


# ---------------------------------------------------------------- apply

JXA_SCREENS = """
ObjC.import('AppKit');
var s = $.NSScreen.screens, out = [];
for (var i = 0; i < s.count; i++) {
  var sc = s.objectAtIndex(i), f = sc.frame, k = sc.backingScaleFactor;
  out.push({w: Math.round(f.size.width * k), h: Math.round(f.size.height * k), k: k});
}
JSON.stringify(out);
"""

JXA_SET = """
ObjC.import('AppKit');
function run(argv) {
  var ws = $.NSWorkspace.sharedWorkspace, s = $.NSScreen.screens;
  for (var i = 0; i < s.count && i < argv.length; i++) {
    var url = $.NSURL.fileURLWithPath(argv[i]);
    ws.setDesktopImageURLForScreenOptionsError(url, s.objectAtIndex(i), $.NSDictionary.dictionary, null);
  }
  return 'ok';
}
"""


def screens() -> list[dict]:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA_SCREENS],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def is_dark() -> bool:
    r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return r.stdout.strip() == "Dark"


def update() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another update is running, skip")
            return
        history, fresh = collect()
        if not history["days"]:
            log("no usage data yet, nothing to draw")
            return
        m = compute(history)
        dark = is_dark()
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        paths = []
        for i, s in enumerate(screens()):
            p = DATA / f"wall-{ts}-{i}.png"
            render_png(m, s["w"], s["h"], s["k"], dark, p, stale=not fresh)
            paths.append(str(p))
        subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA_SET, *paths], check=True,
                       capture_output=True)
        # a new file name every time makes macOS reload; keep only this set
        for old in DATA.glob("wall-*.png"):
            if str(old) not in paths:
                old.unlink()
        log(f"wallpaper set on {len(paths)} screen(s), dark={dark}")


# ---------------------------------------------------------------- launchd

def install(interval: int) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string><string>{ROOT / 'tokenwall.py'}</string><string>update</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{search_path()}</string></dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict>
</plist>
"""
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    PLIST.write_text(plist)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    print(f"installed {PLIST} (every {interval}s); log: {LOG}")


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print(f"removed {PLIST}; history kept in {DATA}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update")
    sub.add_parser("collect")
    r = sub.add_parser("render")
    r.add_argument("--size", default="5120x2880")
    r.add_argument("--scale", type=float, default=2.0, help="backing scale factor of the screen")
    r.add_argument("--out", required=True)
    r.add_argument("--dark", action="store_true")
    r.add_argument("--history", help="history.json or a ccusage report (default: saved history)")
    r.add_argument("--today", help="pretend today is YYYY-MM-DD")
    r.add_argument("--demo", action="store_true", help="draw made-up data (for screenshots)")
    im = sub.add_parser("import")
    im.add_argument("report", help="output of `ccusage daily --json`, e.g. the cc.json viberank-cli writes")
    i = sub.add_parser("install")
    i.add_argument("--interval", type=int, default=1800)
    sub.add_parser("uninstall")
    a = ap.parse_args()

    if a.cmd == "update":
        update()
    elif a.cmd == "collect":
        collect()
    elif a.cmd == "render":
        today = dt.date.fromisoformat(a.today) if a.today else None
        if a.demo:
            history = demo_history(today or dt.date.today())
        elif a.history:
            raw = json.loads(Path(a.history).read_text())
            history = raw if "days" in raw else {"days": {}}
            if "daily" in raw:
                merge(history, raw)
        else:
            history = load_history()
        w, h = map(int, a.size.split("x"))
        DATA.mkdir(parents=True, exist_ok=True)
        out = Path(a.out).resolve()
        out.unlink(missing_ok=True)
        render_png(compute(history, today), w, h, a.scale, a.dark, out)
        print(out)
    elif a.cmd == "import":
        history = load_history()
        n = merge(history, json.loads(Path(a.report).read_text()))
        save_history(history)
        print(f"merged {n} days; {len(history['days'])} days in {HISTORY}")
    elif a.cmd == "install":
        install(a.interval)
    elif a.cmd == "uninstall":
        uninstall()


if __name__ == "__main__":
    main()
