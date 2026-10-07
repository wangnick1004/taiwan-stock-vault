#!/usr/bin/env python3
"""台股個股新聞自動更新：抓重大訊息與新聞，寫入 Obsidian 筆記庫。

只用標準函式庫，GitHub Actions 不需安裝套件。

用法：
    python scripts/update_news.py --vault . [--days 2] [--only 2330,2454]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; tw-stock-vault-news/1.0)"

TWSE_COMPANIES = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANIES = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_MATERIAL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_MATERIAL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
CNYES_LIST = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
GNEWS_RSS = "https://news.google.com/rss/search"
GNEWS_BLOCK = ("股市爆料同學會", "CMoney投資網誌", "PTT", "Dcard", "痞客邦")
GNEWS_MAX = 8  # 每家公司每次最多收幾則 Google 新聞
MOPS_LINK = "https://mops.twse.com.tw/mops/#/web/t05st01"

NEWS_DIR = "新聞"
DAILY_DIR = "新聞/每日"
ARCHIVE_DIR = "新聞/封存"
KEEP_DAYS = 90
BLOCK_ITEMS = 5
BLOCK_START = "<!-- news:start（自動產生，請勿手動編輯此範圍） -->"
BLOCK_END = "<!-- news:end -->"
BLOCK_RE = re.compile(r"<!-- news:start[^>]*-->.*?<!-- news:end -->", re.S)
SECTION_HEADING = "## 最新新聞"
SKIP_DIRS = {".git", ".obsidian", ".github", ".trash", NEWS_DIR, "海外"}

warnings: list[str] = []


def write_text(p: Path, text: str) -> None:
    """先寫暫存檔再 os.replace，避免同步中的檔案被寫到一半。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, p)


@dataclass(frozen=True)
class Item:
    code: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM 或空字串
    kind: str  # 🏛️ 重訊 / 📰 媒體
    source: str
    title: str
    url: str

    def line(self) -> str:
        t = f" {self.time}" if self.time else ""
        return f"- {self.kind}{t}｜{clean(self.title)}｜[{self.source}]({self.url})"

    @property
    def key(self) -> str:
        return norm_title(self.title)


def clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s.replace("|", "／").replace("[", "［").replace("]", "］")


def norm_title(s: str) -> str:
    return re.sub(r"[\s「」『』\"'“”‘’［］\[\]｜|：:，,。.!！?？-]", "", clean(s))


def fetch(url: str, params: dict | None = None, retries: int = 3) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str, params: dict | None = None):
    return json.loads(fetch(url, params).decode("utf-8-sig"))


def pick(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def roc_date(s: str) -> str:
    """'1151005' 或 '115/10/05' → '2026-10-05'。"""
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 8 and digits.startswith(("19", "20")):
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) in (6, 7):
        y, m, d = int(digits[:-4]) + 1911, digits[-4:-2], digits[-2:]
        return f"{y}-{m}-{d}"
    return ""


def hhmm(s: str) -> str:
    digits = re.sub(r"\D", "", s or "").zfill(6)[-6:]
    return f"{digits[:2]}:{digits[2:4]}" if digits.strip("0") else ""


# ---------- 公司清單 ----------

def load_companies() -> dict[str, dict]:
    companies: dict[str, dict] = {}
    for url, market in ((TWSE_COMPANIES, "上市"), (TPEX_COMPANIES, "上櫃")):
        try:
            rows = fetch_json(url)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{market}公司清單抓取失敗：{e}")
            continue
        for r in rows:
            code = pick(r, "公司代號", "SecuritiesCompanyCode", "CompanyCode")
            name = pick(r, "公司簡稱", "CompanyAbbreviation", "公司名稱", "CompanyName")
            if code and name:
                companies[code] = {"name": name, "market": market}
    return companies


# ---------- 來源 ----------

def material_news(since: str) -> list[Item]:
    items = []
    for url, market in ((TWSE_MATERIAL, "上市"), (TPEX_MATERIAL, "上櫃")):
        try:
            rows = fetch_json(url)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{market}重大訊息抓取失敗：{e}")
            continue
        for r in rows:
            code = pick(r, "公司代號", "SecuritiesCompanyCode", "CompanyCode")
            date = roc_date(pick(r, "發言日期", "Date", "AnnouncementDate"))
            title = pick(r, "主旨", "Subject", "主旨 ")
            if not (code and date and title) or date < since:
                continue
            items.append(Item(code, date, hhmm(pick(r, "發言時間", "Time")),
                              "🏛️ 重訊", "觀測站", title, MOPS_LINK))
    return items


def cnyes_codes(n: dict) -> set[str]:
    codes = set()
    for field in ("market", "stock"):
        for m in n.get(field) or []:
            raw = m.get("code") or m.get("symbol") if isinstance(m, dict) else m
            mm = re.search(r"(?:^|[:_])(\d{4,6}[A-Z]?)(?:$|[:_.])", str(raw or ""))
            if mm:
                codes.add(mm.group(1))
    return codes


def cnyes_news(since: str, companies: dict) -> list[Item]:
    start = int(dt.datetime.fromisoformat(since).replace(tzinfo=TZ).timestamp())
    end = int(time.time())
    items = []
    page = 1
    while page <= 30:
        try:
            data = fetch_json(CNYES_LIST, {"startAt": start, "endAt": end, "limit": 100, "page": page})
        except Exception as e:  # noqa: BLE001
            warnings.append(f"鉅亨網抓取失敗（第 {page} 頁）：{e}")
            break
        block = (data.get("items") or {})
        for n in block.get("data") or []:
            ts = dt.datetime.fromtimestamp(int(n.get("publishAt", 0)), TZ)
            url = f"https://news.cnyes.com/news/id/{n.get('newsId')}"
            title = n.get("title", "")
            for code in cnyes_codes(n) & companies.keys():
                if companies[code]["name"] not in title and code not in title:
                    continue  # 大盤綜合報導只是順帶標記，略過
                items.append(Item(code, ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"),
                                  "📰 媒體", "鉅亨網", title, url))
        if page >= int(block.get("last_page") or 1):
            break
        page += 1
    return items


def google_news(code: str, name: str, since: str) -> list[Item]:
    q = f'"{name}" {code} when:3d'
    raw = fetch(GNEWS_RSS, {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    items = []
    for it in ET.fromstring(raw).iter("item"):
        title = it.findtext("title") or ""
        src = it.findtext("source") or "Google 新聞"
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        if name not in title and code not in title:
            continue  # 名稱沒出現在標題的多半是雜訊
        if any(k in title or k in src for k in GNEWS_BLOCK):
            continue  # 論壇、部落格貼文
        try:
            ts = dt.datetime.strptime(it.findtext("pubDate") or "", "%a, %d %b %Y %H:%M:%S %Z")
            ts = ts.replace(tzinfo=dt.timezone.utc).astimezone(TZ)
        except ValueError:
            continue
        if ts.strftime("%Y-%m-%d") < since:
            continue
        items.append(Item(code, ts.strftime("%Y-%m-%d"), ts.strftime("%H:%M"),
                          "📰 媒體", clean(src), title, it.findtext("link") or ""))
    items.sort(key=lambda i: (i.date, i.time), reverse=True)
    return items[:GNEWS_MAX]


# ---------- 筆記庫 ----------

def index_notes(vault: Path) -> dict[str, Path]:
    """代號 → 個股筆記路徑（檔名以代號開頭，例如 2330台積電.md）。"""
    notes: dict[str, Path] = {}
    for p in vault.rglob("*.md"):
        if any(part in SKIP_DIRS for part in p.relative_to(vault).parts[:-1]):
            continue
        m = re.match(r"^(\d{4,6}[A-Z]?)(?=\D)", p.stem)
        if m and m.group(1) not in notes:
            notes[m.group(1)] = p
    return notes


def parse_news_file(text: str) -> dict[str, list[str]]:
    """把新聞檔解析成 {日期: [行...]}。"""
    days: dict[str, list[str]] = defaultdict(list)
    cur = None
    for ln in text.splitlines():
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})\s*$", ln)
        if m:
            cur = m.group(1)
        elif cur and ln.startswith("- "):
            days[cur].append(ln)
    return days


def line_key(ln: str) -> str:
    parts = ln.split("｜")
    return norm_title(parts[1]) if len(parts) >= 3 else norm_title(ln)


def render_news_file(code: str, title: str, market: str, days: dict[str, list[str]], now: dt.datetime) -> str:
    name = title[len(code):]
    out = ["---", f'代號: "{code}"', f"公司: {name}", f"市場: {market}",
           f"最後更新: {now:%Y-%m-%d %H:%M}", "tags: [新聞]", "---",
           f"# [[{title}]] 新聞動態", ""]
    for d in sorted(days, reverse=True):
        out += [f"## {d}", *days[d], ""]
    return "\n".join(out)


def archive(vault: Path, title: str, old: dict[str, list[str]]) -> None:
    by_year: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for d, lines in old.items():
        by_year[d[:4]][d] = lines
    for year, days in by_year.items():
        p = vault / ARCHIVE_DIR / f"{title} 新聞 {year}.md"
        existing = parse_news_file(p.read_text("utf-8")) if p.exists() else {}
        for d, lines in days.items():
            seen = {line_key(x) for x in existing.get(d, [])}
            existing.setdefault(d, []).extend(x for x in lines if line_key(x) not in seen)
        body = [f"# [[{title}]] 新聞封存 {year}", ""]
        for d in sorted(existing, reverse=True):
            body += [f"## {d}", *existing[d], ""]
        write_text(p, "\n".join(body))


def update_company(vault: Path, code: str, title: str, market: str, new: list[Item],
                   note: Path | None, now: dt.datetime) -> list[Item]:
    """寫入新聞檔與個股筆記區塊，回傳實際新增的項目。"""
    p = vault / NEWS_DIR / f"{title} 新聞.md"
    days = parse_news_file(p.read_text("utf-8")) if p.exists() else defaultdict(list)
    seen = {line_key(x) for lines in days.values() for x in lines}
    added = []
    for it in sorted(new, key=lambda i: (i.date, i.time), reverse=True):
        if it.key in seen:
            continue
        seen.add(it.key)
        days.setdefault(it.date, []).append(it.line())
        added.append(it)
    if not added:
        if note:
            update_note_block(note, title, days, now)
        return []
    for d in days:  # 同一天內依時間由新到舊
        days[d].sort(key=lambda x: re.search(r"\d{2}:\d{2}", x).group(0) if re.search(r"\d{2}:\d{2}", x) else "", reverse=True)
    cutoff = (now - dt.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    old = {d: v for d, v in days.items() if d < cutoff}
    if old:
        archive(vault, title, old)
        days = {d: v for d, v in days.items() if d >= cutoff}
    write_text(p, render_news_file(code, title, market, days, now))
    if note:
        update_note_block(note, title, days, now)
    return added


def update_note_block(note: Path, title: str, days: dict[str, list[str]], now: dt.datetime) -> None:
    latest = []
    for d in sorted(days, reverse=True):
        for ln in days[d]:
            latest.append(ln.replace("- ", f"- {d} ", 1))
    block = "\n".join([BLOCK_START, *latest[:BLOCK_ITEMS], "", f"完整紀錄：[[{title} 新聞]]", BLOCK_END])
    text = note.read_text("utf-8")
    m = BLOCK_RE.search(text)
    if m and m.group(0) == block:
        return
    if BLOCK_RE.search(text):
        text = BLOCK_RE.sub(lambda _: block, text, count=1)
    elif re.search(rf"^{re.escape(SECTION_HEADING)}\s*$", text, re.M):
        text = re.sub(rf"^({re.escape(SECTION_HEADING)})\s*$", lambda m: f"{m.group(1)}\n{block}", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n{SECTION_HEADING}\n{block}\n"
    stamp = f"新聞更新: {now:%Y-%m-%d %H:%M}"
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if fm:
        body = fm.group(1)
        body = re.sub(r"^新聞更新:.*$", stamp, body, flags=re.M) if re.search(r"^新聞更新:", body, re.M) else f"{body}\n{stamp}"
        text = f"---\n{body}\n---\n" + text[fm.end():]
    write_text(note, text)


def write_daily(vault: Path, now: dt.datetime, added: dict[str, list[Item]], titles: dict[str, str]) -> None:
    if not added and not warnings:
        return
    p = vault / DAILY_DIR / f"{now:%Y-%m-%d}.md"
    old = p.read_text("utf-8") if p.exists() else ""
    sections = []
    for code in sorted(added, key=lambda c: (-sum("重訊" in i.kind for i in added[c]), c)):
        its = sorted(added[code], key=lambda i: (i.date, i.time), reverse=True)
        today = f"{now:%Y-%m-%d}"
        lines = [i.line() if i.date == today else i.line().replace("- ", f"- {i.date[5:]} ", 1) for i in its]
        sections.append(f"### [[{titles[code]}]]\n" + "\n".join(lines))
    head = f"# {now:%Y-%m-%d} 台股新聞總覽\n"
    warn = ("> [!warning] 本次部分來源失敗\n" + "\n".join(f"> - {w}" for w in warnings) + "\n") if warnings else ""
    run = f"\n## {now:%H:%M} 更新（{len(added)} 家公司）\n\n" + ("\n\n".join(sections) if sections else "本次沒有新消息。") + "\n"
    prev = old.split("\n", 1)[1] if old.startswith("# ") else old
    prev = re.sub(r"^> \[!warning\].*?(?=\n[^>]|\Z)", "", prev, flags=re.S).lstrip("\n")
    write_text(p, head + warn + run + ("\n" + prev if prev else ""))


def load_watchlist(vault: Path, notes: dict[str, Path]) -> list[str]:
    f = vault / NEWS_DIR / "關注清單.md"
    if f.exists():
        codes = re.findall(r"^\s*[-*]\s*\[*\s*(\d{4,6}[A-Z]?)", f.read_text("utf-8"), re.M)
        if codes:
            return codes
    # 沒有關注清單時：內容最多的前 60 份個股筆記
    return sorted(notes, key=lambda c: notes[c].stat().st_size, reverse=True)[:60]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".")
    ap.add_argument("--days", type=int, default=2, help="抓最近幾天")
    ap.add_argument("--only", default=os.environ.get("ONLY_CODES", ""), help="只處理這些代號（逗號分隔），試跑用")
    ap.add_argument("--no-google", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    now = dt.datetime.now(TZ)
    since = (now - dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
    only = {c.strip() for c in args.only.split(",") if c.strip()}

    companies = load_companies()
    notes = index_notes(vault)
    if companies:
        # 代號相同但名稱對不上的筆記（例如 6753夏普 是日股，台股 6753 是別家公司）不寫入
        for code in list(notes):
            name = companies.get(code, {}).get("name", "")
            stem = notes[code].stem[len(code):]
            if not name or not (name in stem or stem in name):
                del notes[code]
    else:  # 兩份公司清單都抓不到時，至少處理已有筆記的公司
        companies = {c: {"name": p.stem[len(c):], "market": "未知"} for c, p in notes.items()}
    if only:
        companies = {c: v for c, v in companies.items() if c in only}
    print(f"公司數：{len(companies)}，已有筆記：{len(notes)}，起始日：{since}")

    items: list[Item] = [i for i in material_news(since) if i.code in companies]
    items += cnyes_news(since, companies)
    if not args.no_google:
        watch = [c for c in load_watchlist(vault, notes) if c in companies] or sorted(only & companies.keys())
        print(f"Google 新聞關注清單：{len(watch)} 檔")
        for code in watch:
            try:
                items += google_news(code, companies[code]["name"], since)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"Google 新聞 {code} 失敗：{e}")
            time.sleep(1.5)

    by_code: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        by_code[it.code].append(it)

    titles, added = {}, {}
    for code, its in by_code.items():
        note = notes.get(code)
        title = note.stem if note else f"{code}{companies[code]['name']}".replace("*", "")  # 沒有筆記時才用官方簡稱，去掉 * 以免連結失效
        titles[code] = title
        new = update_company(vault, code, title, companies[code]["market"], its, note, now)
        if new:
            added[code] = new
    for code, note in notes.items():
        if code in by_code or code not in companies:
            continue
        p = vault / NEWS_DIR / f"{note.stem} 新聞.md"
        if p.exists():
            update_note_block(note, note.stem, parse_news_file(p.read_text("utf-8")), now)
    write_daily(vault, now, added, titles)
    print(f"新增 {sum(map(len, added.values()))} 則，涉及 {len(added)} 家公司")
    for w in warnings:
        print(f"::warning::{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
