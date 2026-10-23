#!/usr/bin/env python3
"""異常訊號篩選：把每天大量的個股新聞與財務數字，篩成少數值得追的訊號，寫成專業訊號報告。

讀 repo 內已抓到的資料（`--check-links` 以外不連網），輸出：
- 新聞/訊號/YYYY-MM-DD 異常訊號.md：訊號報告（導覽圖、Top N、明細、資料檢核、文末資料來源）
- 新聞/訊號/題材雷達.md：近 14 天各題材出現的公司數
- _data/訊號/YYYY-MM-DD.json：同內容的機器可讀版，週報彙整用
- _data/訊號/財務歷史.json：每次執行從財務摘要存一筆，用來判斷轉折
- _data/訊號/已報.json：已報過的訊號與首次日期，同一訊號不重報

用法：
    python scripts/訊號篩選/screen.py --vault . [--date 2026-10-23] [--dry-run] [--check-links]
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
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
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
DAILY_DIR = "新聞/每日"
NOTE_DIR = "個股/台股"
ITEM_RE = re.compile(r"^- (?:(\d\d-\d\d) )?(\S+) (\S+)(?: (\d\d:\d\d))?｜(.+)｜\[(.+?)\]\((.+?)\)\s*$")
MOPS_URL = "https://mops.twse.com.tw/mops/#/web/t05st01"
# 財務訊號引用的官方資料集（update_financials.py 抓的就是這幾份）
DATASETS = {
    "月營收_上市": ("證交所 OpenAPI｜上市公司每月營業收入彙總表（t187ap05_L）", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
    "月營收_上櫃": ("櫃買中心 OpenAPI｜上櫃公司每月營業收入彙總表（mopsfin_t187ap05_O）", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"),
    "損益_上市": ("證交所 OpenAPI｜上市公司綜合損益表（t187ap06_L_ci 等六份）", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"),
    "損益_上櫃": ("櫃買中心 OpenAPI｜上櫃公司綜合損益表（mopsfin_t187ap06_O_ci 等六份）", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci"),
}


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
    points: float    # 強度（已乘上可信度係數）
    direction: int   # +1 偏多、-1 偏空、0 中性
    reason: str
    evidence: list[dict] = field(default_factory=list)  # 出處，見 ev_news / ev_data
    key: str = ""    # 去重用，同一 key 只在第一次出現那天報
    grade: str = "B"  # 可信度 A / B / C
    check: str = ""   # 來源檢核說明


def load_notes(vault: Path) -> dict[str, dict]:
    """代號 → {title, name, 產業, 市場}。"""
    out = {}
    for p in sorted((vault / NOTE_DIR).glob("*.md")):
        m = re.match(r"^(\d{4,6}[A-Z]?)(?=\D)", p.stem)
        if not m:
            continue
        head = p.read_text("utf-8")[:3000]
        ind = re.search(r"^產業:\s*(.+)$", head, re.M)
        mkt = re.search(r"^市場:\s*(.+)$", head, re.M)
        out[m.group(1)] = {"title": p.stem, "name": p.stem[len(m.group(1)):],
                           "產業": ind.group(1).strip() if ind else "未分類",
                           "市場": mkt.group(1).strip() if mkt else "上市"}
    return out


def load_0050(vault: Path) -> set[str]:
    f = vault / LIST_0050
    if not f.exists():
        return set()
    return {re.match(r"\d{4,6}", ln).group(0) for ln in f.read_text("utf-8").splitlines()
            if re.match(r"\d{4,6}", ln.strip())}


def load_news(vault: Path, notes: dict[str, dict]) -> tuple[list[News], dict]:
    """讀所有新聞檔，回傳 (新聞, 檢核資訊)。"""
    items, meta = [], {"檔數": 0, "重複": 0, "最後更新": ""}
    for p in (vault / NEWS_DIR).glob("* 新聞.md"):
        m = re.match(r"^(\d{4,6}[A-Z]?)", p.name)
        if not m or m.group(1) not in notes:
            continue
        meta["檔數"] += 1
        code, day, seen = m.group(1), None, set()
        text = p.read_text("utf-8")
        upd = re.search(r"^最後更新:\s*(.+)$", text[:500], re.M)
        if upd and upd.group(1).strip() > meta["最後更新"]:
            meta["最後更新"] = upd.group(1).strip()
        for ln in text.splitlines():
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
                meta["重複"] += 1
                continue
            seen.add((it.key, day))
            items.append(it)
    return items, meta


# ---------- 來源與可信度 ----------

def source_tier(source: str, cfg: dict) -> str:
    for tier, kws in cfg["來源分級"].items():
        if any(k.lower() in source.lower() for k in kws):
            return tier
    return "其他"


def ev_news(n: News, cfg: dict) -> dict:
    return {"類": "重訊" if n.kind == "重訊" else "新聞", "代號": n.code, "標題": n.title, "來源": n.source,
            "等級": source_tier(n.source, cfg), "網址": n.url, "時間": f"{n.at:%Y-%m-%d %H:%M}"}


def ev_data(dataset: str, detail: str) -> dict:
    return {"類": "資料", "集": dataset, "說明": detail}


def grade_news(ns: list[News], cfg: dict) -> tuple[str, str]:
    """新聞類訊號的可信度：看來源等級與有幾家不同來源。"""
    sources = sorted({n.source for n in ns})
    tiers = {source_tier(s, cfg) for s in sources}
    if "官方" in tiers:
        return "A", "官方公告（公開資訊觀測站）"
    if len(sources) >= 2 and "主流媒體" in tiers:
        return "A", f"{len(sources)} 家來源交叉確認（{'、'.join(sources[:3])}），含主流媒體"
    if "主流媒體" in tiers:
        return "B", f"單一主流媒體（{sources[0]}），未見第二來源"
    if len(sources) >= 2:
        return "B", f"{len(sources)} 家非主流來源（{'、'.join(sources[:3])}），未見主流媒體"
    return "C", f"只有{source_tier(sources[0], cfg)}來源（{sources[0]}），需查證"


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


def verify_revenue(cur: dict, prev: dict, tol: float) -> tuple[bool | None, str]:
    """驗算：前月營收 ×（1＋月增率）應等於本月營收。回傳 (是否一致；None 為無法驗算, 說明)。"""
    rev, mom, pr = cur.get("月營收"), cur.get("月增率"), prev.get("月營收")
    if None in (rev, mom, pr) or pr == 0:
        return None, "前月資料不足，未驗算月增率"
    exp = pr * (1 + mom / 100)
    diff = abs(rev - exp)
    if diff <= max(abs(rev) * tol, 0.02):
        return True, f"月增率驗算一致（前月 {pr:g} 億 × (1{mom:+g}%) ≈ {exp:.2f} 億）"
    return False, f"月增率驗算不一致：前月 {pr:g} 億 × (1{mom:+g}%) = {exp:.2f} 億，公布值 {rev:g} 億"


# ---------- 訊號規則 ----------

def revenue_signals(code: str, series: dict, cfg: dict, market: str) -> list[Signal]:
    R = cfg["營收"]
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
    ok, note = verify_revenue(cur, prev, cfg["驗算容許誤差"])
    grade = "B" if ok is False else "A"
    check = ("官方資料；" + note) if ok is not False else ("⚠️ 官方資料，但" + note)
    ds = "月營收_上櫃" if market == "上櫃" else "月營收_上市"
    ev = [ev_data(ds, f"{ym} 月營收 {rev:g} 億，年增 {yoy:g}%" + (f"，前月年增 {py:g}%" if py is not None else ""))]
    out = []

    def add(label, pts, d, reason):
        out.append(Signal(code, "營收", label, round(pts * scale, 2), d, reason, ev, f"{code}|{label}|{ym}", grade, check))
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


def quarter_signals(code: str, series: dict, Q: dict, market: str) -> list[Signal]:
    if not series:
        return []
    q = max(series)
    cur, prev = series[q], series.get(prev_quarter(q))
    if not prev:
        return []  # 要有前一季才比得出轉折
    out = []
    same_year = q[:4] == prev_quarter(q)[:4]
    ds = "損益_上櫃" if market == "上櫃" else "損益_上市"

    def add(label, pts, d, reason, detail):
        out.append(Signal(code, "季報", label, pts, d, reason, [ev_data(ds, f"{q}：{detail}")], f"{code}|{label}|{q}",
                          "A", f"官方資料；與 {prev_quarter(q)} 的歷史快照比較"))

    # 毛利率、營益率是年度累計值；同一年度內累計值上升代表最新一季比前幾季好
    for k, th, name in (("毛利率", Q["毛利率變動百分點"], "毛利率"), ("營業利益率", Q["營益率變動百分點"], "營益率")):
        a, b = cur.get(k), prev.get(k)
        if a is None or b is None or not same_year:
            continue
        if a - b >= th:
            add(f"{name}跳升", 3, 1, f"{q} 累計{name} {a:g}%，比 {prev_quarter(q)} 的 {b:g}% 提高 {a - b:.1f} 個百分點",
                f"累計{name} {b:g}% → {a:g}%")
        elif b - a >= th:
            add(f"{name}下滑", 3, -1, f"{q} 累計{name} {a:g}%，比 {prev_quarter(q)} 的 {b:g}% 下降 {b - a:.1f} 個百分點",
                f"累計{name} {b:g}% → {a:g}%")
    e1, e0 = single_eps(series, q), single_eps(series, prev_quarter(q))
    if e1 is not None and e0 is not None:
        if e0 < 0 <= e1:
            add("單季轉虧為盈", 4, 1, f"{q} 單季 EPS {e1:g} 元，前一季 {e0:g} 元", f"單季 EPS {e0:g} → {e1:g} 元")
        elif e0 > 0 > e1:
            add("單季轉盈為虧", 4, -1, f"{q} 單季 EPS {e1:g} 元，前一季 {e0:g} 元", f"單季 EPS {e0:g} → {e1:g} 元")
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


def classify_news(items: list[News], cfg: dict, rx: dict) -> dict:
    """把新聞分成：可計分、彙整文（只算題材熱度）、營收回聲（只算新聞量）、雜訊。"""
    shared = Counter(n.key for n in items)  # 同一標題出現在幾家公司的新聞檔
    tag = {}
    for n in items:
        if n.kind == "重訊":  # 例行公告（背書保證、受邀論壇…）當雜訊，不算新聞量
            tag[id(n)] = "score" if material_tier(n.title, cfg) else "noise"
            continue
        codes_in_title = len(set(re.findall(r"(?<!\d)\d{4}(?!\d)", n.title)))
        names_in_title = n.title.count("、") + 1
        if any(p.search(n.title) for p in rx["noise"]):
            tag[id(n)] = "noise"
        elif any(p.search(n.title) for p in rx["echo"]):
            tag[id(n)] = "echo"
        elif shared[n.key] >= cfg["彙整文公司數"] or codes_in_title >= 3 or names_in_title >= 4:
            tag[id(n)] = "roundup"
        else:
            tag[id(n)] = "score"
    return tag


def news_signals(code: str, win: list[News], tag: dict, cfg: dict) -> list[Signal]:
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
        grade, check = grade_news(ns, cfg)
        reason = ns[0].title + (f"（另有 {len(ns) - 1} 則相關）" if len(ns) > 1 else "")
        raw = pts + min(len(ns) - 1, 2) * 0.5
        out.append(Signal(code, kind, label, round(raw * cfg["可信度係數"][grade], 2), d, reason,
                          [ev_news(n, cfg) for n in ns[:3]],
                          f"{code}|{kind}|" + "+".join(sorted(n.key for n in ns))[:200], grade, check))
    return out


def volume_signal(code: str, win: list[News], hist_items: list[News], tag: dict, cfg: dict,
                  day: dt.date) -> Signal | None:
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
    sources = len({n.source for n in usable})
    ev = [ev_news(n, cfg) for n in sorted(usable, key=lambda x: x.at, reverse=True)[:3]]
    return Signal(code, "新聞量", "新聞量暴增", round(2 + min(math.log2(ratio), 3), 2), 0,
                  f"本期 {cnt} 則，前 {V['基準天數']} 天平均每天 {avg:.1f} 則（{ratio:.1f} 倍）", ev,
                  f"{code}|新聞量|{day}", "B", f"依新聞檔計數，來自 {sources} 家來源；Google 新聞只涵蓋關注清單")


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
            uniq = {n.key: n for n in sorted(samples[th], key=lambda x: x.at)}.values()
            rows.append({"題材": th, "公司數": cnt, "基準": round(base, 2), "倍數": round(ratio, 2), "熱": hot,
                         "公司": sorted(now[th]),
                         "代表新聞": [ev_news(x, cfg) for x in sorted(uniq, key=lambda x: x.at, reverse=True)[:3]]})
    rows.sort(key=lambda r: (-r["熱"], -r["公司數"], -r["倍數"]))
    matrix = {th: {d.isoformat(): len(c) for d, c in per_day[th].items()} for th in themes}
    return rows, matrix


# ---------- 評分 ----------

def size_bonus(rev: float | None, in0050: bool, S: dict) -> float:
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


# ---------- 資料檢核 ----------

def check_links(urls: list[str], timeout: int = 10) -> dict[str, str]:
    """實際連線檢查引用連結（只在 Actions 用 --check-links 時執行）。回傳 網址 → 「ok 200」或錯誤說明。"""
    def one(u: str) -> tuple[str, str]:
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(u, method=method, headers={"User-Agent": "Mozilla/5.0 (tw-stock-vault-signal)"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return u, f"ok {r.status}"
            except urllib.error.HTTPError as e:
                if method == "HEAD" and e.code in (400, 403, 405, 429):
                    continue
                return u, f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001
                return u, f"連線失敗 {type(e).__name__}"
        return u, "連線失敗"
    with ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, urls))


def schedule_warnings(vault: Path, day: dt.date) -> list[str]:
    """讀當天日報附錄裡排程留下的「本次部分來源失敗」警告（只看最近一次執行）。"""
    p = vault / DAILY_DIR / f"{day} 台股日報.md"
    if not p.exists():
        return []
    m = re.search(r"<!-- headlines:start[^>]*-->\n?(.*?)(?=\n### |\Z)", p.read_text("utf-8"), re.S)
    if not m or "[!warning]" not in m.group(1):
        return []
    return [ln[4:].strip() for ln in m.group(1).splitlines() if ln.startswith("> - ")]


def data_checks(vault: Path, cfg: dict, day: dt.date, news_meta: dict, win: list[News], tag: dict,
                hist: dict, notes: dict, top: list[dict], links: dict | None, summary_date: str) -> list[dict]:
    rows = []

    def add(item, ok, note):
        rows.append({"項目": item, "狀態": ok, "說明": note})

    ref = min(dt.datetime.now(TZ).replace(tzinfo=None), dt.datetime.combine(day, dt.time(23, 59)))
    last = news_meta["最後更新"]
    if last:
        age = (ref - dt.datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() / 3600
        add("新聞資料新鮮度", "✅" if age <= cfg["新聞資料最久小時"] else "⚠️",
            f"新聞檔最後更新 {last}（距報告時間 {max(age, 0):.0f} 小時；超過 {cfg['新聞資料最久小時']} 小時代表排程延遲或失敗）")
    else:
        add("新聞資料新鮮度", "❌", "找不到新聞檔的更新時間")
    warns = schedule_warnings(vault, day)
    add("新聞抓取排程", "⚠️" if warns else "✅",
        ("最近一次排程有來源失敗：" + "；".join(warns[:4])) if warns else "最近一次排程沒有來源失敗紀錄")
    tiers = Counter(source_tier(n.source, cfg) for n in win)
    mix = "、".join(f"{t} {c}" for t, c in tiers.most_common()) or "無"
    weekday = day.weekday() < 5
    add("新聞來源組成", "⚠️" if weekday and not tiers.get("官方") else "✅",
        f"視窗內 {len(win)} 則：{mix}" + ("（平日卻沒有官方重訊，可能是重訊來源抓取失敗）" if weekday and not tiers.get("官方") else ""))
    noise = sum(1 for n in win if tag[id(n)] == "noise")
    roundup = sum(1 for n in win if tag[id(n)] == "roundup")
    echo = sum(1 for n in win if tag[id(n)] == "echo")
    add("雜訊過濾", "✅", f"排除雜訊 {noise} 則（例行重訊、零股排行等），彙整文 {roundup} 則只算題材熱度，營收速報 {echo} 則只算新聞量")
    add("新聞檔重複行", "✅" if not news_meta["重複"] else "ℹ️",
        f"讀檔時略過同日同標題的重複行 {news_meta['重複']} 筆" + ("（舊版去重錯誤留下的，不影響計分）" if news_meta["重複"] else ""))
    if summary_date:
        age_d = (day - dt.date.fromisoformat(summary_date)).days
        add("財務資料新鮮度", "✅" if age_d <= cfg["財務資料最久天數"] else "⚠️",
            f"`_data/財務摘要.json` 抓取日期 {summary_date}（{age_d} 天前）")
    else:
        add("財務資料新鮮度", "❌", "找不到 `_data/財務摘要.json`")
    latest = Counter(max(s) for s in hist["月營收"].values() if s)
    if latest:
        ym, n = latest.most_common(1)[0]
        share = sum(1 for c in notes if ym in hist["月營收"].get(c, {})) / max(len(notes), 1)
        add("月營收覆蓋", "✅" if share >= 0.9 else "ℹ️",
            f"{share:.0%} 的個股筆記已有 {ym} 月營收" + ("" if share >= 0.9 else "（月中陸續公布，其餘公司下次排程補上）"))
    bad = []
    for c in notes:
        s = expand(hist["月營收"].get(c, {}), REV_COLS)
        if len(s) >= 2:
            ym = max(s)
            ok, _ = verify_revenue(s[ym], s.get(prev_month(ym)) or {}, cfg["驗算容許誤差"])
            if ok is False:
                bad.append(notes[c]["title"])
    add("月營收驗算", "✅" if not bad else "⚠️",
        "前月營收 ×（1＋月增率）與公布值全部一致" if not bad else
        f"{len(bad)} 家不一致（可能是公司更正前月營收）：" + "、".join(f"[[{t}]]" for t in bad[:8]) + ("…" if len(bad) > 8 else ""))
    qprev = sum(1 for s in hist["季報"].values() if s and prev_quarter(max(s)) in s)
    add("季報比較基礎", "✅" if qprev else "ℹ️",
        f"{qprev} 家有前一季快照可比較" if qprev else "歷史快照還沒有前一季，季報轉折要等下一季財報公布後才會出現")
    graded = Counter(s["grade"] for c in top for s in c["訊號"])
    total = sum(graded.values())
    if total:
        add("入榜訊號可信度", "✅" if graded.get("C", 0) / total < 0.2 else "⚠️",
            f"A 級 {graded.get('A', 0)}、B 級 {graded.get('B', 0)}、C 級 {graded.get('C', 0)}（C 級只有單一平台來源，引用前要查證）")
    gnews = sum(1 for c in top for s in c["訊號"] for e in s["evidence"] if "news.google.com" in e.get("網址", ""))
    if gnews:
        add("Google 新聞轉址", "ℹ️", f"{gnews} 筆出處是 Google 新聞轉址連結，需用瀏覽器開啟才會導到原始媒體")
    if links is None:
        add("連結可用性", "ℹ️", "本次未實際連線檢查（只有排程以 `--check-links` 執行時才檢查）")
    else:
        fail = [u for u, v in links.items() if not v.startswith("ok")]
        add("連結可用性", "✅" if not fail else "⚠️", f"檢查 {len(links)} 個引用連結，{len(links) - len(fail)} 個可開啟" +
            (f"，{len(fail)} 個失敗（見文末資料來源標記）" if fail else ""))
    if "待校對" in ((vault / LIST_0050).read_text("utf-8") if (vault / LIST_0050).exists() else ""):
        add("0050 成分股名單", "ℹ️", "`_data/0050成分股.txt` 標註 #待校對，⭐ 標記請以元大投信公告為準")
    return rows


# ---------- 主流程 ----------

def run(vault: Path, day: dt.date, since: str | None, dry: bool, do_links: bool = False) -> dict:
    cfg = read_json(vault / RULES, None)
    if cfg is None:
        raise SystemExit(f"找不到 {RULES}")
    rx = compile_rules(cfg)
    notes = load_notes(vault)
    core = load_0050(vault)
    hist = update_history(vault, read_json(vault / HISTORY, {}))
    ledger = read_json(vault / LEDGER, {})
    today = day.isoformat()
    summary_date = read_json(vault / SUMMARY, {}).get("抓取日期", "")

    items, news_meta = load_news(vault, notes)
    start = window(day, since)
    end = dt.datetime.combine(day, dt.time(23, 59))
    win = [n for n in items if start <= n.at <= end]
    win_ids = {id(n) for n in win}
    tag = classify_news(items, cfg, rx)
    radar, matrix = theme_radar(items, tag, cfg, win_ids, day)
    hot = {r["題材"] for r in radar if r["熱"]}

    by_code: dict[str, list[News]] = defaultdict(list)
    for n in items:
        by_code[n.code].append(n)
    win_by_code: dict[str, list[News]] = defaultdict(list)
    for n in win:
        win_by_code[n.code].append(n)

    candidates, all_fin = [], []
    for code, meta in notes.items():
        series = expand(hist["月營收"].get(code, {}), REV_COLS)
        sigs = revenue_signals(code, series, cfg, meta["市場"])
        sigs += quarter_signals(code, expand(hist["季報"].get(code, {}), QTR_COLS), cfg["季報"], meta["市場"])
        w = win_by_code.get(code, [])
        sigs += news_signals(code, w, tag, cfg)
        v = volume_signal(code, w, by_code.get(code, []), tag, cfg, day) if w else None
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
        bonus = size_bonus(rev, code in core, cfg["規模"])
        net = sum(s.points * s.direction for s in fresh)
        dirs = {s.direction for s in fresh if s.direction}
        direction = "多空交錯" if len(dirs) > 1 and abs(net) < 2 else ("偏多" if net > 0 else "偏空" if net < 0 else "中性")
        best = min(s.grade for s in fresh)  # A < B < C
        candidates.append({
            "代號": code, "公司": meta["title"], "產業": meta["產業"], "0050": code in core,
            "分數": score_company(fresh, bonus, resonance, theme), "訊號分": score_company(fresh, 0, 0, 0),
            "方向": direction, "可信度": best, "規模加分": bonus, "共振加分": resonance,
            "題材加分": theme, "題材": themes_hit,
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
        # 同產業多空互見不算共振：用多空相抵後的淨家數判斷
        d = 1 if len(v[1]) >= len(v[-1]) else -1
        codes, other = v[d], v[-d]
        net = len(codes) - len(other)
        if net >= P["最少公司數"] and net / ind_total[k] >= P["最低比例"]:
            industry.append({"產業": k, "方向": "偏多" if d > 0 else "偏空", "家數": len(codes), "反向家數": len(other),
                             "淨家數": net, "產業家數": ind_total[k], "公司": sorted(codes)})
    industry.sort(key=lambda x: -x["淨家數"] / x["產業家數"])

    links = None
    if do_links:
        urls = sorted({e["網址"] for c in top for s in c["訊號"] for e in s["evidence"]
                       if e.get("網址", "").startswith("http") and e["類"] != "重訊"}
                      | {e["網址"] for t in radar if t["熱"] for e in t["代表新聞"] if e["類"] != "重訊"})
        links = check_links(urls[:120])

    cutoff = (day - dt.timedelta(days=cfg["已報保留天數"])).isoformat()
    ledger = {k: v for k, v in ledger.items() if v >= cutoff}
    result = {"日期": today, "產生時間": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
              "新聞視窗": f"{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}",
              "財務資料日期": summary_date,
              "統計": {"視窗內新聞": len(win), "候選公司": len(candidates), "入榜門檻以上": len(ranked)},
              "TopN": top, "題材": radar, "產業共振": industry,
              "財務訊號": [s.__dict__ for s in sorted(all_fin, key=lambda s: (-s.points, s.code))],
              "資料檢核": data_checks(vault, cfg, day, news_meta, win, tag, hist, notes, top, links, summary_date),
              "連結檢查": links}
    report = render_daily(result, notes)
    if dry:
        print(report)
        return result
    write_if_changed(vault / HISTORY, json.dumps(hist, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    write_if_changed(vault / LEDGER, json.dumps(ledger, ensure_ascii=False, indent=0, sort_keys=True))
    write_if_changed(vault / OUT_JSON_DIR / f"{today}.json", json.dumps(compact(result), ensure_ascii=False, indent=1))
    write_if_changed(vault / OUT_DIR / f"{today} 異常訊號.md", report)
    write_if_changed(vault / OUT_DIR / "題材雷達.md", render_radar(matrix, day))
    print(f"{today}：候選 {len(candidates)} 家，入榜 {len(top)} 家，熱門題材 {len(hot)} 個")
    return result


# ---------- 輸出 ----------

def compact(r: dict) -> dict:
    """存檔用的精簡版：Top N 保留完整證據，其餘只留代號、訊號與理由，給週報彙整。"""
    out = {k: v for k, v in r.items() if k not in ("題材", "財務訊號")}
    out["題材"] = [{k: v for k, v in t.items() if k != "代表新聞"} for t in r["題材"]]
    out["財務訊號"] = [[s["code"], s["label"], s["points"], s["direction"], s["reason"]] for s in r["財務訊號"]]
    return out


def cell(s: str) -> str:
    """放進表格的文字：去掉會打斷表格或連結的符號。"""
    return re.sub(r"\s+", " ", str(s)).replace("|", "／").replace("[", "［").replace("]", "］")


def mm(s: str) -> str:
    """Mermaid 節點文字：去掉會打斷語法的符號。"""
    return re.sub(r"[\"\[\]{}|<>()#;]", " ", str(s)).strip()


class Refs:
    """文末資料來源：同一出處只編一個號，正文用 Obsidian 註腳 [^n] 引用。"""

    def __init__(self, r: dict):
        self.r, self.keys, self.defs = r, {}, []

    def cite(self, e: dict) -> str:
        if e["類"] == "資料":
            key = "ds:" + e["集"]
        elif e["類"] == "重訊":
            key = f"mops:{e['代號']}:{e['時間']}:{norm_title(e['標題'])}"
        else:
            key = "url:" + e["網址"]
        if key not in self.keys:
            self.keys[key] = len(self.keys) + 1
            self.defs.append((self.keys[key], self.text(e)))
        return f"[^{self.keys[key]}]"

    def text(self, e: dict) -> str:
        if e["類"] == "資料":
            name, url = DATASETS[e["集"]]
            return (f"{name}。官方開放資料，排程抓取日 {self.r['財務資料日期'] or '未知'}，repo 快照 `_data/財務摘要.json`；"
                    f"轉折比較用 `_data/訊號/財務歷史.json`。[{url}]({url})")
        title = cell(e["標題"])
        if e["類"] == "重訊":
            return (f"公開資訊觀測站重大訊息（官方），{e['時間']}，代號 {e['代號']}：〈{title}〉。"
                    f"[公開資訊觀測站]({MOPS_URL})（以代號與日期查詢原文）")
        url = e["網址"]
        host = urllib.parse.urlparse(url).netloc
        shown = "Google 新聞轉址連結" if "news.google.com" in host else host
        mark = ""
        links = self.r.get("連結檢查")
        if links and url in links:
            mark = "　✅ 連結可開啟" if links[url].startswith("ok") else f"　⚠️ 連結檢查失敗（{links[url]}）"
        return f"{e['來源']}（{e['等級']}），{e['時間']}，〈{title}〉。[{shown}]({url}){mark}"


def nav_map(r: dict, notes: dict) -> list[str]:
    """開頭導覽圖：方向 → 個股，加上熱門題材與產業共振。節點可點進個股筆記。"""
    top, links, lines = r["TopN"], [], ["```mermaid", "flowchart LR"]
    groups = [("B", "🔺 偏多", [c for c in top if c["方向"] == "偏多"]),
              ("S", "🔻 偏空", [c for c in top if c["方向"] == "偏空"]),
              ("N", "◽ 中性／多空交錯", [c for c in top if c["方向"] in ("中性", "多空交錯")])]
    hot = [t for t in r["題材"] if t["熱"]]
    lines.append(f'  R(("{r["日期"][5:].replace("-", "/")} 訊號<br/>{len(top)} 檔候選"))')
    for gid, name, cs in groups:
        if not cs:
            continue
        lines.append(f'  R --> {gid}["{name} {len(cs)} 檔"]')
        for i, c in enumerate(cs[:6], 1):
            nid = f"{gid}{i}"
            lines.append(f'  {gid} -->|{mm(c["訊號"][0]["label"])}| {nid}["{mm(c["公司"])}"]')
            links.append(nid)
        if len(cs) > 6:
            lines.append(f'  {gid} --> {gid}x["…另 {len(cs) - 6} 檔"]')
    if hot:
        lines.append(f'  R --> T["🔥 熱門題材 {len(hot)} 個"]')
        for i, t in enumerate(hot[:5], 1):
            lines.append(f'  T --> T{i}["{mm(t["題材"])}<br/>{t["公司數"]} 家・{t["倍數"]:g} 倍"]')
    if r["產業共振"]:
        lines.append(f'  R --> I["🏭 產業共振 {len(r["產業共振"])} 個"]')
        for i, x in enumerate(r["產業共振"][:5], 1):
            lines.append(f'  I --> I{i}["{mm(x["產業"])} {x["方向"]}<br/>淨 {x["淨家數"]}/{x["產業家數"]} 家"]')
    if links:
        lines.append(f"  class {','.join(links)} internal-link")
    lines.append("```")
    return lines


def key_points(r: dict) -> list[str]:
    top, pts = r["TopN"], []
    bull = [c for c in top if c["方向"] == "偏多"]
    bear = [c for c in top if c["方向"] == "偏空"]
    if bull:
        c = bull[0]
        pts.append(f"**最強偏多**：[[{c['公司']}]]（{c['分數']:g} 分，可信度 {c['可信度']}），{c['訊號'][0]['reason']}。")
    if bear:
        c = bear[0]
        pts.append(f"**最強偏空**：[[{c['公司']}]]（{c['分數']:g} 分，可信度 {c['可信度']}），{c['訊號'][0]['reason']}。")
    hot = [t for t in r["題材"] if t["熱"]]
    if hot:
        pts.append("**熱門題材**：" + "；".join(f"{t['題材']} {t['公司數']} 家（前 7 天日均 {t['基準']:g} 家，{t['倍數']:g} 倍）" for t in hot[:3]) + "。")
    else:
        pts.append("**熱門題材**：本期沒有題材同時達到公司數與倍數門檻。")
    if r["產業共振"]:
        x = r["產業共振"][0]
        pts.append(f"**產業共振**：{x['產業']}有 {x['家數']} 家出現營收{x['方向']}轉折（反向 {x['反向家數']} 家，產業共 {x['產業家數']} 家），淨比例最高。")
    warn = [c for c in r["資料檢核"] if c["狀態"] in ("⚠️", "❌")]
    pts.append("**資料檢核**：" + ("全部通過。" if not warn else f"{len(warn)} 項需注意（{'、'.join(c['項目'] for c in warn)}），見〈七、資料檢核〉。"))
    return pts


def render_daily(r: dict, notes: dict) -> str:
    refs = Refs(r)
    top, st = r["TopN"], r["統計"]
    status = "❌" if any(c["狀態"] == "❌" for c in r["資料檢核"]) else "⚠️" if any(c["狀態"] == "⚠️" for c in r["資料檢核"]) else "✅"
    dirs = Counter(c["方向"] for c in top)
    official = sum(1 for c in top for s in c["訊號"] if s["kind"] in ("營收", "季報", "重訊"))
    total_sig = sum(len(c["訊號"]) for c in top)
    hot = [t for t in r["題材"] if t["熱"]]

    out = ["---", f"日期: {r['日期']}", "類型: 訊號報告", f"產生時間: {r['產生時間']}", f"資料檢核: \"{status}\"",
           "重點個股: [" + ", ".join(f'"[[{c["公司"]}]]"' for c in top[:10]) + "]",
           "tags: [新聞, 訊號]", "---", f"# {r['日期']} 台股異常訊號報告", "",
           "> [!abstract] 報告摘要",
           f"> 新聞視窗 {r['新聞視窗']}；財務資料 {r['財務資料日期'] or '未知'}（證交所、櫃買中心 OpenAPI）。"
           f"視窗內 {st['視窗內新聞']} 則新聞，{st['候選公司']} 家公司出現新訊號，{st['入榜門檻以上']} 家達入榜門檻，取前 {len(top)} 名。"
           f"資料檢核 {status}。", ""]

    out += ["## 一、訊號導覽", ""] + nav_map(r, notes) + [""]
    out += ["| 入榜候選 | 🔺 偏多 | 🔻 偏空 | ◽ 中性／交錯 | 🔥 熱門題材 | 🏭 產業共振 | 官方來源訊號 | 資料檢核 |",
            "|---|---|---|---|---|---|---|---|",
            f"| {len(top)} | {dirs.get('偏多', 0)} | {dirs.get('偏空', 0)} | {dirs.get('中性', 0) + dirs.get('多空交錯', 0)} | "
            f"{len(hot)} | {len(r['產業共振'])} | {official}/{total_sig} | {status} |", ""]
    out += ["### 重點摘要", ""] + [f"- {p}" for p in key_points(r)] + [""]

    out += ["## 二、Top 候選總表", "",
            "| # | 公司 | 產業 | 分數 | 方向 | 可信度 | 主要訊號 | 出處 |", "|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(top, 1):
        tags = "、".join(dict.fromkeys(s["label"] for s in c["訊號"][:3]))
        cites = "".join(dict.fromkeys(refs.cite(e) for s in c["訊號"][:3] for e in s["evidence"][:1]))
        star = "⭐" if c["0050"] else ""
        out.append(f"| {i} | [[{c['公司']}]]{star} | {c['產業']} | {c['分數']:g} | {c['方向']} | {c['可信度']} | {cell(tags)} | {cites} |")
    if not top:
        out.append("| | 今天沒有達門檻的訊號 | | | | | | |")
    out += ["", "⭐ 0050 成分股。可信度：A 官方資料或多來源交叉確認；B 單一主流媒體或多家非主流來源；C 只有單一平台來源，引用前必須查證。", ""]

    out += ["## 三、個股訊號明細", ""]
    for i, c in enumerate(top, 1):
        parts = [f"訊號 {c['訊號分']:g}"]
        if c["規模加分"]:
            parts.append(f"規模 {c['規模加分']:+g}")
        if c["共振加分"]:
            parts.append(f"新聞與財報共振 +{c['共振加分']:g}")
        if c["題材加分"]:
            parts.append(f"熱門題材（{'、'.join(c['題材'])}）+{c['題材加分']:g}")
        out += [f"### {i}. [[{c['公司']}]]{'⭐' if c['0050'] else ''}｜{c['分數']:g} 分｜{c['方向']}｜可信度 {c['可信度']}",
                f"{c['產業']}｜分數組成：{' ＋ '.join(parts)}｜完整新聞：[[{c['公司']} 新聞]]", "",
                "| 訊號 | 方向 | 強度 | 說明 | 來源檢核 | 出處 |", "|---|---|---|---|---|---|"]
        for s in c["訊號"]:
            arrow = {1: "🔺", -1: "🔻"}.get(s["direction"], "◽")
            cites = "".join(dict.fromkeys(refs.cite(e) for e in s["evidence"]))
            out.append(f"| {cell(s['label'])} | {arrow} | {s['points']:g} | {cell(s['reason'])} | {s['grade']}：{cell(s['check'])} | {cites} |")
        out.append("")

    out += ["## 四、題材雷達", "", "| 題材 | 本期公司數 | 前 7 天日均 | 倍數 | 公司 | 代表報導 |", "|---|---|---|---|---|---|"]
    for t in r["題材"][:15]:
        names = "、".join(f"[[{notes[c]['title']}]]" for c in t["公司"][:6]) + ("…" if len(t["公司"]) > 6 else "")
        cites = "".join(refs.cite(e) for e in t["代表新聞"][:3]) if t["熱"] else ""
        out.append(f"| {'🔥 ' if t['熱'] else ''}{t['題材']} | {t['公司數']} | {t['基準']:g} | {t['倍數']:g} | {names} | {cites} |")
    if not r["題材"]:
        out.append("| 本期沒有新聞提到題材關鍵字 | | | | | |")
    out += ["", "🔥 為熱門：本期公司數 ≥ 門檻，且是前 7 天日均的 2 倍以上。近 14 天走勢見 [[題材雷達]]。", ""]

    out += ["## 五、產業共振", ""]
    if r["產業共振"]:
        out += ["| 產業 | 方向 | 同向家數 | 反向家數 | 淨家數佔產業 | 公司 |", "|---|---|---|---|---|---|"]
        for x in r["產業共振"]:
            names = "、".join(f"[[{notes[c]['title']}]]" for c in x["公司"][:10]) + ("…" if x["家數"] > 10 else "")
            out.append(f"| {x['產業']} | {x['方向']} | {x['家數']}/{x['產業家數']} | {x['反向家數']} | {x['淨家數'] / x['產業家數']:.0%} | {names} |")
    else:
        out.append("本期沒有產業達到共振門檻。")
    out.append("")

    fin = r["財務訊號"]
    out += [f"## 六、財務訊號總表（{len(fin)} 筆，含未入榜）", ""]
    if fin:
        out += ["| 公司 | 訊號 | 說明 | 來源檢核 | 出處 |", "|---|---|---|---|---|"]
        for s in fin[:80]:
            out.append(f"| [[{notes[s['code']]['title']}]] | {s['label']} | {cell(s['reason'])} | {s['grade']} | {refs.cite(s['evidence'][0])} |")
        if len(fin) > 80:
            out.append(f"| … | 其餘 {len(fin) - 80} 筆見 `_data/訊號/{r['日期']}.json` | | | |")
    else:
        out.append("本期沒有新的財務訊號（同一期間的營收、季報訊號只在第一次出現那天列出）。")
    out.append("")

    out += ["## 七、資料檢核", "", "| 檢核項目 | 狀態 | 說明 |", "|---|---|---|"]
    for c in r["資料檢核"]:
        out.append(f"| {c['項目']} | {c['狀態']} | {c['說明']} |")
    out += ["", "✅ 通過　⚠️ 需注意　❌ 失敗　ℹ️ 說明", ""]

    out += ["## 八、方法與限制", "",
            "- 訊號由程式依 `_data/訊號規則.json` 的規則篩出，是值得追蹤的候選，不是投資建議；寫成文章前要回原始來源確認，未查證內容標 #待校對。",
            "- 分數＝同一家公司各訊號強度遞減加總（1、½、¼…）＋規模加分＋新聞與財報共振加分＋熱門題材加分；入榜只看訊號本身的分數。",
            "- 媒體訊號強度乘上可信度係數（A 1.0、B 0.85、C 0.6）。財務數字以官方開放資料為準，並用前月營收驗算月增率。",
            "- Google 新聞只抓關注清單，清單外公司的媒體新聞較少；重訊連結統一指向公開資訊觀測站查詢頁。",
            "- 架構說明見 `scripts/訊號篩選/篩選架構說明.md`。", ""]

    out += ["## 九、資料來源", "", "### 資料集", "",
            "| 資料 | 用途 | 位置 |", "|---|---|---|",
            f"| 證交所、櫃買中心 OpenAPI 月營收與綜合損益表 | 營收、季報訊號 | `_data/財務摘要.json`（抓取日 {r['財務資料日期'] or '未知'}） |",
            "| 公開資訊觀測站重大訊息 | 重訊分級 | `新聞/<代號名稱> 新聞.md` 的 🏛️ 重訊 |",
            "| 鉅亨網 API、Google 新聞（關注清單） | 媒體訊號、新聞量、題材熱度 | `新聞/<代號名稱> 新聞.md` 的 📰 媒體 |",
            "| 個股筆記 frontmatter「產業」、`_data/0050成分股.txt` | 產業共振、規模加分 | `個股/台股/` |", "",
            f"### 引用明細（共 {len(refs.defs)} 筆）", ""]
    out += [f"[^{n}]: {t}" for n, t in refs.defs]
    return "\n".join(out) + "\n"


def render_radar(matrix: dict, day: dt.date) -> str:
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
            seen[c["公司"]]["理由"] += [f"{d:%m/%d} {s['label']}（可信度 {s.get('grade', '?')}）：{s['reason']}" for s in c["訊號"][:2]]
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
    ap.add_argument("--check-links", action="store_true", help="實際連線檢查報告引用的連結（需要網路）")
    ap.add_argument("--week", help="週報素材：給週六日期，彙整該週一到週五的訊號")
    a = ap.parse_args()
    vault = Path(a.vault).resolve()
    if a.week:
        week(vault, dt.date.fromisoformat(a.week))
        return 0
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ).date()
    run(vault, day, a.since, a.dry_run, a.check_links)
    return 0


if __name__ == "__main__":
    sys.exit(main())
