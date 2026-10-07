"""離線測試：假資料驗證 frontmatter 寫入與不重複改檔。"""
import datetime as dt, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_news as un
import update_financials as uf

FAKE = {
    un.TWSE_COMPANIES: [{"公司代號": "2303", "公司簡稱": "聯電"}],
    un.TPEX_COMPANIES: [{"SecuritiesCompanyCode": "3131", "CompanyAbbreviation": "弘塑"}],
    uf.REVENUE[0]: [{"資料年月": "11509", "公司代號": "2303", "營業收入-當月營收": "23456789",
                     "營業收入-上月比較增減(%)": "3.21", "營業收入-去年同月增減(%)": "12.345", "累計營業收入-前期比較增減(%)": "8.8"}],
    uf.INCOME[0]: [{"年度": "115", "季別": "1", "公司代號": "2303", "營業收入": "100", "營業毛利（毛損）淨額": "30", "營業利益（損失）": "20", "本期淨利（淨損）": "15", "基本每股盈餘（元）": "1.29"},
                   {"年度": "115", "季別": "2", "公司代號": "2303", "營業收入": "200", "營業毛利（毛損）淨額": "62", "營業利益（損失）": "40", "本期淨利（淨損）": "60", "基本每股盈餘（元）": "4.68"}],
    uf.DIVIDEND[0]: [{"公司代號": "2303", "股利所屬期間": "1140101~1141231", "股東配發-盈餘分配之現金股利(元/股)": "2.85"},
                     {"公司代號": "2303", "股利所屬期間": "1130101~1131231", "股東配發-盈餘分配之現金股利(元/股)": "3.0"}],
}
def fake(url, params=None, retries=3):
    if url in FAKE:
        return json.dumps(FAKE[url]).encode()
    raise RuntimeError("404")
un.fetch = fake

vault = Path(tempfile.mkdtemp())
src = Path(__file__).resolve().parents[1] / "個股/台股/2303聯電.md"
(vault / "個股/台股").mkdir(parents=True)
shutil.copy(src, vault / "個股/台股/2303聯電.md")
note = vault / "個股/台股/2303聯電.md"
before = note.read_text("utf-8")
sys.argv = ["x", "--vault", str(vault)]
uf.main()
after = note.read_text("utf-8")
print(after.split("\n---\n")[0])
assert "月營收: 234.57" in after and "月營收年增率: 12.35" in after and '月營收年月: "2026-09"' in after
assert '財報季度: "2026Q2"' in after and "毛利率: 31" in after and "累計EPS: 4.68" in after
assert "現金股利: 2.85" in after and "股利所屬年度: 2025" in after
assert before.split("\n---\n", 1)[1] == after.split("\n---\n", 1)[1]  # 內文完全不變
import yaml; yaml.safe_load(after.split("---\n")[1])
uf.main()
assert note.read_text("utf-8") == after  # 數字沒變不改檔
print("ALL OK")
