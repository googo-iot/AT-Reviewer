"""assets/icon.png → assets/icon.ico 변환.

    .venv/Scripts/python.exe tools/make_icon.py

Windows 는 창·작업표시줄·바로가기마다 다른 크기의 아이콘을 요구한다.
한 장짜리 .ico 를 주면 큰 화면에서 뭉개지므로 여러 크기를 한 파일에 넣는다.

작은 크기에는 다른 도안을 넣는다
--------------------------------
원본 도안은 글자가 캔버스의 1/4 정도라, 16px 아이콘에서는 글자 높이가 4px 밖에
안 되어 무엇이 적혔는지 알아볼 수 없다. 원본을 4000px 로 그려도 마찬가지다.
그래서 작은 크기용으로는 돋보기를 빼고 글자만 크게 채운 판을 따로 만든다.
Windows 기본 아이콘들이 쓰는 방식이다.

    assets/icon_small.png 을 두면 그 그림을 작은 크기에 쓴다.
    없으면 원본에서 색을 뽑아 글자판을 만든다.

아이콘을 바꿀 때는 assets/icon.png 를 갈아끼우고 이 스크립트를 다시 돌리면 된다.
"""

from __future__ import annotations

import struct
import sys
from io import BytesIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS = PROJECT_ROOT / "assets"
SOURCE = ASSETS / "icon.png"
SOURCE_SMALL = ASSETS / "icon_small.png"      # 있으면 작은 크기에 이걸 쓴다
TARGET = ASSETS / "icon.ico"
PREVIEW = PROJECT_ROOT / "output" / "icon_preview.png"

#: Windows 가 골라 쓰는 크기들. 화면 배율(100/125/150/175/200%)마다 요구 크기가 다르고,
#: 딱 맞는 크기가 없으면 다른 크기를 늘려 쓰면서 흐려진다. 그래서 촘촘히 넣는다.
SIZES = (16, 20, 24, 30, 32, 36, 40, 48, 64, 96, 128, 256)

#: 이 크기 미만에서는 단순화한 도안을 쓴다.
SIMPLIFY_BELOW = 48

#: 작은 크기 판에 넣을 글자.
SMALL_TEXT = "ATR"

#: 글꼴 후보. Bold 를 먼저 쓴다 — Black 은 너무 굵어서 작은 크기에서
#: A·R 안쪽 구멍이 메워지고 오히려 뭉개져 보인다.
FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",    # Segoe UI Bold
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/verdanab.ttf",
    "C:/Windows/Fonts/seguibl.ttf",     # Segoe UI Black (마지막 수단)
)

#: 글자가 판 가로에서 차지할 비율. 더 키우면 판 밖으로 넘친다.
TEXT_RATIO = 0.84


# --------------------------------------------------------------------------
# 색 뽑기
# --------------------------------------------------------------------------


def sample_colors(image) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """원본에서 (배경색, 글자색)을 뽑는다.

    배경은 판 안쪽 모서리에서, 글자색은 가장 밝은 불투명 픽셀에서 가져온다.
    도안이 바뀌어도 색을 다시 적어줄 필요가 없게 한다.
    """
    width, height = image.size
    background = image.getpixel((int(width * 0.08), int(height * 0.5)))
    if background[3] < 200:                       # 모서리가 투명하면 한가운데 근처로
        background = image.getpixel((int(width * 0.5), int(height * 0.08)))

    brightest = (255, 255, 255, 255)
    step = max(1, width // 200)
    best = -1
    for y in range(0, height, step):
        for x in range(0, width, step):
            pixel = image.getpixel((x, y))
            if pixel[3] < 200:
                continue
            score = pixel[0] + pixel[1] + pixel[2]
            if score > best:
                best, brightest = score, pixel
    return background, brightest


# --------------------------------------------------------------------------
# 작은 크기용 판 만들기
# --------------------------------------------------------------------------


def build_small_tile(size: int, background, foreground):
    """글자만 크게 채운 정사각 판. 작은 크기에서 판독되는 것이 유일한 목표다.

    크게 그린 뒤 줄이지 않고 목표 크기에 바로 그린다.
    축소를 거치면 획 가장자리에 회색이 번져 흐려 보인다.
    바로 그리면 글꼴 힌팅이 획을 픽셀 격자에 맞춰줘 또렷해진다.
    """
    from PIL import Image, ImageDraw, ImageFont

    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, round(size * 0.19)), fill=background
    )

    font_path = next((p for p in FONT_CANDIDATES if Path(p).is_file()), None)
    if font_path is None:                        # 글꼴이 없으면 판만이라도 만든다
        return tile

    # 판을 넘지 않는 선에서 가장 큰 글자 크기를 찾는다.
    # 한 칸씩 키우다가 넘치기 직전 것을 쓴다 — 넘치면 글자가 잘린다.
    chosen = None
    for points in range(max(4, round(size * 0.35)), size * 2):
        font = ImageFont.truetype(font_path, points)
        box = draw.textbbox((0, 0), SMALL_TEXT, font=font)
        width, height = box[2] - box[0], box[3] - box[1]
        if width > size * TEXT_RATIO or height > size * 0.66:
            break
        chosen = (font, box)
    if chosen is None:                           # 판이 너무 작아 글자가 안 들어간다
        return tile

    font, (left, top, right, bottom) = chosen
    draw.text(
        (round((size - (right - left)) / 2) - left,
         round((size - (bottom - top)) / 2) - top),
        SMALL_TEXT,
        font=font,
        fill=foreground,
    )
    return tile


# --------------------------------------------------------------------------
# ICO 쓰기
# --------------------------------------------------------------------------


def write_ico(images: list, path: Path) -> None:
    """크기별로 다른 그림을 담은 .ico 를 직접 쓴다.

    Pillow 의 ICO 저장은 원본 한 장을 크기별로 줄여 넣기만 해서,
    '작은 크기에는 다른 도안' 을 넣을 수 없다. 그래서 컨테이너를 직접 만든다.
    각 항목은 PNG 로 담는다 (Windows Vista 이후 지원).
    """
    blobs = []
    for image in images:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        blobs.append(buffer.getvalue())

    header = struct.pack("<HHH", 0, 1, len(blobs))      # reserved, type=icon, count
    offset = len(header) + 16 * len(blobs)
    entries, body = b"", b""
    for image, blob in zip(images, blobs):
        side = 0 if image.width >= 256 else image.width  # 256 은 0 으로 적는다
        entries += struct.pack(
            "<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset
        )
        body += blob
        offset += len(blob)

    path.write_bytes(header + entries + body)


# --------------------------------------------------------------------------
# 미리보기
# --------------------------------------------------------------------------


def write_preview(images: list, path: Path, background) -> None:
    """크기별 결과를 8배로 확대해 나란히 붙인 그림. 눈으로 확인하기 위한 것."""
    from PIL import Image, ImageDraw

    zoom = 8
    gap = 12
    width = sum(image.width * zoom + gap for image in images) + gap
    height = max(image.height for image in images) * zoom + 46
    sheet = Image.new("RGBA", (width, height), (250, 250, 250, 255))
    draw = ImageDraw.Draw(sheet)

    x = gap
    for image in images:
        scaled = image.resize(
            (image.width * zoom, image.height * zoom), Image.NEAREST  # 실제 픽셀을 보이게
        )
        sheet.alpha_composite(scaled, (x, 30))
        draw.text((x, 10), f"{image.width}px", fill=(40, 40, 40))
        x += scaled.width + gap

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


# --------------------------------------------------------------------------


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

    source = Image.open(SOURCE).convert("RGBA")
    print(f"원본  {SOURCE.name}  {source.width}x{source.height}")

    if source.width != source.height:            # 늘리면 그림이 찌그러진다
        side = max(source.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(source, ((side - source.width) // 2, (side - source.height) // 2))
        source = square
        print(f"      정사각형으로 맞춤 → {side}x{side}")

    background, foreground = sample_colors(source)
    print(f"      배경 {background[:3]} / 글자 {foreground[:3]}")

    small_source = None
    if SOURCE_SMALL.is_file():
        small_source = Image.open(SOURCE_SMALL).convert("RGBA")
        print(f"      작은 크기용 원본: {SOURCE_SMALL.name}")

    images, simplified = [], []
    for size in SIZES:
        if size < SIMPLIFY_BELOW:
            if small_source is not None:
                image = small_source.resize((size, size), Image.LANCZOS)
            else:
                image = build_small_tile(size, background, foreground)
            simplified.append(size)
        else:
            image = source.resize((size, size), Image.LANCZOS)
        images.append(image)

    write_ico(images, TARGET)
    write_preview(images, PREVIEW, background)

    print(f"생성  {TARGET.name}  {TARGET.stat().st_size:,} bytes")
    print(f"      크기: {', '.join(str(i.width) for i in images)}")
    print(f"      단순화 도안 적용: {', '.join(f'{s}px' for s in simplified)}")
    print(f"미리보기  {PREVIEW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
