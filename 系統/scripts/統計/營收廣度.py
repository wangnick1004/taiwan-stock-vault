"""月營收年增率的分布、廣度與成長集中度（股市統計討論用）。

用法（vault 根目錄）：
  python3 系統/scripts/統計/營收廣度.py --out 統計/每週統計/附件 --prefix 2026-10-23
資料：系統/資料/財務摘要.json（證交所、櫃買中心 OpenAPI 月營收，由 update_financials.py 抓取）。
產業：個股筆記 frontmatter「產業」，沒有筆記的公司用 系統/資料/公司資料/*_每月營收.json 的產業別。
排除：金融保險業（營收定義與一般業不同）、當月營收或年增率缺值、去年同月營收為 0。
輸出四張 SVG，並在終端印出文章要用的統計數字（JSON）。
"""
import argparse
import glob
import json
import random
import re
import statistics as st
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from svgkit import Svg, scale, INK, INK2, MUTED, GRID, BLUE, ORANGE, RED

FIN = {"金融保險業", "金融保險", "金融業"}


def industries():
    ind = {}
    for f in glob.glob("系統/資料/公司資料/*_每月營收.json"):
        for r in json.load(open(f, encoding="utf-8")):
            ind[r["公司代號"]] = r["產業別"]
    names = {}
    for f in glob.glob("個股/台股/*.md"):
        head = open(f, encoding="utf-8").read(1500)
        c = re.search(r'^代號:\s*"?([0-9A-Z]+)"?', head, re.M)
        i = re.search(r"^產業:\s*(\S+)", head, re.M)
        if c:
            names[c.group(1)] = os.path.basename(f)[:-3]
            if i:
                ind[c.group(1)] = i.group(1)
    return ind, names


def load(month):
    d = json.load(open("系統/資料/財務摘要.json", encoding="utf-8"))
    ind, names = industries()
    rows = []
    for code, v in d["公司"].items():
        if v.get("月營收年月") != month or v.get("月營收") is None or v.get("月營收年增率") is None:
            continue
        i = ind.get(code, "未分類")
        if i in FIN:
            continue
        rev, yoy = float(v["月營收"]), float(v["月營收年增率"])
        if yoy <= -100:
            continue
        prev = rev / (1 + yoy / 100)
        rows.append(dict(code=code, name=names.get(code, code), ind=i, rev=rev, yoy=yoy, prev=prev,
                         cum=v.get("累計營收年增率")))
    return d["抓取日期"], rows


def q(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


def boot_median(xs, n=2000, seed=7):
    rnd = random.Random(seed)
    m = [st.median(rnd.choices(xs, k=len(xs))) for _ in range(n)]
    return q(m, 0.025), q(m, 0.975)


def ztest(a, b):
    """兩組正成長比例的雙尾 z 檢定，回傳 (z, p)。"""
    import math
    p1, p2, n1, n2 = a["breadth"] / 100, b["breadth"] / 100, a["n"], b["n"]
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    z = (p2 - p1) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return z, math.erfc(abs(z) / math.sqrt(2))


def agg(rows):
    return (sum(r["rev"] for r in rows) / sum(r["prev"] for r in rows) - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prefix", required=True)
    a = ap.parse_args()
    d = json.load(open("系統/資料/財務摘要.json", encoding="utf-8"))
    month = a.month or max(v["月營收年月"] for v in d["公司"].values() if v.get("月營收年月"))
    fetched, rows = load(month)
    os.makedirs(a.out, exist_ok=True)
    ys = [r["yoy"] for r in rows]
    n = len(rows)
    med = st.median(ys)
    lo, hi = boot_median(ys)
    srt = sorted(ys)
    k = int(n * 0.1)
    trimmed = st.mean(srt[k:n - k])
    tsmc = [r for r in rows if r["code"] == "2330"]
    rest = [r for r in rows if r["code"] != "2330"]
    drev = sorted(rows, key=lambda r: r["rev"] - r["prev"], reverse=True)
    total_d = sum(r["rev"] - r["prev"] for r in rows)
    pos_d = sum(max(r["rev"] - r["prev"], 0) for r in rows)
    cum = []
    acc = 0
    for r in drev:
        acc += r["rev"] - r["prev"]
        cum.append(acc / total_d * 100)
    # 規模五分位
    by_size = sorted(rows, key=lambda r: r["prev"])
    quint = []
    for i in range(5):
        part = by_size[i * n // 5:(i + 1) * n // 5]
        quint.append(dict(q=i + 1, n=len(part), median=st.median(r["yoy"] for r in part),
                          breadth=sum(r["yoy"] > 0 for r in part) / len(part) * 100,
                          agg=agg(part), min_prev=min(r["prev"] for r in part), max_prev=max(r["prev"] for r in part)))
    # 產業
    inds = {}
    for r in rows:
        inds.setdefault(r["ind"], []).append(r)
    ind_stats = []
    for i, rs in inds.items():
        if len(rs) < 15 or i == "未分類":
            continue
        yy = [r["yoy"] for r in rs]
        ind_stats.append(dict(ind=i, n=len(rs), median=st.median(yy), q1=q(yy, .25), q3=q(yy, .75),
                              breadth=sum(y > 0 for y in yy) / len(yy) * 100, agg=agg(rs)))
    ind_stats.sort(key=lambda s: s["median"], reverse=True)
    cums = [float(r["cum"]) for r in rows if r["cum"] is not None]
    out = dict(month=month, fetched=fetched, n=n, median=med, ci=[lo, hi], mean=st.mean(ys), trimmed10=trimmed,
               q1=q(ys, .25), q3=q(ys, .75), breadth=sum(y > 0 for y in ys) / n * 100,
               over20=sum(y >= 20 for y in ys) / n * 100, under_m20=sum(y <= -20 for y in ys) / n * 100,
               agg=agg(rows), agg_ex_tsmc=agg(rest), tsmc_yoy=tsmc[0]["yoy"] if tsmc else None,
               tsmc_share_rev=(tsmc[0]["rev"] / sum(r["rev"] for r in rows) * 100) if tsmc else None,
               top1_share_d=cum[0], top10_share_d=cum[9], top50_share_d=cum[49], pos_d=pos_d, total_d=total_d,
               top10=[(r["name"], round(r["rev"] - r["prev"], 1), r["yoy"]) for r in drev[:10]],
               n_needed_100=next(i + 1 for i, c in enumerate(cum) if c >= 100),
               cum_median=st.median(cums), cum_breadth=sum(c > 0 for c in cums) / len(cums) * 100,
               breadth_z=ztest(quint[0], quint[4]),
               quintiles=quint, industries=ind_stats, extreme=sum(abs(y) > 100 for y in ys))
    # 毛利近似：以最新一季毛利率乘上營收年增金額（假設兩期毛利率相同，只是粗略換算）
    gm = {c: v.get("毛利率") for c, v in d["公司"].items()}
    gpd = [(r["name"], (r["rev"] - r["prev"]) * gm[r["code"]] / 100) for r in rows if gm.get(r["code"]) is not None]
    gtot = sum(x for _, x in gpd)
    gpd.sort(key=lambda t: -t[1])
    acc, gtop = 0, []
    for nm, x in gpd[:10]:
        acc += x
        gtop.append((nm, round(x, 1), round(x / gtot * 100, 1), round(acc / gtot * 100, 1)))
    out["gp_total"], out["gp_n"], out["gp_top10"] = gtot, len(gpd), gtop
    out["rev_top10"] = [(r["name"], round(r["rev"] - r["prev"], 1), round((r["rev"] - r["prev"]) / total_d * 100, 1), r["yoy"], gm.get(r["code"])) for r in drev[:10]]
    P = f"{a.out}/{a.prefix}"
    chart_hist(ys, med, out, f"{P}_圖1_年增率分布.svg")
    chart_cum(cum, out, f"{P}_圖2_成長集中度.svg")
    chart_ind(ind_stats, med, out, f"{P}_圖3_產業年增率.svg")
    chart_quint(quint, out, f"{P}_圖4_規模分組.svg")
    print(json.dumps(out, ensure_ascii=False, indent=1))


SRC = "資料：證交所、櫃買中心 OpenAPI 每月營收（本庫 系統/資料/財務摘要.json）；排除金融保險業。"


def ym(m):
    y, mm = m.split("-")
    return f"{y} 年 {int(mm)} 月"


def chart_hist(ys, med, s, path):
    W, H = 900, 520
    L, R, T, B = 70, 30, 90, 70
    g = Svg(W, H)
    g.title(f"{ym(s['month'])}營收年增率分布（{s['n']:,} 家上市櫃公司）",
            f"中位數 {med:.1f}%，{s['breadth']:.0f}% 的公司年增率為正；兩端超過 ±60% 的公司併入最外側組距")
    edges = list(range(-60, 101, 10))
    cnt = [0] * (len(edges) - 1)
    for y in ys:
        v = min(max(y, -59.99), 99.99)
        cnt[int((v + 60) // 10)] += 1
    x = scale(-60, 100, L, W - R)
    ymax = max(cnt) * 1.1
    y = scale(0, ymax, H - B, T)
    for t in range(0, int(ymax) + 1, 100):
        g.line(L, y(t), W - R, y(t))
        g.text(L - 8, y(t) + 4, t, size=12, anchor="end")
    for i, c in enumerate(cnt):
        x0, x1 = x(edges[i]) + 1, x(edges[i + 1]) - 1
        g.rect(x0, y(c), x1 - x0, y(0) - y(c), fill=BLUE if edges[i] >= 0 else ORANGE, rx=3)
    for e in edges[::2]:
        lab = f"{e}%" if -60 < e < 100 else ("≤−60%" if e == -60 else "≥100%")
        g.text(x(e), H - B + 20, lab.replace("-", "−"), size=12, anchor="middle")
    g.line(x(med), T - 6, x(med), H - B, stroke=INK, width=1.5, dash="5 4")
    g.text(x(med) + 6, T + 6, f"中位數 {med:.1f}%", size=12, fill=INK)
    g.line(L, y(0), W - R, y(0), stroke=MUTED)
    g.text(W - R, H - B + 40, "月營收年增率（組距 10 個百分點）", size=12, anchor="end")
    g.text(L - 8, T - 14, "家數", size=12, anchor="end")
    g.source(SRC)
    g.save(path)


def chart_cum(cum, s, path):
    W, H = 900, 520
    L, R, T, B = 70, 40, 90, 70
    g = Svg(W, H)
    g.title("營收年增金額由少數公司貢獻",
            f"依年增金額由大到小排序的累計貢獻；前 10 家合計占全體淨增加額 {s['top10_share_d']:.0f}%")
    n = 200
    x = scale(0, n, L, W - R)
    top = max(cum[:n]) * 1.08
    y = scale(0, top, H - B, T)
    for t in range(0, int(top) + 1, 20):
        g.line(L, y(t), W - R, y(t), stroke=GRID if t != 100 else MUTED, dash=None if t != 100 else "5 4")
        g.text(L - 8, y(t) + 4, f"{t}%", size=12, anchor="end")
    g.text(W - R, y(100) - 6, "100% ＝ 全體淨增加額", size=12, anchor="end")
    d = "M" + " L".join(f"{x(i + 1):.1f},{y(c):.1f}" for i, c in enumerate(cum[:n]))
    g.path(d, stroke=BLUE, width=2.5)
    for k in (1, 10, 50):
        g.circle(x(k), y(cum[k - 1]), 5, fill=BLUE)
        g.text(x(k) + 8, y(cum[k - 1]) + 16, f"前 {k} 家 {cum[k - 1]:.0f}%", size=12, fill=INK)
    for t in (1, 50, 100, 150, 200):
        g.text(x(t), H - B + 20, t, size=12, anchor="middle")
    g.text(W - R, H - B + 40, "公司數（依年增金額排序）", size=12, anchor="end")
    g.source(SRC + " 曲線超過 100% 代表其餘公司合計為負成長。")
    g.save(path)


def chart_ind(stats, med, s, path):
    W = 900
    rowh = 26
    T, B, L, R = 90, 70, 170, 150
    H = T + B + rowh * len(stats)
    g = Svg(W, H)
    g.title("各產業年增率中位數與四分位距", f"橫線為第 1 至第 3 四分位，圓點為中位數；只列 15 家以上的產業；右欄為年增率為正的公司比例")
    lo = min(-40, min(t["q1"] for t in stats))
    hi = min(100, max(60, max(t["q3"] for t in stats)))
    x = scale(lo, hi, L, W - R)
    for t in range(-40, int(hi) + 1, 20):
        g.line(x(t), T - 10, x(t), H - B, stroke=GRID if t else MUTED)
        g.text(x(t), H - B + 18, f"{t}%".replace("-", "−"), size=12, anchor="middle")
    g.line(x(med), T - 10, x(med), H - B, stroke=INK, dash="5 4")
    g.text(x(med) + 4, T - 14, f"全體中位數 {med:.1f}%", size=11, fill=INK)
    g.text(W - R + 70, T - 14, "正成長比例", size=11, anchor="middle")
    for i, t in enumerate(stats):
        yy = T + rowh * i + rowh / 2
        g.text(L - 10, yy + 4, f"{t['ind']}（{t['n']}）", size=12, fill=INK, anchor="end")
        g.line(x(max(t["q1"], lo)), yy, x(min(t["q3"], hi)), yy, stroke=BLUE, width=3)
        g.circle(x(t["median"]), yy, 5, fill=BLUE if t["median"] >= 0 else ORANGE)
        g.text(W - R + 70, yy + 4, f"{t['breadth']:.0f}%", size=12, anchor="middle")
    g.source(SRC)
    g.save(path)


def chart_quint(qs, s, path):
    W, H = 900, 480
    L, R, T, B = 70, 30, 90, 90
    g = Svg(W, H)
    g.title("規模最大的一組，正成長比例明顯較高",
            "依去年同月營收由小到大分五組，每組家數相同；柱高為組內年增率為正的公司比例，柱下為組內年增率中位數")
    y = scale(0, 100, H - B, T)
    for t in range(0, 101, 20):
        g.line(L, y(t), W - R, y(t), stroke=GRID if t else MUTED)
        g.text(L - 8, y(t) + 4, f"{t}%", size=12, anchor="end")
    bw = (W - L - R) / 5
    for i, t in enumerate(qs):
        cx = L + bw * i + bw / 2
        g.rect(cx - 32, y(t["breadth"]), 64, y(0) - y(t["breadth"]), fill=BLUE, rx=3)
        g.text(cx, y(t["breadth"]) - 8, f"{t['breadth']:.1f}%", size=12, fill=INK, anchor="middle")
        lab = ["最小", "第二", "第三", "第四", "最大"][i] + " 20%"
        g.text(cx, H - B + 22, lab, size=12, fill=INK, anchor="middle")
        g.text(cx, H - B + 40, f"中位數 {t['median']:.1f}%", size=11, anchor="middle")
    g.source(SRC)
    g.save(path)


if __name__ == "__main__":
    main()
