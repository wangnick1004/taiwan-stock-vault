#!/usr/bin/env python3
"""異常訊號篩選：把每天大量的個股新聞與財務數字，篩成少數值得追的訊號。

讀 repo 內已抓到的資料（不連網），輸出：
- 新聞/訊號/YYYY-MM-DD 異常訊號.md：當日 Top N 候選（含理由與證據連結）
- 新聞/訊號/題材雷達.md：近 14 天各題材出現的公司數
- _data/訊號/YYYY-MM-DD.json：同內容的機器可讀版，週報彙整用
- _data/訊號/財務歷史.json：每次執行從財務摘要存一筆，用來判斷轉折
- _data/訊號/已報.json：已報過的訊號與首次日期，同一訊號不重報

用法：
    python scripts/訊號篩選/screen.py --vault . [--date 2026-10-23] [--dry-run]
    python scripts/訊號篩選/screen.py --vault . --week 2026-10-24   # 週報素材，印到 stdout
參數與關鍵字在 _data/訊號規則.json，架構說明在 scripts/訊號篩選/篩選架構說明.md。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
RULES = "_data/訊號規則.json"
SUMMARY = "_data/財務摘要.json"
RAW_REVENUE = ["_data/公司資料/上市_每月營收.json", "_data/公司資料/上櫃_每月營收.json"]
LIST_0050 = "_data/0050成分股.txt"
HISTORY = "_data/訊號/財務歷史.json"
REV_COLS = ["月營收", "年增率", "月增率", "累計年增率"]
QTR_COLS = ["毛利率", "營業利益率", "淨利率", "累計EPS"]
LEDGER = "_data/訊號/已報.json"
OUT_JSON_DIR = "_data/訊號"
OUT_DIR = "新聞/訊號"
NEWS_DIR = "新聞"
NOTE_DIR = "個股/台股"
ITEM_RE = re.compile(r"^- (?:(\d\d-\d\d) )?(\S+) (\S+)(?: (\d\d:\d\d))?｜(.+)｜\[(.+?)\]\((.+?)\)\s*$")


def write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, p)


def write_if_changed(p: Path, text: str) -> bool:
    """內容（忽略「產生時間」那行）沒變就不寫，避免每次排程都產生 commit。"""
    strip = lambda t: re.sub(r"^產生時間: .*$|\"產生時間\": \"[^\"]*\"", "", t, flags=re.M)  # noqa: E731
    if p.exists() and strip(p.read_text("utf-8")) == strip(text):
        return False
    write_text(p, text)
    return True


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def norm_title(s: str) -> str:
    return re.sub(r"[\s「」『』\"'“”‘’［］\[\]｜|：:，,。.!！?？\-–—]", "", s or "")


def num(v):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "-", "--") else None
    except ValueError:
        return None


# ---------- 資料 ----------

@dataclass
class News:
    code: str
    at: dt.datetime
    kind: str  # 重訊 / 媒體
    title: str
    source: str
    url: str

    @property
    def key(self) -> str:
        return norm_title(self.title)


@dataclass
class Signal:
    code: str
    kind: str        # 營收 / 季報 / 重訊 / 媒體 / 新聞量
    label: str       # 例：營收年增轉正
    points: float    # 強度
    direction: int   # +1 偏多、-1 偏空、0 中性
    reason: str
    evidence: list[str] = field(default_factory=list)
    key: str = ""    # 去重用，同一 key 只在第一次出現那天報


def load_notes(vault: Path) -> dict[str, dict]:
    """代號 → {title, 產業}。"""
    out = {}
    for p in sorted((vault / NOTE_DIR).glob("*.md")):
        m = re.match(r"^(\d{4,6}[A-Z]?)(?=\D)", p.stem)
        if not m:
            continue
        head = p.read_text("utf-8")[:3000]
        ind = re.search(r"^產業:\s*(.+)$", head, re.M)
        out[m.group(1)] = {"title": p.stem, "name": p.stem[len(m.group(1)):],
                           "產業": ind.group(1).strip() if ind else "未分類"}
    return out


def load_0050(vault: Path) -> set[str]:
    f = vault / LIST_0050
    if not f.exists():
        return set()
    return {re.match(r"\d{4,6}", ln).group(0) for ln in f.read_text("utf-8").splitlines()
            if re.match(r"\d{4,6}", ln.strip())}


def load_news(vault: Path, notes: dict[str, dict]) -> list[News]:
    items = []
    for p in (vault / NEWS_DIR).glob("* 新聞.md"):
        m = re.match(r"^(\d{4,6}[A-Z]?)", p.name)
        if not m or m.group(1) not in notes:
            continue
        code, day, seen = m.group(1), None, set()
        for ln in p.read_text("utf-8").splitlines():
            if ln.startswith("## "):
                day = ln[3:].strip()
                continue
            mm = ITEM_RE.match(ln)
            if not (mm and day):
                continue
            try:
                at = dt.datetime.strptime(f"{day} {mm.group(4) or '00:00'}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            kind = "重訊" if "重訊" in mm.group(3) or "重訊" in mm.group(2) else "媒體"
            it = News(code, at, kind, mm.group(5).strip(), mm.group(6), mm.group(7))
            if (it.key, day) in seen:  # 新聞檔裡同一則重複寫入時只算一次
                continue
            seen.add((it.key, day))
            items.append(it)
    return items


# ---------- 財務歷史 ----------

def update_history(vault: Path, hist: dict) -> dict:
    """把財務摘要（與 _data/公司資料 裡較舊的月營收原始檔）併進歷史，同一期間以新值為準。"""
    rev, qtr = hist.setdefault("月營收", {}), hist.setdefault("季報", {})
    for f in RAW_REVENUE:  # 只補歷史裡沒有的月份
        for r in read_json(vault / f, []):
            ym = re.sub(r"\D", "", str(r.get("資料年月", "")))
            code = str(r.get("公司代號", "")).strip()
            if len(ym) < 5 or not code:
                continue
            key = f"{int(ym[:-2]) + 1911}-{ym[-2:]}"
            cur = num(r.get("營業收入-當月營收"))
            rev.setdefault(code, {}).setdefault(key, [
                round(cur / 1e5, 2) if cur is not None else None,
                _r(num(r.get("營業收入-去年同月增減(%)"))),
                _r(num(r.get("營業收入-上月比較增減(%)"))),
                _r(num(r.get("累計營業收入-前期比較增減(%)")))])
    for code, v in read_json(vault / SUMMARY, {}).get("公司", {}).items():
        if v.get("月營收年月"):
            rev.setdefault(code, {})[v["月營收年月"]] = [
                v.get("月營收"), v.get("月營收年增率"), v.get("月營收月增率"), v.get("累計營收年增率")]
        if v.get("財報季度"):
            qtr.setdefault(code, {})[v["財報季度"]] = [
                v.get("毛利率"), v.get("營業利益率"), v.get("淨利率"), v.get("累計EPS")]
    hist["欄位"] = {"月營收": REV_COLS, "季報": QTR_COLS}
    return hist


def expand(series: dict, cols: list[str]) -> dict:
    """歷史檔用陣列存（省空間），讀進來時轉回欄位名稱。"""
    return {k: dict(zip(cols, v)) for k, v in series.items()}


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - (m == 1)}-{12 if m == 1 else m - 1:02d}"


def prev_quarter(q: str) -> str:
    y, n = int(q[:4]), int(q[-1])
    return f"{y - 1}Q4" if n == 1 else f"{y}Q{n - 1}"


def single_eps(series: dict, q: str):
    """累計 EPS 換算單季：Q1 就是累計值，其餘減前一季累計。"""
    cur = (series.get(q) or {}).get("累計EPS")
    if cur is None:
        return None
    if q.endswith("Q1"):
        return cur
    prev = (series.get(prev_quarter(q)) or {}).get("累計EPS")
    return None if prev is None else round(cur - prev, 2)


# ---------- 訊號規則 ----------

def revenue_signals(code: str, series: dict, R: dict, link: str) -> list[Signal]:
    if not series:
        return []
    ym = max(series)
    cur, prev = series[ym], series.get(prev_month(ym)) or {}
    yoy, rev = cur.get("年增率"), cur.get("月營收")
    if yoy is None or rev is None:
        return []
    py = prev.get("年增率")
    base = rev / (1 + yoy / 100) if yoy > -100 else None  # 去年同月營收（億）
    low_base = base is None or base < R["去年同月最低億"] or yoy > R["年增上限"]
    tiny = rev < R["最低月營收億"]
    scale = 0.5 if tiny else 1.0
    ev = [f"{ym} 月營收 {rev:g} 億，年增 {yoy:g}%"
          + (f"（前月 {py:g}%）" if py is not None else "") + f"｜{link}"]
    out = []

    def add(label, pts, d, reason):
        out.append(Signal(code, "營收", label, round(pts * scale, 2), d, reason, ev, f"{code}|{label}|{ym}"))
        return label

    if low_base:
        return []  # 基期過低的暴增（例：年增數十萬 %）不列入
    trend = None
    if py is not None:
        if py < 0 and yoy >= R["轉正門檻"]:
            trend = add("營收年增轉正", 3 + min(yoy, 50) / 25, 1, f"{ym} 營收年增由 {py:g}% 轉為 {yoy:g}%")
        elif py > 0 and yoy <= R["轉負門檻"]:
            trend = add("營收年增轉負", 3 + min(-yoy, 50) / 25, -1, f"{ym} 營收年增由 {py:g}% 轉為 {yoy:g}%")
        elif yoy - py >= R["加速百分點"] and yoy >= R["加速最低年增"]:
            trend = add("營收成長加速", 2 + min(yoy - py, 60) / 20, 1, f"{ym} 營收年增 {yoy:g}%，比前月加快 {yoy - py:.1f} 個百分點")
        elif py - yoy >= R["減速百分點"] and py > 0 and yoy < R["高成長年增"]:
            # 年增仍在高成長區（例如 120% → 90%）不算減速，只抓成長明顯降溫
            trend = add("營收成長減速", 2 + min(py - yoy, 60) / 20, -1, f"{ym} 營收年增 {yoy:g}%，比前月放慢 {py - yoy:.1f} 個百分點")
    if yoy >= R["高成長年增"] and not tiny and trend is None:
        add("營收高成長", 1.5 + min(yoy, 200) / 100, 1, f"{ym} 營收年增 {yoy:g}%" + (f"（前月 {py:g}%）" if py is not None else ""))
    cy, pcy = cur.get("累計年增率"), prev.get("累計年增率")
    if cy is not None and pcy is not None:
        if pcy < 0 <= cy:
            add("累計營收轉正", 2, 1, f"今年累計營收年增由 {pcy:g}% 轉為 {cy:g}%")
        elif pcy > 0 >= cy:
            add("累計營收轉負", 2, -1, f"今年累計營收年增由 {pcy:g}% 轉為 {cy:g}%")
    months = sorted(series)
    if len(months) >= R["創新高最少月數"]:
        past = [series[m].get("月營收") for m in months[:-1] if series[m].get("月營收") is not None]
        if past and rev > max(past):
            add("月營收創新高", 3, 1, f"{ym} 月營收 {rev:g} 億，高於歷史紀錄內的前高 {max(past):g} 億（共 {len(months)} 個月）")
    return out


def quarter_signals(code: str, series: dict, Q: dict, link: str) -> list[Signal]:
    if not series:
        return []
    q = max(series)
    cur, prev = series[q], series.get(prev_quarter(q))
    if not prev:
        return []  # 要有前一季才比得出轉折
    out = []
    same_year = q[:4] == prev_quarter(q)[:4]

    def add(label, pts, d, reason, ev):
        out.append(Signal(code, "季報", label, pts, d, reason, [ev + f"｜{link}"], f"{code}|{label}|{q}"))

    # 毛利率、營益率是年度累計值；同一年度內累計值上升代表最新一季比前幾季好
    for k, th, name in (("毛利率", Q["毛利率變動百分點"], "毛利率"), ("營業利益率", Q["營益率變動百分點"], "營益率")):
        a, b = cur.get(k), prev.get(k)
        if a is None or b is None or not same_year:
            continue
        if a - b >= th:
            add(f"{name}跳升", 3, 1, f"{q} 累計{name} {a:g}%，比 {prev_quarter(q)} 的 {b:g}% 提高 {a - b:.1f} 個百分點",
                f"{name} {b:g}% → {a:g}%")
        elif b - a >= th:
            add(f"{name}下滑", 3, -1, f"{q} 累計{name} {a:g}%，比 {prev_quarter(q)} 的 {b:g}% 下降 {b - a:.1f} 個百分點",
                f"{name} {b:g}% → {a:g}%")
    e1, e0 = single_eps(series, q), single_eps(series, prev_quarter(q))
    if e1 is not None and e0 is not None:
        if e0 < 0 <= e1:
            add("單季轉虧為盈", 4, 1, f"{q} 單季 EPS {e1:g} 元，前一季 {e0:g} 元", f"單季 EPS {e0:g} → {e1:g}")
        elif e0 > 0 > e1:
            add("單季轉盈為虧", 4, -1, f"{q} 單季 EPS {e1:g} 元，前一季 {e0:g} 元", f"單季 EPS {e0:g} → {e1:g}")
    return out


def compile_rules(cfg: dict) -> dict:
    return {"noise": [re.compile(p) for p in cfg["媒體雜訊樣式"]],
            "echo": [re.compile(p) for p in cfg["媒體回聲樣式"]]}


def direction_of(title: str, cfg: dict) -> int:
    neg = any(k in title for k in cfg["偏空關鍵字"])
    pos = any(k in title for k in cfg["偏多關鍵字"])
    return 0 if neg == pos else (1 if pos else -1)


def material_tier(title: str, cfg: dict):
    """重訊分級：回傳 (級別, 分數, 命中關鍵字)；命中雜訊樣式或都沒命中回傳 None。
    雜訊清單只放例行公告的固定用語（背書保證、受邀參加、退還股款……），所以先判雜訊再分級。"""
    tiers = cfg["重訊分級"]
    if any(k in title for k in tiers["雜訊"]):
        return None
    for t in ("A", "B", "C"):
        k = next((k for k in tiers[t]["關鍵字"] if k in title), None)
        if k:
            return t, tiers[t]["分數"], k
    return None


def news_link(n: News) -> str:
    title = n.title.replace("[", "［").replace("]", "］")
    short = title if len(title) <= 60 else title[:58] + "…"
    return f"{n.at:%m-%d %H:%M} [{short}]({n.url})（{n.source}）"


def classify_news(items: list[News], notes: dict, cfg: dict, rx: dict) -> tuple[dict, Counter]:
    """把新聞分成：可計分、彙整文（只算題材熱度）、雜訊。"""
    shared = Counter(n.key for n in items)  # 同一標題出現在幾家公司的新聞檔
    tag, stats = {}, Counter()
    for n in items:
        if n.kind == "重訊":  # 例行公告（背書保證、受邀論壇…）當雜訊，不算新聞量
            tag[id(n)] = "score" if material_tier(n.title, cfg) else "noise"
            stats[tag[id(n)]] += 1
            continue
        codes_in_title = len(set(re.findall(r"(?<!\d)\d{4}(?!\d)", n.title)))
        names_in_title = n.title.count("、") + 1
        if any(p.search(n.title) for p in rx["noise"]):
            tag[id(n)] = "noise"
        elif any(p.search(n.title) for p in rx["echo"]):
            tag[id(n)] = "echo"  # 營收速報之類，財務訊號已涵蓋，只算新聞量
        elif shared[n.key] >= cfg["彙整文公司數"] or codes_in_title >= 3 or names_in_title >= 4:
            tag[id(n)] = "roundup"
        else:
            tag[id(n)] = "score"
        stats[tag[id(n)]] += 1
    return tag, stats


def news_signals(code: str, win: list[News], tag: dict, cfg: dict, title: str) -> list[Signal]:
    """同一家公司、同一級別與關鍵字的新聞合併成一個訊號（多家媒體報同一件事只加一點分）。"""
    groups: dict[tuple, list] = defaultdict(list)
    for n in sorted(win, key=lambda x: x.at, reverse=True):
        if tag[id(n)] != "score":
            continue
        if n.kind == "重訊":
            t = material_tier(n.title, cfg)
            if t:
                groups[("重訊", f"重訊 {t[0]} 級：{t[2]}", t[1])].append(n)
        else:
            kw = next((k for k in cfg["媒體加分"]["關鍵字"] if k in n.title), None)
            if kw:
                groups[("媒體", f"媒體：{kw}", cfg["媒體加分"]["分數"])].append(n)
    out = []
    for (kind, label, pts), ns in groups.items():
        d = direction_of(" ".join(n.title for n in ns), cfg)
        reason = ns[0].title + (f"（另有 {len(ns) - 1} 則相關）" if len(ns) > 1 else "")
        ev = [news_link(n) for n in ns[:3]] + [f"[[{title} 新聞]]"]
        out.append(Signal(code, kind, label, round(pts + min(len(ns) - 1, 2) * 0.5, 2), d, reason, ev,
                          f"{code}|{kind}|" + "+".join(sorted(n.key for n in ns))[:200]))
    return out


def volume_signal(code: str, win: list[News], hist_items: list[News], tag: dict, cfg: dict,
                  day: dt.date, title: str) -> Signal | None:
    V = cfg["新聞量"]
    usable = [n for n in win if tag[id(n)] in ("score", "echo")]
    cnt = len({n.key for n in usable})
    if cnt < V["最少則數"]:
        return None
    start = day - dt.timedelta(days=V["基準天數"])
    base = len({(n.key, n.at.date()) for n in hist_items
                if start <= n.at.date() < day - dt.timedelta(days=1) and tag[id(n)] in ("score", "echo")})
    avg = base / V["基準天數"]
    ratio = cnt / max(avg, 0.5)
    if ratio < V["倍數"]:
        return None
    return Signal(code, "新聞量", "新聞量暴增", round(2 + min(math.log2(ratio), 3), 2), 0,
                  f"本期 {cnt} 則，前 {V['基準天數']} 天平均每天 {avg:.1f} 則（{ratio:.1f} 倍）",
                  [f"[[{title} 新聞]]"], f"{code}|新聞量|{day}")


def theme_hits(n: News, themes: dict) -> set[str]:
    t = n.title.upper()
    return {name for name, kws in themes.items() if any(k.upper() in t for k in kws)}


def theme_radar(items: list[News], tag: dict, cfg: dict, win_ids: set, day: dt.date) -> tuple[list[dict], dict]:
    """題材熱度：本期出現該題材的公司數，對照前 N 天每天的平均公司數。"""
    T = cfg["題材"]
    themes = T["清單"]
    per_day: dict[str, dict[dt.date, set]] = defaultdict(lambda: defaultdict(set))
    now: dict[str, set] = defaultdict(set)
    samples: dict[str, list[News]] = defaultdict(list)
    for n in items:
        if tag[id(n)] == "noise":
            continue
        for th in theme_hits(n, themes):
            per_day[th][n.at.date()].add(n.code)
            if id(n) in win_ids:
                now[th].add(n.code)
                samples[th].append(n)
    rows = []
    for th in themes:
        cnt = len(now[th])
        days = [day - dt.timedelta(days=i) for i in range(2, T["基準天數"] + 2)]
        base = sum(len(per_day[th].get(d, ())) for d in days) / T["基準天數"]
        ratio = cnt / max(base, 0.5)
        hot = cnt >= T["最少公司數"] and ratio >= T["相對基準倍數"]
        if cnt:
            rows.append({"題材": th, "公司數": cnt, "基準": round(base, 2), "倍數": round(ratio, 2), "熱": hot,
                         "公司": sorted(now[th]),
                         "代表新聞": [news_link(x) for x in sorted(samples[th], key=lambda x: x.at, reverse=True)[:3]]})
    rows.sort(key=lambda r: (-r["熱"], -r["公司數"], -r["倍數"]))
    matrix = {th: {d.isoformat(): len(c) for d, c in per_day[th].items()} for th in themes}
    return rows, matrix


# ---------- 評分 ----------

def size_bonus(code: str, rev: float | None, in0050: bool, S: dict) -> float:
    b = S["0050加分"] if in0050 else 0.0
    if rev is not None:
        for th, pts in S["月營收級距加分"]:
            if rev >= th:
                b += pts
                break
        if rev < S["小型門檻億"]:
            b += S["小型扣分"]
    return b


def score_company(sigs: list[Signal], bonus: float, resonance: float, theme: float) -> float:
    pts = sorted((s.points for s in sigs), reverse=True)
    core = sum(p * (0.5 ** i) for i, p in enumerate(pts))  # 同一家多個訊號遞減加總，避免灌分
    return round(core + bonus + resonance + theme, 2)


def window(day: dt.date, since: str | None) -> dt.datetime:
    if since:
        return dt.datetime.strptime(since, "%Y-%m-%d %H:%M")
    prev = day - dt.timedelta(days=3 if day.weekday() == 0 else 1)
    return dt.datetime.combine(prev, dt.time(13, 30))


def run(vault: Path, day: dt.date, since: str | None, dry: bool) -> dict:
    cfg = read_json(vault / RULES, None)
    if cfg is None:
        raise SystemExit(f"找不到 {RULES}")
    rx = compile_rules(cfg)
    notes = load_notes(vault)
    core = load_0050(vault)
    hist = update_history(vault, read_json(vault / HISTORY, {}))
    ledger = read_json(vault / LEDGER, {})
    today = day.isoformat()

    items = load_news(vault, notes)
    start = window(day, since)
    end = dt.datetime.combine(day, dt.time(23, 59))
    win = [n for n in items if start <= n.at <= end]
    win_ids = {id(n) for n in win}
    tag, stats = classify_news(items, notes, cfg, rx)
    radar, matrix = theme_radar(items, tag, cfg, win_ids, day)
    hot = {r["題材"] for r in radar if r["熱"]}

    by_code: dict[str, list[News]] = defaultdict(list)
    for n in items:
        by_code[n.code].append(n)
    win_by_code: dict[str, list[News]] = defaultdict(list)
    for n in win:
        win_by_code[n.code].append(n)

    candidates = []
    all_fin = []
    for code, meta in notes.items():
        link = f"[[{meta['title']}]]"
        series = expand(hist["月營收"].get(code, {}), REV_COLS)
        sigs = revenue_signals(code, series, cfg["營收"], link)
        sigs += quarter_signals(code, expand(hist["季報"].get(code, {}), QTR_COLS), cfg["季報"], link)
        w = win_by_code.get(code, [])
        sigs += news_signals(code, w, tag, cfg, meta["title"])
        v = volume_signal(code, w, by_code.get(code, []), tag, cfg, day, meta["title"]) if w else None
        if v:
            sigs.append(v)
        fresh = []
        for s in sigs:  # 同一訊號只在第一次出現那天列出
            first = ledger.get(s.key)
            if first is None or first == today:
                fresh.append(s)
                ledger[s.key] = today
        if not fresh:
            continue
        all_fin += [s for s in fresh if s.kind in ("營收", "季報")]
        rev = series[max(series)].get("月營收") if series else None
        kinds = {s.kind for s in fresh}
        resonance = cfg["共振加分"] if kinds & {"營收", "季報"} and kinds & {"重訊", "媒體", "新聞量"} else 0.0
        themes_hit = sorted({th for n in w if tag[id(n)] != "noise" for th in theme_hits(n, cfg["題材"]["清單"])} & hot)
        theme = cfg["題材加分"] if themes_hit else 0.0
        bonus = size_bonus(code, rev, code in core, cfg["規模"])
        score = score_company(fresh, bonus, resonance, theme)
        core_pts = score_company(fresh, 0, 0, 0)
        net = sum(s.points * s.direction for s in fresh)
        dirs = {s.direction for s in fresh if s.direction}
        direction = "多空交錯" if len(dirs) > 1 and abs(net) < 2 else ("偏多" if net > 0 else "偏空" if net < 0 else "中性")
        candidates.append({
            "代號": code, "公司": meta["title"], "產業": meta["產業"], "0050": code in core,
            "分數": score, "訊號分": core_pts, "方向": direction, "規模加分": bonus, "共振加分": resonance, "題材": themes_hit,
            "訊號": [s.__dict__ for s in sorted(fresh, key=lambda s: -s.points)],
        })

    ranked = sorted((c for c in candidates if c["訊號分"] >= cfg["入榜最低分"]),
                    key=lambda c: (-c["分數"], c["代號"]))
    top = ranked[: cfg["TopN"]]

    # 產業共振：同產業有一定比例的公司同時出現營收轉折（轉正、加速為多；轉負、減速為空）
    ind_total = Counter(m["產業"] for m in notes.values())
    ind = defaultdict(lambda: {1: set(), -1: set()})
    for s in all_fin:
        if s.label in ("營收年增轉正", "營收成長加速", "營收年增轉負", "營收成長減速"):
            ind[notes[s.code]["產業"]][s.direction].add(s.code)
    P = cfg["產業共振"]
    industry = []
    for k, v in ind.items():
        for d, codes in v.items():
            share = len(codes) / ind_total[k]
            if len(codes) >= P["最少公司數"] and share >= P["最低比例"]:
                industry.append({"產業": k, "方向": "偏多" if d > 0 else "偏空", "家數": len(codes),
                                 "產業家數": ind_total[k], "公司": sorted(codes)})
    industry.sort(key=lambda x: -x["家數"] / x["產業家數"])

    cutoff = (day - dt.timedelta(days=cfg["已報保留天數"])).isoformat()
    ledger = {k: v for k, v in ledger.items() if v >= cutoff}
    result = {"日期": today, "產生時間": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
              "新聞視窗": f"{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}",
              "財務資料日期": read_json(vault / SUMMARY, {}).get("抓取日期", ""),
              "統計": {"視窗內新聞": len(win), "候選公司": len(candidates), "入榜門檻以上": len(ranked),
                       "雜訊": sum(1 for n in win if tag[id(n)] == "noise"),
                       "彙整文": sum(1 for n in win if tag[id(n)] == "roundup"),
                       "營收回聲": sum(1 for n in win if tag[id(n)] == "echo")},
              "TopN": top, "題材": radar, "產業共振": industry,
              "財務訊號": [s.__dict__ for s in sorted(all_fin, key=lambda s: (-s.points, s.code))]}
    if dry:
        print(render_daily(result, notes))
        return result
    write_if_changed(vault / HISTORY, json.dumps(hist, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    write_if_changed(vault / LEDGER, json.dumps(ledger, ensure_ascii=False, indent=0, sort_keys=True))
    write_if_changed(vault / OUT_JSON_DIR / f"{today}.json", json.dumps(compact(result), ensure_ascii=False, indent=1))
    write_if_changed(vault / OUT_DIR / f"{today} 異常訊號.md", render_daily(result, notes))
    write_if_changed(vault / OUT_DIR / "題材雷達.md", render_radar(matrix, day, cfg))
    print(f"{today}：候選 {len(candidates)} 家，入榜 {len(top)} 家，熱門題材 {len(hot)} 個")
    return result


# ---------- 輸出 ----------

def compact(r: dict) -> dict:
    """存檔用的精簡版：Top N 保留完整證據，其餘只留代號、訊號與理由，給週報彙整。"""
    out = {k: v for k, v in r.items() if k not in ("題材", "財務訊號")}
    out["題材"] = [{k: v for k, v in t.items() if k != "代表新聞"} for t in r["題材"]]
    out["財務訊號"] = [[s["code"], s["label"], s["points"], s["direction"], s["reason"]] for s in r["財務訊號"]]
    return out


def render_daily(r: dict, notes: dict) -> str:
    st = r["統計"]
    out = ["---", f"日期: {r['日期']}", "類型: 異常訊號", f"產生時間: {r['產生時間']}",
           "重點個股: [" + ", ".join(f'"[[{c["公司"]}]]"' for c in r["TopN"][:10]) + "]",
           "tags: [新聞, 訊號]", "---", f"# {r['日期']} 異常訊號", "",
           "> [!info] 自動篩選",
           f"> 新聞視窗 {r['新聞視窗']}，財務資料 {r['財務資料日期'] or '未知'}。"
           f"視窗內 {st['視窗內新聞']} 則新聞，排除雜訊 {st['雜訊']} 則（例行重訊、零股排行等）、彙整文 {st['彙整文']} 則（只算題材熱度）、營收速報 {st['營收回聲']} 則（只算新聞量）。"
           f"有新訊號的公司 {st['候選公司']} 家，達入榜門檻 {st['入榜門檻以上']} 家。",
           "> 這是程式依規則篩出的候選，不是結論；寫成文章前請回原始來源確認。規則與參數見 `_data/訊號規則.json`。", ""]
    out += ["## Top 候選", "", "| # | 公司 | 產業 | 分數 | 方向 | 主要理由 |", "|---|---|---|---|---|---|"]
    for i, c in enumerate(r["TopN"], 1):
        tags = "、".join(dict.fromkeys(s["label"] for s in c["訊號"][:3]))
        star = "⭐" if c["0050"] else ""
        out.append(f"| {i} | [[{c['公司']}]]{star} | {c['產業']} | {c['分數']:g} | {c['方向']} | {tags} |")
    if not r["TopN"]:
        out.append("| | 今天沒有達門檻的訊號 | | | | |")
    out += ["", "⭐ 為 0050 成分股。", "", "## 個股明細", ""]
    for i, c in enumerate(r["TopN"], 1):
        extra = []
        if c["規模加分"]:
            extra.append(f"規模 {c['規模加分']:+g}")
        if c["共振加分"]:
            extra.append(f"新聞與財報共振 +{c['共振加分']:g}")
        if c["題材"]:
            extra.append("熱門題材 " + "、".join(c["題材"]))
        out.append(f"### {i}. [[{c['公司']}]]｜{c['分數']:g} 分｜{c['方向']}")
        if extra:
            out.append(f"加分：{'；'.join(extra)}")
        out.append("")
        for s in c["訊號"]:
            arrow = {1: "🔺", -1: "🔻"}.get(s["direction"], "◽")
            out.append(f"- {arrow} **{s['label']}**（{s['points']:g}）：{s['reason']}")
            for e in s["evidence"]:
                out.append(f"    - {e}")
        out.append("")
    out += ["## 題材雷達（本期）", "", "| 題材 | 公司數 | 前 7 天平均 | 倍數 | 公司 |", "|---|---|---|---|---|"]
    for t in r["題材"][:15]:
        names = "、".join(f"[[{notes[c]['title']}]]" for c in t["公司"][:8]) + ("…" if len(t["公司"]) > 8 else "")
        out.append(f"| {'🔥 ' if t['熱'] else ''}{t['題材']} | {t['公司數']} | {t['基準']:g} | {t['倍數']:g} | {names} |")
    hot = [t for t in r["題材"] if t["熱"]]
    for t in hot[:5]:
        out += ["", f"**{t['題材']}** 代表新聞："] + [f"- {x}" for x in t["代表新聞"]]
    if r["產業共振"]:
        out += ["", "## 產業共振（同產業多家同時出現營收轉折）", ""]
        for x in r["產業共振"]:
            out.append(f"- {x['產業']}｜{x['方向']}｜{x['家數']}/{x['產業家數']} 家：" + "、".join(f"[[{notes[c]['title']}]]" for c in x["公司"][:12])
                       + ("…" if x["家數"] > 12 else ""))
    fin = r["財務訊號"]
    if fin:
        out += ["", f"## 財務訊號總表（{len(fin)} 筆，含未入榜）", "", "| 公司 | 訊號 | 說明 |", "|---|---|---|"]
        for s in fin[:80]:
            out.append(f"| [[{notes[s['code']]['title']}]] | {s['label']} | {s['reason']} |")
        if len(fin) > 80:
            out.append(f"| … | 其餘 {len(fin) - 80} 筆見 `_data/訊號/{r['日期']}.json` | |")
    return "\n".join(out) + "\n"


def render_radar(matrix: dict, day: dt.date, cfg: dict) -> str:
    days = [day - dt.timedelta(days=i) for i in range(13, -1, -1)]
    out = ["---", "類型: 題材雷達", f"更新: {day}", "tags: [新聞, 訊號]", "---", "# 題材雷達",
           "", "近 14 天每天在新聞標題提到該題材的公司數（排除零股排行、三大法人等雜訊）。題材與關鍵字在 `_data/訊號規則.json` 的「題材」。",
           "", "| 題材 | " + " | ".join(f"{d:%m/%d}" for d in days) + " |", "|---|" + "---|" * len(days)]
    rows = sorted(matrix.items(), key=lambda kv: -sum(kv[1].get(d.isoformat(), 0) for d in days[-3:]))
    for th, per in rows:
        vals = [per.get(d.isoformat(), 0) for d in days]
        if any(vals):
            out.append(f"| {th} | " + " | ".join(str(v) if v else "·" for v in vals) + " |")
    return "\n".join(out) + "\n"


def week(vault: Path, sat: dt.date) -> None:
    """週報素材：彙整週一到週五的每日訊號。"""
    mon = sat - dt.timedelta(days=sat.weekday())
    days = [mon + dt.timedelta(days=i) for i in range(5)]
    score: Counter = Counter()
    seen: dict[str, dict] = {}
    themes: dict[str, list] = defaultdict(list)
    for d in days:
        r = read_json(vault / OUT_JSON_DIR / f"{d}.json", None)
        if not r:
            continue
        for c in r["TopN"]:
            score[c["公司"]] += c["分數"]
            seen.setdefault(c["公司"], {"天數": 0, "理由": []})
            seen[c["公司"]]["天數"] += 1
            seen[c["公司"]]["理由"] += [f"{d:%m/%d} {s['label']}：{s['reason']}" for s in c["訊號"][:2]]
        for t in r["題材"]:
            themes[t["題材"]].append((d, t["公司數"], t["熱"]))
    print(f"# {days[0]} ～ {days[-1]} 週訊號彙整\n\n## 本週累計分數前 15 名\n")
    for name, s in score.most_common(15):
        print(f"### [[{name}]]（累計 {s:.1f} 分，入榜 {seen[name]['天數']} 天）")
        for x in seen[name]["理由"][:6]:
            print(f"- {x}")
        print()
    print("## 題材熱度（每天公司數，🔥 為當天熱門）\n")
    for th, vals in sorted(themes.items(), key=lambda kv: -sum(v[1] for v in kv[1])):
        print(f"- {th}：" + "、".join(f"{d:%m/%d} {n}{'🔥' if h else ''}" for d, n, h in vals))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".")
    ap.add_argument("--date", help="訊號日期 YYYY-MM-DD，預設今天（台灣時間）")
    ap.add_argument("--since", help="新聞起算時間 YYYY-MM-DD HH:MM，預設前一交易日 13:30")
    ap.add_argument("--dry-run", action="store_true", help="只印出結果，不寫任何檔案")
    ap.add_argument("--week", help="週報素材：給週六日期，彙整該週一到週五的訊號")
    a = ap.parse_args()
    vault = Path(a.vault).resolve()
    if a.week:
        week(vault, dt.date.fromisoformat(a.week))
        return 0
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ).date()
    run(vault, day, a.since, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
