"""assets/icon.png → assets/icon.ico 변환.

    .venv/Scripts/python.exe tools/make_icon.py

Windows 는 창·작업표시줄·바로가기마다 다른 크기의 아이콘을 요구한다.
한 장짜리 .ico 를 주면 큰 화면에서 뭉개지므로 여러 크기를 한 파일에 넣는다.

아이콘을 바꿀 때는 assets/icon.png 만 갈아끼우고 이 스크립트를 다시 돌리면 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "assets" / "icon.png"
TARGET = PROJECT_ROOT / "assets" / "icon.ico"

#: Windows 가 실제로 골라 쓰는 크기들. 작은 쪽이 없으면 큰 것을 줄여 쓰며 뭉개진다.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow 가 필요합니다:  pip install Pillow")
        return 2

    if not SOURCE.is_file():
        print(f"원본이 없습니다: {SOURCE}")
        print("  아이콘 이미지를 이 경로에 저장한 뒤 다시 실행하세요.")
        return 2

    image = Image.open(SOURCE)
    print(f"원본  {SOURCE.name}  {image.width}x{image.height}  {image.mode}")

    # 투명 배경을 살리려면 RGBA 여야 한다.
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    # 정사각형이 아니면 긴 쪽에 맞춰 투명 여백을 채운다. 늘리면 그림이 찌그러진다.
    if image.width != image.height:
        side = max(image.width, image.height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        image = canvas
        print(f"      정사각형으로 맞춤 → {side}x{side}")

    sizes = [(s, s) for s in SIZES if s <= max(image.width, 256)]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    image.save(TARGET, format="ICO", sizes=sizes)

    print(f"생성  {TARGET.name}  {TARGET.stat().st_size:,} bytes")
    print(f"      포함 크기: {', '.join(f'{w}x{h}' for w, h in sizes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
