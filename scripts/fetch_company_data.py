#!/usr/bin/env python3
"""下載證交所、櫃買中心 OpenAPI 的公司基本資料與財務指標，存成 _data/公司資料/*.json。

個股內文撰寫時用這些官方數字（營收、三率、EPS、資本額等），並在筆記標註資料日期。
"""
import json
import pathlib
import sys
import time
import urllib.request

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "_data/公司資料")

SOURCES = {
    # 上市
    "上市_基本資料": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "上市_每月營收": "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
    "上市_營益分析": "https://openapi.twse.com.tw/v1/opendata/t187ap17_L",
    "上市_綜合損益_一般業": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
    "上市_資產負債_一般業": "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci",
    "上市_股利": "https://openapi.twse.com.tw/v1/opendata/t187ap45_L",
    "上市_本益比殖利率": "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
    # 上櫃
    "上櫃_基本資料": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    "上櫃_每月營收": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
    "上櫃_營益分析": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap17_O",
    "上櫃_綜合損益_一般業": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
    "上櫃_資產負債_一般業": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci",
    "上櫃_股利": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap45_O",
    "上櫃_本益比殖利率": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
}

UA = {"User-Agent": "Mozilla/5.0 (taiwan-stock-vault data fetch)", "Accept": "application/json"}


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            print(f"  重試 {i + 1}: {e}")
            time.sleep(3 * (i + 1))
    return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"抓取時間": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)), "結果": {}}
    for name, url in SOURCES.items():
        print(name, url)
        data = get(url)
        if data is None:
            report["結果"][name] = "失敗"
            continue
        (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        report["結果"][name] = f"{len(data)} 筆"
    (OUT / "_抓取紀錄.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
