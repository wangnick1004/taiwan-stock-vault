"""產生 0050 總覽文的圖表（SVG）。

用法：在 vault 根目錄執行 `python3 scripts/0050總覽/make_charts.py _data/0050持股明細_YYYY-MM-DD.csv`
- 產業別取自各成分股筆記 frontmatter 的「產業」欄（即證交所上市產業分類）。
- ROIC／WACC 取自 統計/WACC與ROIC彙整.md（本庫個股筆記的推算值，#待校對）。
輸出到 0050總覽文/附件/，並在終端印出文章要用的統計數字。
"""
import csv
import glob
import re
import sys
from html import escape

OUT = "0050總覽文/附件"
FONT = "'PingFang TC','Noto Sans TC','Microsoft JhengHei','WenQuanYi Zen Hei',sans-serif"
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e6e4de", "#ffffff"
BLUE, ORANGE = "#2a78d6", "#eb6834"


def load(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        f = glob.glob(f"個股/台股/{r['代號']}*.md")
        assert len(f) == 1, (r["代號"], f)
        head = open(f[0], encoding="utf-8").read(3000)
        r["產業"] = re.search(r"^產業:\s*(\S+)", head, re.M).group(1)
        r["檔名"] = f[0].split("/")[-1][:-3]
        r["w"] = float(r["權重%"])
    return rows


def svg(w, h, body, title, note):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'font-family="{FONT}" font-size="13">\n'
        f'<rect width="{w}" height="{h}" fill="{SURF}"/>\n'
        f'<text x="24" y="34" font-size="18" font-weight="700" fill="{INK}">{escape(title)}</text>\n'
        + body
        + f'<text x="24" y="{h - 14}" font-size="11" fill="{MUTED}">{escape(note)}</text>\n</svg>\n'
    )


def hbar(items, title, note, fname, unit="%", xmax=None, label_w=150, sub=None):
    """水平長條圖：items = [(標籤, 數值, 右側附註)]，單一色相。"""
    W, top, rowh = 900, 64 if not sub else 80, 30
    H = top + rowh * len(items) + 56
    x0, x1 = 24 + label_w, W - 110
    xmax = xmax or max(v for _, v, _ in items)
    sx = lambda v: x0 + (x1 - x0) * v / xmax
    b = ""
    if sub:
        b += f'<text x="24" y="56" font-size="12" fill="{INK2}">{escape(sub)}</text>\n'
    step = 10 if xmax > 30 else (2 if xmax > 8 else 1)
    t = 0
    while t <= xmax + 1e-9:
        X = sx(t)
        b += f'<line x1="{X:.1f}" x2="{X:.1f}" y1="{top - 6}" y2="{top + rowh * len(items)}" stroke="{GRID}"/>\n'
        b += f'<text x="{X:.1f}" y="{top + rowh * len(items) + 18}" text-anchor="middle" font-size="11" fill="{INK2}">{t:g}{unit}</text>\n'
        t += step
    for i, (lab, v, extra) in enumerate(items):
        y = top + i * rowh
        bw = max(sx(v) - x0, 1)
        b += f'<text x="{x0 - 10}" y="{y + 19}" text-anchor="end" fill="{INK}">{escape(lab)}</text>\n'
        b += f'<rect x="{x0}" y="{y + 6}" width="{bw:.1f}" height="18" rx="4" fill="{BLUE}"><title>{escape(lab)} {v:.2f}{unit}</title></rect>\n'
        b += f'<text x="{x0 + bw + 8:.1f}" y="{y + 19}" fill="{INK}">{v:.2f}{unit}'
        if extra:
            b += f'<tspan fill="{INK2}" font-size="11">  {escape(extra)}</tspan>'
        b += "</text>\n"
    open(f"{OUT}/{fname}", "w", encoding="utf-8").write(svg(W, H, b, title, note))


def cumulative(rows, title, note, fname):
    W, H = 900, 480
    x0, x1, y0, y1 = 70, 860, 400, 70
    n = len(rows)
    sx = lambda i: x0 + (x1 - x0) * (i - 1) / (n - 1)
    sy = lambda v: y0 - (y0 - y1) * v / 100
    b = ""
    for t in range(0, 101, 20):
        b += f'<line x1="{x0}" x2="{x1}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" stroke="{GRID}"/>\n'
        b += f'<text x="{x0 - 8}" y="{sy(t) + 4:.1f}" text-anchor="end" font-size="11" fill="{INK2}">{t}%</text>\n'
    for i in (1, 10, 20, 30, 40, 50):
        b += f'<text x="{sx(i):.1f}" y="{y0 + 20}" text-anchor="middle" font-size="11" fill="{INK2}">第{i}大</text>\n'
    b += f'<text x="{(x0 + x1) / 2}" y="{y0 + 40}" text-anchor="middle" font-size="12" fill="{INK2}">成分股依權重排序</text>\n'
    # 等權重參考線
    b += f'<line x1="{sx(1):.1f}" y1="{sy(100 / n):.1f}" x2="{sx(n):.1f}" y2="{sy(100):.1f}" stroke="{MUTED}" stroke-width="1.5" stroke-dasharray="5 4"/>\n'
    b += f'<text x="{sx(30):.1f}" y="{sy(52):.1f}" font-size="12" fill="{INK2}">若 50 檔等權重（每檔 2%）</text>\n'
    cum, pts, c = [], [], 0
    for i, r in enumerate(rows, 1):
        c += r["w"]
        cum.append(c)
        pts.append(f"{sx(i):.1f},{sy(c):.1f}")
    b += f'<polyline points="{" ".join(pts)}" fill="none" stroke="{BLUE}" stroke-width="2"/>\n'
    for i, dx, dy in ((1, 10, 18), (3, 10, 20), (10, 0, -14), (20, 0, -14), (50, -6, -14)):
        X, Y = sx(i), sy(cum[i - 1])
        b += f'<circle cx="{X:.1f}" cy="{Y:.1f}" r="5" fill="{BLUE}" stroke="{SURF}" stroke-width="2"/>\n'
        anchor = "start" if i < 10 else ("end" if i == 50 else "middle")
        b += f'<text x="{X + dx:.1f}" y="{Y + dy:.1f}" text-anchor="{anchor}" font-size="12" fill="{INK}">前{i}大 {cum[i - 1]:.1f}%</text>\n'
    open(f"{OUT}/{fname}", "w", encoding="utf-8").write(svg(W, H, b, title, note))
    return cum


def spread_chart(items, title, note, fname):
    """ROIC − WACC 利差，雙色發散長條：正值藍、負值橙，數值一律標示正負號。"""
    W, top, rowh, label_w = 900, 80, 24, 130
    H = top + rowh * len(items) + 60
    lo = min(v for _, v, _ in items)
    hi = max(v for _, v, _ in items)
    lo, hi = min(-10, lo - 1), max(20, hi + 1)
    x0, x1 = 24 + label_w, W - 70
    sx = lambda v: x0 + (x1 - x0) * (v - lo) / (hi - lo)
    b = f'<text x="24" y="56" font-size="12" fill="{INK2}">ROIC − WACC（百分點）；藍＝ROIC 高於資金成本，橙＝低於資金成本。括號內為 ROIC／WACC。</text>\n'
    t = int(lo // 5 * 5)
    while t <= hi:
        X = sx(t)
        b += f'<line x1="{X:.1f}" x2="{X:.1f}" y1="{top - 4}" y2="{top + rowh * len(items)}" stroke="{INK2 if t == 0 else GRID}"/>\n'
        b += f'<text x="{X:.1f}" y="{top + rowh * len(items) + 18}" text-anchor="middle" font-size="11" fill="{INK2}">{t:+d}</text>\n'
        t += 5
    for i, (lab, v, extra) in enumerate(items):
        y = top + i * rowh
        a, z = sorted((sx(0), sx(v)))
        col = BLUE if v >= 0 else ORANGE
        b += f'<text x="{x0 - 10}" y="{y + 16}" text-anchor="end" font-size="12" fill="{INK}">{escape(lab)}</text>\n'
        b += f'<rect x="{a:.1f}" y="{y + 5}" width="{max(z - a, 1):.1f}" height="14" rx="3" fill="{col}"><title>{escape(lab)} {v:+.1f}</title></rect>\n'
        # 負值的數字標在零線右側，避免與左側公司名稱重疊
        tx, anc = (z + 6, "start") if v >= 0 else (sx(0) + 6, "start")
        b += f'<text x="{tx:.1f}" y="{y + 16}" text-anchor="{anc}" font-size="11" fill="{INK}">{v:+.1f} <tspan fill="{INK2}">({escape(extra)})</tspan></text>\n'
    open(f"{OUT}/{fname}", "w", encoding="utf-8").write(svg(W, H, b, title, note))


def roic_wacc(rows):
    table = {}
    for line in open("統計/WACC與ROIC彙整.md", encoding="utf-8"):
        m = re.match(r"\| \[\[(\d{4})[^\]]*\]\] \| [^|]+ \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", line)
        if m:
            table[m.group(1)] = [s.strip() for s in m.groups()[1:]]
    out = []
    for r in rows:
        if r["代號"] in table:
            roic, yr, wacc, spread = table[r["代號"]]
            out.append((r["名稱"], float(spread), f"{roic}%／{wacc}%"))
    return sorted(out, key=lambda x: -x[1])


def main():
    rows = load(sys.argv[1])
    date = re.search(r"(\d{4}-\d{2}-\d{2})", sys.argv[1]).group(1).replace("-", "/")
    src = f"資料來源：元大投信 0050 持股明細（經 MoneyDJ 理財網），持股日期 {date}；產業別為證交所上市產業分類。"

    ind = {}
    for r in rows:
        ind.setdefault(r["產業"], []).append(r)
    inds = sorted(ind.items(), key=lambda kv: -sum(r["w"] for r in kv[1]))
    hbar([(k, sum(r["w"] for r in v), f"{len(v)} 檔") for k, v in inds],
         "圖 1　0050 產業權重配置", src, "圖1_產業權重.svg", xmax=70)

    hbar([(f"{r['代號']} {r['名稱']}", r["w"], r["產業"]) for r in rows[:15]],
         "圖 2　0050 前 15 大成分股權重", src, "圖2_前15大權重.svg", xmax=60)

    cum = cumulative(rows, "圖 3　0050 權重集中度：累計權重曲線", src, "圖3_累計權重.svg")

    rw = roic_wacc(rows)
    spread_chart(rw, "圖 4　0050 非金融成分股：ROIC 與 WACC 利差（2025 年，推算值）",
                 "資料來源：本庫個股筆記推算值，彙整於 統計/WACC與ROIC彙整.md（#待校對）。金融股不適用；未列者為筆記未提供數值。",
                 "圖4_ROIC_WACC利差.svg")

    tot = sum(r["w"] for r in rows)
    hhi = sum((r["w"] / tot) ** 2 for r in rows)
    rest = [r["w"] for r in rows[1:]]
    hhi_rest = sum((w / sum(rest)) ** 2 for w in rest)
    print(f"股票合計 {tot:.2f}%  HHI {hhi:.4f}  有效檔數 {1 / hhi:.2f}  扣除第一大後有效檔數 {1 / hhi_rest:.1f}")
    for i in (1, 3, 5, 10, 20, 30):
        print(f"前{i}大 {cum[i - 1]:.2f}%")
    for k, v in inds:
        print(k, f"{sum(r['w'] for r in v):.2f}", len(v), "、".join(r["名稱"] for r in v))
    pos = [x for x in rw if x[1] > 0]
    print(f"ROIC/WACC 有數值 {len(rw)} 檔，正利差 {len(pos)} 檔")
    for x in rw:
        print(x)


if __name__ == "__main__":
    main()
