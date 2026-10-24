"""媒體關注度的集中度，以及關注度與規模、成長的關係（股市統計討論用）。

用法（vault 根目錄）：
  python3 系統/scripts/統計/關注度.py --out 統計/每週統計/附件 --prefix 2026-10-24 [--start 2026-10-05 --end 2026-10-23]
資料：
- 新聞：新聞/<代號名稱> 新聞.md（update_news.py 產生）。只用「全體公司一致收集」的兩種來源：
  鉅亨網（news.cnyes.com，依公司名稱比對）與公開資訊觀測站重大訊息。Google 新聞只替關注清單內的公司抓，
  收集方式不一致，主分析排除，只在穩健性檢查使用。同一公司同一標題只算一次。
- 營收：系統/資料/財務摘要.json（9 月營收與年增率）。
母體：有個股筆記（個股/台股）且有當月營收的公司；新聞檔只會替有筆記的公司建立，沒有新聞檔的公司視為 0 則。
"""
import argparse
import glob
import json
import math
import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(__file__))
from svgkit import Svg, scale, INK, INK2, MUTED, GRID, BLUE, ORANGE

REV = re.compile(r"營收|業績|營業額")  # 月營收、季營收報導
LINE = re.compile(r"^- (\S+) (重訊|媒體) (\d{2}:\d{2})?｜(.+?)｜\[([^\]]*)\]\((\S+?)\)")


def notes():
    out = {}
    for f in glob.glob("個股/台股/*.md"):
        head = open(f, encoding="utf-8").read(1500)
        c = re.search(r'^代號:\s*"?([0-9A-Z]+)"?', head, re.M)
        i = re.search(r"^產業:\s*(\S+)", head, re.M)
        if c:
            out[c.group(1)] = dict(name=os.path.basename(f)[:-3], ind=i.group(1) if i else "未分類")
    return out


def news_counts(start, end):
    res = {}
    for f in glob.glob("新聞/*新聞.md"):
        txt = open(f, encoding="utf-8").read()
        c = re.search(r'^代號:\s*"?([0-9A-Z]+)"?', txt, re.M)
        if not c:
            continue
        day, seen = None, {"cnyes": set(), "mops": set(), "google": set(), "cnyes_nonrev": set()}
        for ln in txt.splitlines():
            if ln.startswith("## 20"):
                day = ln[3:13]
                continue
            m = LINE.match(ln)
            if not m or not day or not (start <= day <= end):
                continue
            title, url = m.group(4).strip(), m.group(6)
            src = "mops" if m.group(2) == "重訊" else ("cnyes" if "cnyes.com" in url else "google")
            seen[src].add(title)
            if src == "cnyes" and not REV.search(title):
                seen["cnyes_nonrev"].add(title)
        res[c.group(1)] = {k: len(v) for k, v in seen.items()}
    return res


def gini(xs):
    xs = sorted(xs)
    n, s = len(xs), sum(xs)
    if s == 0:
        return 0
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs)) / (n * s)


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a, b):
    ma, mb = st.mean(a), st.mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))


def spearman(a, b):
    return pearson(ranks(a), ranks(b))


def partial(rxy, rxz, ryz):
    return (rxy - rxz * ryz) / math.sqrt((1 - rxz ** 2) * (1 - ryz ** 2))


def z_p(r, n, k=0):
    """Fisher z 檢定相關係數（k 為控制變數數），回傳雙尾 p。"""
    z = 0.5 * math.log((1 + r) / (1 - r)) * math.sqrt(n - 3 - k)
    return math.erfc(abs(z) / math.sqrt(2))


def prop_ci(k, n):
    """Wilson 95% 信賴區間。"""
    z = 1.96
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - h, c + h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--start", default="2026-10-05")
    ap.add_argument("--end", default="2026-10-23")
    a = ap.parse_args()
    fin = json.load(open("系統/資料/財務摘要.json", encoding="utf-8"))
    nt = notes()
    nc = news_counts(a.start, a.end)
    rows = []
    for code, v in fin["公司"].items():
        if code not in nt or v.get("月營收") is None or v.get("月營收年增率") is None or v["月營收"] <= 0:
            continue
        c = nc.get(code, {"cnyes": 0, "mops": 0, "google": 0, "cnyes_nonrev": 0})
        rows.append(dict(code=code, name=nt[code]["name"], ind=nt[code]["ind"], rev=float(v["月營收"]),
                         yoy=float(v["月營收年增率"]), cum=v.get("累計營收年增率"), **c))
    n = len(rows)
    cn = [r["cnyes"] for r in rows]
    mo = [r["mops"] for r in rows]
    tot = sum(cn)
    srt = sorted(rows, key=lambda r: -r["cnyes"])
    top1pct = max(1, n // 100)
    hhi = sum((x / tot) ** 2 for x in cn)
    lrev = [math.log(r["rev"]) for r in rows]
    yoy = [r["yoy"] for r in rows]
    r_size = spearman(cn, lrev)
    r_yoy = spearman(cn, yoy)
    r_size_yoy = spearman(lrev, yoy)
    r_part = partial(r_yoy, r_size, r_size_yoy)
    r_mops = spearman(cn, mo)
    # 規模五分位
    by = sorted(rows, key=lambda r: r["rev"])
    quint = []
    for i in range(5):
        part = by[i * n // 5:(i + 1) * n // 5]
        k = sum(r["cnyes"] > 0 for r in part)
        hi = [r for r in part if r["yoy"] >= 30]
        lo = [r for r in part if r["yoy"] < 30]
        quint.append(dict(q=i + 1, n=len(part), covered=k / len(part) * 100, ci=[x * 100 for x in prop_ci(k, len(part))],
                          mean=st.mean(r["cnyes"] for r in part), median=st.median(r["cnyes"] for r in part),
                          rev_lo=part[0]["rev"], rev_hi=part[-1]["rev"],
                          hi_n=len(hi), hi_cov=(sum(r["cnyes"] > 0 for r in hi) / len(hi) * 100) if hi else None,
                          lo_n=len(lo), lo_cov=(sum(r["cnyes"] > 0 for r in lo) / len(lo) * 100) if lo else None))
    # 產業：新聞占比 vs 營收占比
    inds = {}
    for r in rows:
        inds.setdefault(r["ind"], []).append(r)
    trev = sum(r["rev"] for r in rows)
    ind = []
    for i, rs in inds.items():
        if len(rs) < 15 or i == "未分類":
            continue
        ind.append(dict(ind=i, n=len(rs), news=sum(r["cnyes"] for r in rs) / tot * 100,
                        rev=sum(r["rev"] for r in rs) / trev * 100, firms=len(rs) / n * 100,
                        covered=sum(r["cnyes"] > 0 for r in rs) / len(rs) * 100))
    ind.sort(key=lambda t: -t["news"])
    # 資訊落差候選：規模不小、成長高且持續，但視窗內鉅亨網 0 則
    gap = [r for r in rows if r["rev"] >= 5 and r["yoy"] >= 50 and r["cum"] is not None and float(r["cum"]) >= 30]
    gap0 = sorted([r for r in gap if r["cnyes"] == 0], key=lambda r: -r["rev"])
    # 穩健性：含 Google 新聞
    allm = [r["cnyes"] + r["google"] for r in rows]
    nr = [r["cnyes_nonrev"] for r in rows]
    r_nr_size, r_nr_yoy = spearman(nr, lrev), spearman(nr, yoy)
    nonrev = dict(total=sum(nr), share_of_cnyes=sum(nr) / tot * 100, zero_share=sum(x == 0 for x in nr) / n * 100,
                  gini=gini(nr), rho_size=r_nr_size, rho_yoy=r_nr_yoy,
                  rho_yoy_given_size=partial(r_nr_yoy, r_nr_size, r_size_yoy))
    for t, i in zip(quint, range(5)):
        part = by[i * n // 5:(i + 1) * n // 5]
        hi = [r for r in part if r["yoy"] >= 30]; lo = [r for r in part if r["yoy"] < 30]
        t["hi_cov_nonrev"] = sum(r["cnyes_nonrev"] > 0 for r in hi) / len(hi) * 100 if hi else None
        t["lo_cov_nonrev"] = sum(r["cnyes_nonrev"] > 0 for r in lo) / len(lo) * 100 if lo else None
    out = dict(nonrev=nonrev, window=[a.start, a.end], fetched=fin["抓取日期"], n=n, cnyes_total=tot, mops_total=sum(mo),
               google_total=sum(r["google"] for r in rows),
               zero_share=sum(x == 0 for x in cn) / n * 100, zero_share_mops=sum(x == 0 for x in mo) / n * 100,
               gini=gini(cn), gini_mops=gini(mo), gini_all_media=gini(allm),
               top10_share=sum(r["cnyes"] for r in srt[:10]) / tot * 100,
               top1pct_n=top1pct, top1pct_share=sum(r["cnyes"] for r in srt[:top1pct]) / tot * 100,
               eff_n=1 / hhi, top10=[(r["name"], r["cnyes"], r["mops"], round(r["rev"], 1)) for r in srt[:10]],
               rho_size=r_size, p_size=z_p(r_size, n), rho_yoy=r_yoy, p_yoy=z_p(r_yoy, n),
               rho_size_yoy=r_size_yoy, rho_yoy_given_size=r_part, p_part=z_p(r_part, n, 1),
               rho_mops=r_mops, rho_size_all_media=spearman(allm, lrev),
               rho_yoy_given_size_all=partial(spearman(allm, yoy), spearman(allm, lrev), r_size_yoy),
               quintiles=quint, industries=ind, gap_n=len(gap), gap0_n=len(gap0),
               gap0=[(r["name"], r["ind"], round(r["rev"], 1), r["yoy"], r["cum"], r["mops"]) for r in gap0[:12]])
    os.makedirs(a.out, exist_ok=True)
    P = f"{a.out}/{a.prefix}"
    chart_lorenz(rows, out, f"{P}_圖1_關注度洛倫茲曲線.svg")
    chart_quint(quint, out, f"{P}_圖2_規模與報導覆蓋.svg")
    chart_ind(ind, out, f"{P}_圖3_產業新聞與營收占比.svg")
    chart_growth(quint, out, f"{P}_圖4_成長與報導覆蓋.svg")
    chart_growth(quint, out, f"{P}_圖5_排除營收快訊.svg", "hi_cov_nonrev", "lo_cov_nonrev",
                 "排除營收報導後，高成長公司的報導優勢大幅縮小",
                 "同圖 4，但只計算標題不含「營收、業績、營業額」的鉅亨網報導")
    print(json.dumps(out, ensure_ascii=False, indent=1))


def SRC(s):
    return f"資料：鉅亨網個股新聞與公開資訊觀測站重大訊息（本庫 新聞/，{s['window'][0]} 至 {s['window'][1]}）；證交所、櫃買中心月營收。"


def chart_lorenz(rows, s, path):
    W, H = 900, 560
    L, R, T, B = 80, 40, 90, 70
    g = Svg(W, H)
    g.title("媒體報導高度集中在少數公司", f"洛倫茲曲線：公司依報導則數由少到多排列；鉅亨網吉尼係數 {s['gini']:.2f}，重大訊息 {s['gini_mops']:.2f}")
    x = scale(0, 100, L, W - R)
    y = scale(0, 100, H - B, T)
    for t in range(0, 101, 20):
        g.line(L, y(t), W - R, y(t))
        g.text(L - 8, y(t) + 4, f"{t}%", size=12, anchor="end")
        g.text(x(t), H - B + 20, f"{t}%", size=12, anchor="middle")
    g.line(x(0), y(0), x(100), y(100), stroke=MUTED, dash="5 4")
    g.text(x(52), y(58), "完全平均", size=12, fill=MUTED)
    for key, col, lab in (("mops", ORANGE, "重大訊息"), ("cnyes", BLUE, "鉅亨網新聞")):
        xs = sorted(r[key] for r in rows)
        tot = sum(xs)
        acc, pts = 0, [(0, 0)]
        for i, v in enumerate(xs):
            acc += v
            pts.append(((i + 1) / len(xs) * 100, acc / tot * 100))
        step = max(1, len(pts) // 400)
        pts = pts[::step] + [pts[-1]]
        g.path("M" + " L".join(f"{x(a):.1f},{y(b):.1f}" for a, b in pts), stroke=col, width=2.5)
    g.circle(L + 20, T + 10, 5, fill=BLUE); g.text(L + 32, T + 14, "鉅亨網新聞", size=12, fill=INK)
    g.circle(L + 20, T + 32, 5, fill=ORANGE); g.text(L + 32, T + 36, "重大訊息", size=12, fill=INK)
    g.text(W - R, H - B + 40, "公司累計比例（報導由少到多）", size=12, anchor="end")
    g.text(L - 8, T - 14, "報導則數累計比例", size=12, anchor="start")
    g.source(SRC(s))
    g.save(path)


def chart_quint(qs, s, path):
    W, H = 900, 500
    L, R, T, B = 70, 30, 90, 100
    g = Svg(W, H)
    g.title("營收規模越大，被報導的機率越高", "依 9 月營收由小到大分五組，每組家數相同；柱高為觀察期間至少有 1 則鉅亨網報導的公司比例，細線為 95% 信賴區間")
    y = scale(0, 100, H - B, T)
    for t in range(0, 101, 20):
        g.line(L, y(t), W - R, y(t), stroke=GRID if t else MUTED)
        g.text(L - 8, y(t) + 4, f"{t}%", size=12, anchor="end")
    bw = (W - L - R) / 5
    for i, t in enumerate(qs):
        cx = L + bw * i + bw / 2
        g.rect(cx - 32, y(t["covered"]), 64, y(0) - y(t["covered"]), fill=BLUE, rx=3)
        g.line(cx, y(t["ci"][0]), cx, y(t["ci"][1]), stroke=INK, width=1.5)
        g.text(cx + 40, y(t["covered"]) + 4, f"{t['covered']:.1f}%", size=12, fill=INK)
        lab = ["最小", "第二", "第三", "第四", "最大"][i] + " 20%"
        g.text(cx, H - B + 22, lab, size=12, fill=INK, anchor="middle")
        g.text(cx, H - B + 40, f"營收 {t['rev_lo']:.1f}～{t['rev_hi']:,.1f} 億" if t["rev_hi"] < 100 else f"營收 {t['rev_lo']:.1f} 億以上", size=11, anchor="middle")
        g.text(cx, H - B + 58, f"平均 {t['mean']:.2f} 則", size=11, anchor="middle")
    g.source(SRC(s))
    g.save(path)


def chart_ind(ind, s, path):
    W = 900
    rowh = 26
    T, B, L, R = 110, 70, 170, 40
    H = T + B + rowh * len(ind)
    g = Svg(W, H)
    g.title("各產業的新聞占比與營收占比", "藍點為該產業占全體鉅亨網報導則數的比例，橘點為占全體 9 月營收的比例；只列 15 家以上的產業")
    hi = max(max(t["news"] for t in ind), max(t["rev"] for t in ind)) * 1.08
    x = scale(0, hi, L, W - R)
    for t in range(0, int(hi) + 1, 5):
        g.line(x(t), T - 10, x(t), H - B, stroke=GRID if t else MUTED)
        g.text(x(t), H - B + 18, f"{t}%", size=12, anchor="middle")
    g.circle(L, T - 30, 5, fill=BLUE); g.text(L + 10, T - 26, "新聞占比", size=12, fill=INK)
    g.circle(L + 100, T - 30, 5, fill=ORANGE); g.text(L + 110, T - 26, "營收占比", size=12, fill=INK)
    for i, t in enumerate(ind):
        yy = T + rowh * i + rowh / 2
        g.text(L - 10, yy + 4, f"{t['ind']}（{t['n']}）", size=12, fill=INK, anchor="end")
        g.line(x(t["news"]), yy, x(t["rev"]), yy, stroke=GRID, width=3)
        g.circle(x(t["rev"]), yy, 5, fill=ORANGE)
        g.circle(x(t["news"]), yy, 5, fill=BLUE)
    g.source(SRC(s))
    g.save(path)


def chart_growth(qs, s, path, hk="hi_cov", lk="lo_cov", title=None, sub=None):
    W, H = 900, 500
    L, R, T, B = 70, 30, 100, 90
    g = Svg(W, H)
    g.title(title or "同一規模組內，高成長公司被報導的比例高出許多",
            sub or "每個規模組內，比較 9 月營收年增 30% 以上（藍）與未達 30%（灰）的公司，至少有 1 則鉅亨網報導的比例")
    y = scale(0, 100, H - B, T)
    for t in range(0, 101, 20):
        g.line(L, y(t), W - R, y(t), stroke=GRID if t else MUTED)
        g.text(L - 8, y(t) + 4, f"{t}%", size=12, anchor="end")
    g.rect(W - R - 230, T - 34, 12, 12, fill=BLUE); g.text(W - R - 212, T - 24, "年增 ≥30%", size=12, fill=INK)
    g.rect(W - R - 120, T - 34, 12, 12, fill=MUTED); g.text(W - R - 102, T - 24, "年增 <30%", size=12, fill=INK)
    bw = (W - L - R) / 5
    for i, t in enumerate(qs):
        cx = L + bw * i + bw / 2
        for off, val, col, nn in ((-30, t[hk], BLUE, t["hi_n"]), (6, t[lk], MUTED, t["lo_n"])):
            if val is None:
                continue
            g.rect(cx + off, y(val), 24, y(0) - y(val), fill=col, rx=3)
            g.text(cx + off + 12, y(val) - 6, f"{val:.0f}%", size=11, fill=INK, anchor="middle")
            g.text(cx + off + 12, H - B + 40, f"n={nn}", size=10, anchor="middle")
        lab = ["最小", "第二", "第三", "第四", "最大"][i] + " 20%"
        g.text(cx, H - B + 22, lab, size=12, fill=INK, anchor="middle")
    g.source(SRC(s))
    g.save(path)


if __name__ == "__main__":
    main()
