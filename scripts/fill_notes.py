#!/usr/bin/env python3
"""把撰寫好的內文填進個股筆記的空白章節。

用法: python3 fill_notes.py <vault> <內容檔或資料夾> [--dry-run] [--date YYYY-MM-DD]

規則:
- 只填空白的章節（第一章到第四章、核心供應鏈），已有內容的章節不動。
- 不碰 news:start / news:end 之間的文字，也不碰 ## 最新新聞、## 相關連結。
- frontmatter 只補空白欄位，另外加上 內文更新 日期與 待校對 標籤。
- 連到不存在筆記的 [[連結]] 會改成純文字，避免產生空連結。
- @@terms 區塊（每行「分類|名詞|說明」）會在核心供應鏈前插入「# 專有名詞解析」，
  並在 名詞/<分類>/ 建立還不存在的名詞筆記（已存在的名詞筆記不動）。
- --replace：筆記 frontmatter 已有「內文更新」（代表是 Claude 寫的）時，覆寫各章節。
  沒有「內文更新」的筆記（使用者自己的調研）仍然只填空白章節。
"""
import os
import pathlib
import re
import sys

SECTIONS = {
    "ch1": "# 第一章",
    "ch2": "# 第二章",
    "ch3": "# 第三章",
    "ch4": "# 第四章",
    "chain": "# 核心供應鏈",
}
STOP = ("## 供應鏈關係", "## 最新新聞", "## 相關連結")
TERMS_HEAD = "# 專有名詞解析"
TERM_CATS = ("半導體", "應用", "金融", "產業")
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
NEWS_RE = re.compile(r"<!-- news:start -->.*?<!-- news:end -->", re.S)
LINK_RE = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")


def parse_content(text):
    out = {"fm": {}}
    key = None
    buf = []
    for line in text.splitlines():
        if line.startswith("@@"):
            if key:
                out[key] = "\n".join(buf).strip("\n")
            buf = []
            parts = line[2:].split(" ", 1)
            tag = parts[0]
            if tag == "code":
                out["code"] = parts[1].strip()
                key = None
            elif tag == "terms":
                key = "terms"
            elif tag == "fm":
                k, v = parts[1].split(":", 1)
                out["fm"][k.strip()] = v.strip()
                key = None
            else:
                key = tag
        elif key:
            buf.append(line)
    if key:
        out[key] = "\n".join(buf).strip("\n")
    return out


def fix_links(text, names):
    def repl(m):
        target = m.group(1).strip()
        if target in names:
            return m.group(0)
        if m.group(3):
            return m.group(3)[1:]
        return target
    return LINK_RE.sub(repl, text)


def fill(note_path, content, names, date, dry, replace=False, vault=None):
    raw = note_path.read_text(encoding="utf-8")
    news_before = NEWS_RE.findall(raw)
    m = re.match(r"---\n(.*?)\n---\n", raw, re.S)
    if not m:
        return "沒有 frontmatter，略過"
    fm_lines = m.group(1).split("\n")
    body = raw[m.end():]

    # frontmatter：只補空白欄位
    keys = {}
    for i, line in enumerate(fm_lines):
        if ":" in line and not line.startswith((" ", "-")):
            keys[line.split(":", 1)[0]] = i
    claude_written = "內文更新" in keys
    for k, v in content["fm"].items():
        if k in keys:
            i = keys[k]
            if fm_lines[i].split(":", 1)[1].strip() in ("", '""', "[]"):
                fm_lines[i] = f"{k}: {v}"
        else:
            fm_lines.append(f"{k}: {v}")
    if "內文更新" in keys:
        fm_lines[keys["內文更新"]] = f"內文更新: {date}"
    else:
        pos = keys["名稱"] + 1 if "名稱" in keys else len(fm_lines)  # 放在名稱後，避免和自動排程改的欄位相鄰
        fm_lines.insert(pos, f"內文更新: {date}")
    for i, line in enumerate(fm_lines):
        if line.startswith("tags:") and "待校對" not in line:
            fm_lines[i] = re.sub(r"\]\s*$", ", 待校對]", line)

    # 本文：只填空白章節（--replace 時覆寫 Claude 寫過的章節）
    lines = body.split("\n")
    filled, skipped = [], []
    created = []
    if content.get("terms", "").strip():
        terms = parse_terms(content["terms"])
        created = make_term_notes(vault, terms, names, note_path.stem, dry)
        content["termsblock"] = terms_block(terms)
        if not any(l.startswith(TERMS_HEAD) for l in lines):
            idx = next((i for i, l in enumerate(lines) if l.startswith("# 核心供應鏈")), None)
            if idx is not None:
                lines[idx:idx] = [TERMS_HEAD, ""]
    sections = dict(SECTIONS, termsblock=TERMS_HEAD)
    for key, head in sections.items():
        if key not in content or not content[key].strip():
            continue
        idx = next((i for i, l in enumerate(lines) if l.startswith(head)), None)
        if idx is None:
            skipped.append(f"{key}(找不到標題)")
            continue
        end = idx + 1
        while end < len(lines) and not (lines[end].startswith("# ") or lines[end].startswith(STOP)):
            end += 1
        if "\n".join(lines[idx + 1:end]).strip() and not (replace and claude_written):
            skipped.append(f"{key}(已有內容)")
            continue
        block = ["", fix_links(content[key], names), ""]
        lines[idx + 1:end] = block
        filled.append(key)

    new = "---\n" + "\n".join(fm_lines) + "\n---\n" + "\n".join(lines)
    new = re.sub(r"\n{3,}", "\n\n", new)
    if NEWS_RE.findall(new) != news_before:
        return "新聞區塊會被改動，已中止"
    if not dry and new != raw:
        tmp = note_path.with_name(f".{note_path.name}.tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, note_path)
    msg = f"填入 {','.join(filled) or '無'}" + (f"；略過 {','.join(skipped)}" if skipped else "")
    return msg + (f"；新名詞 {len(created)}" if created else "")


def parse_terms(text):
    out = []
    for line in text.splitlines():
        parts = [x.strip() for x in line.split("|", 2)]
        if len(parts) != 3 or not parts[1]:
            continue
        cat = parts[0] if parts[0] in TERM_CATS else "產業"
        name = BAD_CHARS.sub(" ", parts[1]).strip()
        out.append((cat, name, parts[2]))
    return out


def terms_block(terms):
    lines = []
    for cat in TERM_CATS:
        items = [t for t in terms if t[0] == cat]
        if not items:
            continue
        lines += [f"## {cat}", ""] + [f"- **[[{n}]]**：{d}" for _, n, d in items] + [""]
    return "\n".join(lines).strip()


def make_term_notes(vault, terms, names, stock, dry):
    created = []
    for cat, name, desc in terms:
        if name in names:
            continue
        d = vault / "名詞" / cat
        body = f"---\ntags: [名詞/{cat}, 待校對]\n---\n# {name}\n\n{desc}\n\n## 相關\n- [[{stock}]]\n"
        if not dry:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f".{name}.md.tmp"
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, d / f"{name}.md")
        names.add(name)
        created.append(name)
    return created


def main():
    vault = pathlib.Path(sys.argv[1])
    src = pathlib.Path(sys.argv[2])
    dry = "--dry-run" in sys.argv
    replace = "--replace" in sys.argv
    date = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else "2026-10-07"
    names = {p.stem for p in vault.rglob("*.md")} | {p.name for p in vault.rglob("*.png")}
    tw = {p.stem.split(" ")[0]: p for p in (vault / "個股" / "台股").glob("*.md")}
    by_code = {}
    for stem, p in tw.items():
        mm = re.match(r"^(\d{4,6})", stem)
        if mm:
            by_code.setdefault(mm.group(1), p)
    files = sorted(src.glob("*.md")) if src.is_dir() else [src]
    for f in files:
        c = parse_content(f.read_text(encoding="utf-8"))
        note = by_code.get(c.get("code", ""))
        if not note:
            print(f"{c.get('code')}: 找不到筆記")
            continue
        print(f"{note.name}: {fill(note, c, names, date, dry, replace, vault)}")


if __name__ == "__main__":
    main()
