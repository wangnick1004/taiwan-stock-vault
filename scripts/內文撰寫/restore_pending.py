"""把待撰寫追蹤清單裡的公司補回筆記庫：從清理前的版本還原筆記骨架、補回產業索引並重算檔數，
再把它從追蹤清單與待撰寫佇列移除。內文由撰寫排程之後用 fill_notes.py 寫入。

用法：python scripts/內文撰寫/restore_pending.py 2355 5289 ...
"""
import re
import subprocess
import sys
from pathlib import Path

BEFORE_CLEANUP = "32fe3dd"  # 2026-10-20 清理前的 commit
V = Path(__file__).resolve().parents[2]
TRACK = V / "_data/待撰寫追蹤.txt"
QUEUE = V / "_data/待撰寫佇列.md"
ROW = re.compile(r"^\| (\d+[A-Z]?) \| \[\[")


def git_show(path: str) -> str:
    return subprocess.run(["git", "show", f"{BEFORE_CLEANUP}:{path}"], cwd=V, capture_output=True, text=True, check=True).stdout


def recount() -> None:
    tot = {}
    for p in sorted((V / "產業索引").glob("*.md")):
        t = p.read_text("utf-8")
        rows = re.findall(r"^\| \d+[A-Z]? \| \[\[.*?\]\] \| (\S+) \|", t, re.M)
        c = {k: rows.count(k) for k in set(rows)}
        parts = "、".join(f"{k} {c.get(k, 0)}" for k in ["上市", "上櫃", "創新板"] if k in c or k != "創新板")
        p.write_text(re.sub(r"^共 \d+ 檔（.*?）$", f"共 {len(rows)} 檔（{parts}）", t, count=1, flags=re.M), "utf-8")
        tot[p.stem.replace("產業索引-", "")] = c
    T = V / "台股總索引.md"
    t = T.read_text("utf-8")
    for ind, c in tot.items():
        t = re.sub(rf"^(\| \[\[產業索引-{re.escape(ind)}\\\|{re.escape(ind)}\]\] \| )\d+( \|)$",
                   lambda m: f"{m.group(1)}{sum(c.values())}{m.group(2)}", t, flags=re.M)
    allc = {}
    for c in tot.values():
        for k, v in c.items():
            allc[k] = allc.get(k, 0) + v
    parts = "、".join(f"{k} {allc[k]}" for k in ["上市", "上櫃", "創新板"] if k in allc)
    T.write_text(re.sub(r"共 \d+ 檔有調研內文：[^。]*。", f"共 {sum(allc.values())} 檔有調研內文：{parts}。", t, count=1), "utf-8")


def restore(code: str) -> str:
    track = TRACK.read_text("utf-8")
    m = re.search(rf"^{code} (\S.*)$", track, re.M)
    if not m:
        raise SystemExit(f"{code} 不在追蹤清單")
    title = m.group(1)
    note = V / "個股/台股" / f"{title}.md"
    text = git_show(f"個股/台股/{title}.md")
    if not note.exists():
        note.write_text(text, "utf-8")
    ind = re.search(r"^產業:\s*(.+)$", text, re.M).group(1).strip()
    idx = V / "產業索引" / f"產業索引-{ind}.md"
    lines = idx.read_text("utf-8").split("\n")
    if not any(f"[[{title}]]" in ln for ln in lines):
        row = next(ln for ln in git_show(f"產業索引/產業索引-{ind}.md").split("\n") if f"[[{title}]]" in ln)
        pos = [i for i, ln in enumerate(lines) if ROW.match(ln)]
        at = next((i for i in pos if ROW.match(lines[i]).group(1) > code), pos[-1] + 1)
        lines.insert(at, row)
        idx.write_text("\n".join(lines), "utf-8")
    TRACK.write_text(re.sub(rf"^{code} .*\n", "", track, flags=re.M), "utf-8")
    if QUEUE.exists():
        QUEUE.write_text(re.sub(rf"^- {re.escape(title)} .*\n", "", QUEUE.read_text("utf-8"), flags=re.M), "utf-8")
    return title


if __name__ == "__main__":
    for c in sys.argv[1:]:
        print("還原", restore(c))
    recount()
