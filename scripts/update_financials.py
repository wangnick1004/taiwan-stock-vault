#!/usr/bin/env python3
"""台股個股官方財務數字自動更新：月營收、毛利率等三率、累計 EPS、現金股利。

資料來自證交所與櫃買中心 OpenAPI，只寫入個股筆記的 frontmatter，內文不動。
數字沒變就不改檔。

用法：
    python scripts/update_financials.py --vault . [--only 2330,2454]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_news import TZ, fetch_json, index_notes, load_companies, warnings, write_text  # noqa: E402

TWSE = "https://openapi.twse.com.tw/v1/opendata/"
TPEX = "https://www.tpex.org.tw/openapi/v1/"
REVENUE = [TWSE + "t187ap05_L", TPEX + "mopsfin_t187ap05_O"]
# 綜合損益表依產業分成多份：一般業、銀行、證券、金控、保險、異業
INCOME = [TWSE + f"t187ap06_L_{k}" for k in ("ci", "basi", "bd", "fh", "ins", "mim")] + \
         [TPEX + f"mopsfin_t187ap06_O_{k}" for k in ("ci", "basi", "bd", "fh", "ins", "mim")]
DIVIDEND = [TWSE + "t187ap45_L", TPEX + "mopsfin_t187ap45_O"]

FIELDS = ["月營收年月", "月營收", "月營收年增率", "月營收月增率", "累計營收年增率",
          "財報季度", "毛利率", "營業利益率", "淨利率", "累計EPS",
          "現金股利", "股利所屬年度", "財務更新"]


def num(v) -> float | None:
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "-", "--", "N/A") else None
    except ValueError:
        return None


def roc_year(s: str) -> int | None:
    d = re.sub(r"\D", "", s or "")
    return int(d) + 1911 if d and int(d) < 1911 else (int(d) if d else None)


def get(row: dict, *cands: str):
    """依序找欄位：先完全相同，再找包含關鍵字的欄位（櫃買中心欄位名稱可能略有不同）。"""
    for c in cands:
        if c in row and row[c] not in (None, ""):
            return row[c]
    for c in cands:
        for k, v in row.items():
            if c in k and v not in (None, ""):
                return v
    return None


def code_of(row: dict) -> str:
    return str(get(row, "公司代號", "SecuritiesCompanyCode", "CompanyCode") or "").strip()


def load(urls: list[str], label: str) -> list[dict]:
    rows = []
    for u in urls:
        try:
            data = fetch_json(u)
        except Exception as e:  # noqa: BLE001
            if u.endswith(("_ci", "05_L", "05_O", "45_L", "45_O")):
                warnings.append(f"{label}抓取失敗 {u.rsplit('/', 1)[1]}：{e}")
            continue
        if isinstance(data, list):
            if data and not code_of(data[0]):
                warnings.append(f"{label} {u.rsplit('/', 1)[1]} 找不到公司代號欄位：{list(data[0])[:8]}")
            rows += data
    return rows


def r1(x: float | None, nd: int = 2):
    return None if x is None else round(x, nd)


def revenue_fields(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for r in rows:
        ym = re.sub(r"\D", "", str(get(r, "資料年月", "YearMonth", "DataYearMonth") or ""))
        if len(ym) < 4:
            continue
        y, m = roc_year(ym[:-2]), ym[-2:]
        cur = num(get(r, "營業收入-當月營收", "當月營收", "Revenue"))
        out[code_of(r)] = {
            "月營收年月": f"{y}-{m}",
            "月營收": r1(cur / 1e5 if cur is not None else None),  # 原始單位：千元
            "月營收年增率": r1(num(get(r, "營業收入-去年同月增減(%)", "去年同月增減"))),
            "月營收月增率": r1(num(get(r, "營業收入-上月比較增減(%)", "上月比較增減"))),
            "累計營收年增率": r1(num(get(r, "累計營業收入-前期比較增減(%)", "前期比較增減"))),
        }
    return out


def income_fields(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for r in rows:
        code = code_of(r)
        y, q = roc_year(str(get(r, "年度", "Year") or "")), str(get(r, "季別", "Season", "Quarter") or "").strip()
        if not (code and y and q):
            continue
        rev = num(get(r, "營業收入", "淨收益", "收益合計", "Revenue"))
        gross = num(get(r, "營業毛利（毛損）淨額", "營業毛利（毛損）", "GrossProfit"))
        op = num(get(r, "營業利益（損失）", "OperatingIncome"))
        net = num(get(r, "本期淨利（淨損）", "本期稅後淨利（淨損）", "NetIncome"))
        pct = lambda a: r1(a / rev * 100) if (a is not None and rev) else None  # noqa: E731
        key = (y, q)
        if code in out and out[code]["_key"] >= key:
            continue
        out[code] = {"_key": key, "財報季度": f"{y}Q{q}", "毛利率": pct(gross), "營業利益率": pct(op),
                     "淨利率": pct(net), "累計EPS": r1(num(get(r, "基本每股盈餘（元）", "基本每股盈餘", "EPS")))}
    for v in out.values():
        v.pop("_key")
    return out


def dividend_fields(rows: list[dict]) -> dict[str, dict]:
    per: dict[str, dict[int, float]] = {}
    for r in rows:
        code = code_of(r)
        period = str(get(r, "股利所屬期間") or "")
        y = roc_year(period[:3]) if re.match(r"\d{7}", period) else roc_year(str(get(r, "股利年度") or ""))
        if not (code and y):
            continue
        cash = sum(num(get(r, k)) or 0 for k in (
            "股東配發-盈餘分配之現金股利(元/股)", "股東配發-法定盈餘公積發放之現金(元/股)",
            "股東配發-資本公積發放之現金(元/股)"))
        per.setdefault(code, {}).setdefault(y, 0.0)
        per[code][y] += cash
    out = {}
    for code, years in per.items():
        y = max(years)
        out[code] = {"現金股利": r1(years[y], 4), "股利所屬年度": y}
    return out


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, int):
        return str(v)
    return f'"{v}"'


def apply(note: Path, values: dict, now: dt.datetime) -> bool:
    text = note.read_text("utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    body = m.group(1) if m else ""
    lines = body.split("\n") if body else []
    existing = {ln.split(":", 1)[0].strip(): i for i, ln in enumerate(lines) if ":" in ln and not ln.startswith(" ")}
    changed = False
    for k, v in values.items():
        new = f"{k}: {fmt(v)}"
        if k in existing:
            if lines[existing[k]] != new:
                lines[existing[k]] = new
                changed = True
        else:
            lines.append(new)
            existing[k] = len(lines) - 1
            changed = True
    if not changed:
        return False
    stamp = f"財務更新: {now:%Y-%m-%d}"
    if "財務更新" in existing:
        lines[existing["財務更新"]] = stamp
    else:
        lines.append(stamp)
    fm = "---\n" + "\n".join(lines) + "\n---\n"
    write_text(note, fm + (text[m.end():] if m else text))
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".")
    ap.add_argument("--only", default=os.environ.get("ONLY_CODES", ""))
    args = ap.parse_args()
    vault = Path(args.vault).resolve()
    now = dt.datetime.now(TZ)
    only = {c.strip() for c in args.only.split(",") if c.strip()}

    companies = load_companies()
    notes = index_notes(vault)
    if companies:  # 代號相同但名稱對不上的（海外公司）略過
        notes = {c: p for c, p in notes.items()
                 if c in companies and (companies[c]["name"] in p.stem or p.stem[len(c):] in companies[c]["name"])}
    if only:
        notes = {c: p for c, p in notes.items() if c in only}

    rev = revenue_fields(load(REVENUE, "月營收"))
    inc = income_fields(load(INCOME, "綜合損益表"))
    div = dividend_fields(load(DIVIDEND, "股利"))
    print(f"月營收 {len(rev)} 家、損益表 {len(inc)} 家、股利 {len(div)} 家；筆記 {len(notes)} 份")

    updated = 0
    for code, note in notes.items():
        values = {}
        for src in (rev, inc, div):
            values.update({k: v for k, v in src.get(code, {}).items() if v is not None})
        if values and apply(note, values, now):
            updated += 1
    print(f"更新 {updated} 份筆記")
    for w in warnings:
        print(f"::warning::{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
