"""アプリのアイコン (src/virtualdiskmokuroku/ui/VirtualDiskMokuroku.ico) を Pillow で描いて作り直す。

図柄は「光ディスクと目録 (一覧) のカード」。16 px から 256 px までを個別に描き、1 つの .ico にまとめる。
実行: python scripts\\make_icon.py  (Pillow が必要)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
OUTPUT = Path(__file__).resolve().parent.parent / "src" / "virtualdiskmokuroku" / "ui" / "VirtualDiskMokuroku.ico"

BG_TOP = (56, 118, 214)
BG_BOTTOM = (30, 78, 160)
DISC = (245, 248, 252)
DISC_SHADE = (205, 218, 238)
HOLE = (60, 110, 190)
CARD = (255, 255, 255)
CARD_LINE = (70, 120, 200)
CARD_SHADOW = (18, 50, 110)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))


def draw(size: int, scale: int = 8) -> Image.Image:
    """size px のアイコンを scale 倍で描いてから縮小する (縁を滑らかにするため)。"""
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景: 角丸の四角に上下のグラデーション
    radius = round(s * 0.22)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    bg = Image.new("RGBA", (s, s))
    bgd = ImageDraw.Draw(bg)
    for y in range(s):
        bgd.line((0, y, s, y), fill=_lerp(BG_TOP, BG_BOTTOM, y / (s - 1)))
    img.paste(bg, (0, 0), mask)

    small = size < 32
    # ディスク: 小さいサイズでは中央に大きく、大きいサイズでは左上寄りにしてカードを添える
    if small:
        cx = cy = s * 0.5
        r = s * 0.38
    else:
        cx, cy = s * 0.44, s * 0.44
        r = s * 0.34
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=DISC)
    # ディスクの反射 (薄い扇形)
    d.pieslice((cx - r, cy - r, cx + r, cy + r), start=200, end=290, fill=DISC_SHADE)
    d.pieslice((cx - r, cy - r, cx + r, cy + r), start=20, end=110, fill=DISC_SHADE)
    hole = r * (0.3 if small else 0.27)
    ring = r * (0.42 if small else 0.4)
    d.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), fill=DISC)
    d.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=HOLE)

    if not small:
        # 目録のカード: 右下に重ねる。影を付けて浮かせる
        w, h = s * 0.46, s * 0.40
        x0, y0 = s * 0.50, s * 0.52
        cr = round(s * 0.05)
        shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x0 + s * 0.02, y0 + s * 0.03, x0 + w + s * 0.02, y0 + h + s * 0.03), radius=cr, fill=CARD_SHADOW + (110,)
        )
        img.alpha_composite(shadow)
        d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=cr, fill=CARD)
        # 一覧の行: 先頭に小さな四角、続けて横線
        lines = 3
        gap = h / (lines + 1)
        lw = max(1, round(s * 0.035))
        for i in range(lines):
            y = y0 + gap * (i + 1)
            bx = x0 + w * 0.12
            bs = h * 0.11
            d.rectangle((bx, y - bs / 2, bx + bs, y + bs / 2), fill=CARD_LINE)
            d.line((bx + bs * 1.8, y, x0 + w * 0.88, y), fill=CARD_LINE, width=lw)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    images = [draw(size) for size in SIZES]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Pillow の ICO 保存は append_images を使って全サイズを 1 つのファイルにまとめる
    images[-1].save(OUTPUT, format="ICO", sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print(f"作成: {OUTPUT}")


if __name__ == "__main__":
    main()
