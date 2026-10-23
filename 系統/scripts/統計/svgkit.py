"""股市統計文章共用的 SVG 圖表工具（純 Python，無外部套件）。

配色與字型沿用 0050 總覽文：白底、深灰文字、藍色主序列、橘色對照序列。
產生的 SVG 用 `node 系統/scripts/0050總覽/svg2png.js <檔案>.svg` 轉成 2 倍 PNG。
"""
from html import escape

FONT = "'PingFang TC','Noto Sans TC','Microsoft JhengHei','WenQuanYi Zen Hei',sans-serif"
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e6e4de", "#ffffff"
BLUE, ORANGE, RED, GREEN = "#2a78d6", "#eb6834", "#e34948", "#008300"


class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.o = w, h, []

    def add(self, s):
        self.o.append(s)

    def text(self, x, y, t, size=13, fill=INK2, anchor="start", weight="normal"):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{escape(str(t))}</text>')

    def line(self, x1, y1, x2, y2, stroke=GRID, width=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{width}"{d}/>')

    def rect(self, x, y, w, h, fill=BLUE, rx=0, opacity=1):
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{max(h,0):.1f}" fill="{fill}" rx="{rx}" fill-opacity="{opacity}"/>')

    def circle(self, x, y, r=4, fill=BLUE, stroke=SURF, sw=1.5):
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def path(self, d, stroke=BLUE, width=2, fill="none"):
        self.add(f'<path d="{d}" stroke="{stroke}" stroke-width="{width}" fill="{fill}" stroke-linejoin="round" stroke-linecap="round"/>')

    def title(self, t, sub=None):
        self.text(24, 34, t, size=18, fill=INK, weight="600")
        if sub:
            self.text(24, 56, sub, size=13, fill=INK2)

    def source(self, t):
        self.text(24, self.h - 14, t, size=11, fill=MUTED)

    def save(self, path):
        body = "\n".join(self.o)
        open(path, "w", encoding="utf-8").write(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" font-family="{FONT}">\n'
            f'<rect width="{self.w}" height="{self.h}" fill="{SURF}"/>\n{body}\n</svg>\n')


def scale(d0, d1, r0, r1):
    return lambda v: r0 + (v - d0) / (d1 - d0) * (r1 - r0)
