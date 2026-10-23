"""每日日報素材：從 新聞/<代號名稱> 新聞.md 收集指定日期的新聞，0050 成分股排前面。

用法：python 系統/scripts/每日日報/collect.py --vault . --date 2026-10-21 [--since "2026-10-20 13:30"]
輸出 Markdown 到 stdout，給撰寫日報的 Claude 當素材，不寫進 vault。
"""
import argparse
import datetime as dt
import re
from pathlib import Path

ITEM_RE = re.compile(r"^- (?:(\d\d-\d\d) )?(\S+) (\S+) (\d\d:\d\d)｜(.+?)｜\[(.+?)\]\((.+?)\)\s*$")


def load_0050(vault: Path) -> list[str]:
    f = vault / "系統/資料" / "0050成分股.txt"
    return [ln.strip() for ln in f.read_text("utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def items(path: Path, since: dt.datetime, until: dt.datetime):
    day = None
    for ln in path.read_text("utf-8").splitlines():
        if ln.startswith("## "):
            day = ln[3:].strip()
            continue
        m = ITEM_RE.match(ln)
        if not m or not day:
            continue
        try:
            t = dt.datetime.strptime(f"{day} {m.group(4)}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if since <= t <= until:
            yield t, m.group(3), m.group(5), m.group(6), m.group(7)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=".")
    ap.add_argument("--date", required=True, help="日報日期 YYYY-MM-DD")
    ap.add_argument("--since", help="起算時間 YYYY-MM-DD HH:MM，預設前一個交易日 18:00（上一份日報的截止點）")
    args = ap.parse_args()
    vault = Path(args.vault)
    d = dt.datetime.strptime(args.date, "%Y-%m-%d")
    prev = d - dt.timedelta(days=3 if d.weekday() == 0 else 1)
    since = dt.datetime.strptime(args.since, "%Y-%m-%d %H:%M") if args.since else prev.replace(hour=18, minute=0)
    until = d.replace(hour=23, minute=59)
    core = load_0050(vault)
    news = vault / "新聞"
    others = sorted(p.name[:-6] for p in news.glob("* 新聞.md") if p.name[:-6] not in core)
    print(f"# {args.date} 日報素材（{since:%m-%d %H:%M} 起）\n")
    for group, names in (("0050 成分股", core), ("其他個股", others)):
        print(f"## {group}\n")
        for name in names:
            p = news / f"{name} 新聞.md"
            if not p.exists():
                continue
            its = sorted(items(p, since, until), reverse=True)
            if group == "其他個股":
                its = [i for i in its if i[1] == "媒體"]  # 非權值股只看媒體報導，例行重訊太多
            if not its:
                continue
            print(f"### [[{name}]]")
            for t, kind, title, src, url in its:
                print(f"- {t:%m-%d %H:%M} {kind}｜{title}｜{src}｜{url}")
            print()


if __name__ == "__main__":
    main()
