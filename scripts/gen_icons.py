"""
PWA アイコン生成スクリプト。
PIL/Pillow なし、stdlib のみで PNG ファイルを生成。

Usage: python scripts/gen_icons.py
生成ファイル: surge_radar/web/static/icon-192.png
             surge_radar/web/static/icon-512.png
"""
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).parent.parent / "surge_radar" / "web" / "static"

BG  = (15,  25,  35)    # #0f1923 – ダークネイビー
ACC = (34, 211, 238)    # #22d3ee – シアン（メインアクセント）
GRN = (52, 211, 153)    # #34d399 – グリーン（最高値バー）


def _make_png(pixels: list, w: int, h: int) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def _crc(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    def _chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + _crc(t, d)

    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in pixels)
    idat = _chunk(b"IDAT", zlib.compress(raw, level=9))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def create_icon(size: int) -> list:
    """上昇バーチャートアイコンを描画。返り値は pixels[y][x]=(R,G,B) のリスト。"""
    px = [[BG] * size for _ in range(size)]

    pad = max(size // 10, 8)
    inner_w = size - 2 * pad
    inner_h = size - 2 * pad

    n_bars = 3
    bar_gap = max(inner_w // 14, 2)
    bar_w = (inner_w - (n_bars - 1) * bar_gap) // n_bars

    # 各バーの高さ比率（左→右で増加するバーチャート）
    heights_pct = [0.42, 0.65, 1.0]
    colors = [ACC, ACC, GRN]

    for i, (h_pct, color) in enumerate(zip(heights_pct, colors)):
        bar_h = int(inner_h * h_pct)
        x0 = pad + i * (bar_w + bar_gap)
        x1 = x0 + bar_w
        y0 = pad + inner_h - bar_h
        y1 = pad + inner_h

        # バー本体
        for y in range(y0, y1):
            for x in range(x0, x1):
                if 0 <= x < size and 0 <= y < size:
                    px[y][x] = color

    # 最右バーの上に小さな上昇矢印ヘッド（三角形）
    right_bar_cx = pad + 2 * (bar_w + bar_gap) + bar_w // 2
    arrow_tip_y = pad + inner_h - int(inner_h * 1.0) - max(pad // 3, 3)
    arrow_half = max(bar_w // 3, 3)
    for dy in range(arrow_half + 1):
        half_w = arrow_half - dy
        for dx in range(-half_w, half_w + 1):
            ax = right_bar_cx + dx
            ay = arrow_tip_y + dy
            if 0 <= ax < size and 0 <= ay < size:
                px[ay][ax] = GRN

    return px


def main():
    STATIC.mkdir(parents=True, exist_ok=True)
    for size in [192, 512]:
        pixels = create_icon(size)
        data = _make_png(pixels, size, size)
        out = STATIC / f"icon-{size}.png"
        out.write_bytes(data)
        print(f"Generated {out} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()
