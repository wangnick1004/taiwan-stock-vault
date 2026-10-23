"""離線測試：假資料驗證異常訊號篩選的規則、去重與不重報。"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/訊號篩選/screen.py"
vault = Path(tempfile.mkdtemp())
(vault / "_data").mkdir()
shutil.copy(ROOT / "_data/訊號規則.json", vault / "_data/訊號規則.json")
(vault / "_data/0050成分股.txt").write_text("# 測試\n2330台積電\n", "utf-8")
(vault / "個股/台股").mkdir(parents=True)
for code, name, ind in [("2330", "台積電", "半導體業"), ("1101", "台泥", "水泥工業"), ("4113", "聯上", "建材營造業"),
                        ("2303", "聯電", "半導體業"), ("3037", "欣興", "電子零組件業"), ("8046", "南電", "電子零組件業")]:
    (vault / f"個股/台股/{code}{name}.md").write_text(f"---\n代號: \"{code}\"\n產業: {ind}\n---\n# {code}\n", "utf-8")

# 財務摘要：9 月；8 月放在原始月營收檔，模擬補歷史
(vault / "_data/財務摘要.json").write_text(json.dumps({"抓取日期": "2026-10-21", "公司": {
    "2330": {"月營收年月": "2026-09", "月營收": 5000, "月營收年增率": 60, "月營收月增率": 1, "累計營收年增率": 40},
    "1101": {"月營收年月": "2026-09", "月營收": 140, "月營收年增率": 8, "月營收月增率": 5, "累計營收年增率": 1},
    "4113": {"月營收年月": "2026-09", "月營收": 18, "月營收年增率": 3425111, "月營收月增率": 5, "累計營收年增率": 900},
    "2303": {"月營收年月": "2026-09", "月營收": 250, "月營收年增率": 1, "月營收月增率": 1, "累計營收年增率": 10},
}}, ensure_ascii=False), "utf-8")
(vault / "_data/公司資料").mkdir()
(vault / "_data/公司資料/上市_每月營收.json").write_text(json.dumps([
    {"資料年月": "11508", "公司代號": c, "營業收入-當月營收": "1000000", "營業收入-去年同月增減(%)": y,
     "營業收入-上月比較增減(%)": "0", "累計營業收入-前期比較增減(%)": cy}
    for c, y, cy in [("2330", "55", "38"), ("1101", "-6", "-1"), ("2303", "40", "11")]]), "utf-8")

TPEX_DUP = "【零股排行榜】盤中零股成交量TOP 20｜0050 元大台灣50、2330 台積電、2303 聯電"
news = {
    "2330台積電": ["- 🏛️ 重訊 17:30｜公告本公司董事會決議財務預測更新｜[觀測站](https://m)",
                   "- 🏛️ 重訊 17:40｜代子公司公告為他人背書保證｜[觀測站](https://m)",
                   f"- 📰 媒體 15:28｜{TPEX_DUP}｜[永豐金證券](https://g)",
                   f"- 📰 媒體 15:28｜{TPEX_DUP}｜[永豐金證券](https://g)",
                   "- 📰 媒體 20:00｜台積電CoWoS擴產 設備廠接單滿載｜[鉅亨網](https://c1)",
                   "- 📰 媒體 21:00｜台積電擴產CoWoS 南科再蓋新廠｜[經濟日報](https://c2)"],
    "3037欣興": ["- 📰 媒體 19:00｜ABF載板報價再漲 欣興受惠｜[鉅亨網](https://a)"],
    "8046南電": ["- 📰 媒體 19:10｜南電ABF載板漲價 第四季營運看增｜[鉅亨網](https://b)"],
    "2303聯電": [f"- 📰 媒體 15:28｜{TPEX_DUP}｜[永豐金證券](https://g)",
                 "- 📰 媒體 19:20｜聯電CoWoS中介層 ABF載板需求｜[鉅亨網](https://d)"],
}
(vault / "新聞").mkdir()
for title, lines in news.items():
    (vault / f"新聞/{title} 新聞.md").write_text(f"---\n---\n# [[{title}]]\n\n## 2026-10-22\n" + "\n".join(lines) + "\n", "utf-8")


def run(*args):
    out = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(vault), *args], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


run("--date", "2026-10-23")
r = json.loads((vault / "_data/訊號/2026-10-23.json").read_text("utf-8"))
top = {c["代號"]: c for c in r["TopN"]}
labels = {c: [s["label"] for s in v["訊號"]] for c, v in top.items()}
print(json.dumps(labels, ensure_ascii=False, indent=1))

assert "營收年增轉正" in labels["1101"]                                  # -6% → 8%
assert "營收成長減速" in labels["2303"]                                  # 40% → 1%
assert "4113" not in top and not any(s[0] == "4113" for s in r["財務訊號"])  # 基期過低的暴增排除
assert "重訊 A 級：財務預測" in labels["2330"]
assert not any("背書保證" in s["reason"] for s in top["2330"]["訊號"])     # 例行重訊是雜訊
media = [s for s in top["2330"]["訊號"] if s["label"] == "媒體：擴產"]
assert len(media) == 1 and "另有 1 則" in media[0]["reason"]             # 兩家媒體報同一件事合併成一個訊號
assert top["2330"]["規模加分"] > 0 and top["2330"]["0050"]
assert r["統計"]["雜訊"] >= 1                                            # 零股排行榜
assert list(top)[0] == "2330"
# 題材：ABF 載板出現在 3 家（欣興、南電、聯電），前 7 天沒有 → 熱門
pcb = next(t for t in r["題材"] if t["題材"] == "PCB／載板")
assert pcb["熱"] and pcb["公司數"] == 3, pcb
md = (vault / "新聞/訊號/2026-10-23 異常訊號.md").read_text("utf-8")
assert "[[2330台積電]]⭐" in md and "🔥 PCB／載板" in md
assert (vault / "新聞/訊號/題材雷達.md").exists()

# 同一天重跑：結果不變、檔案不改
before = md
run("--date", "2026-10-23")
assert (vault / "新聞/訊號/2026-10-23 異常訊號.md").read_text("utf-8") == before
# 隔天：已報過的財務訊號不再列出
run("--date", "2026-10-24")
r2 = json.loads((vault / "_data/訊號/2026-10-24.json").read_text("utf-8"))
assert not r2["財務訊號"] and not r2["TopN"], r2["TopN"]
hist = json.loads((vault / "_data/訊號/財務歷史.json").read_text("utf-8"))
assert set(hist["月營收"]["1101"]) == {"2026-08", "2026-09"}
print(run("--week", "2026-10-24")[:200])
print("ALL OK")
