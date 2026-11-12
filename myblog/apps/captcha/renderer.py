"""
基于 Pillow 13.0.0 的纯本地图片验证码渲染器。

不依赖操作系统外部字体文件，使用 Pillow 内置可缩放默认字体 (ImageFont.load_default(size=...))，
加入字符旋转抖动、干扰线与噪点，防止简单 OCR 直接识别，且零外部网络依赖。
"""

from io import BytesIO
import secrets
from PIL import Image, ImageDraw, ImageFont

# 去除易混淆字符 0/O/1/I/L
CAPTCHA_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_captcha_code(length: int = 4) -> str:
    """使用密码学安全随机数生成避歧义验证码文本。"""
    return "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(length))


def render_captcha_png(code: str, width: int = 160, height: int = 52) -> bytes:
    """将验证码文本渲染为干净的静态 PNG 字节流。"""
    bg_r = 240 + secrets.randbelow(14)
    bg_g = 243 + secrets.randbelow(12)
    bg_b = 248 + secrets.randbelow(8)
    canvas = Image.new("RGB", (width, height), (bg_r, bg_g, bg_b))
    draw = ImageDraw.Draw(canvas)

    # 1. 背景干扰线段
    for _ in range(5):
        x1 = secrets.randbelow(width)
        y1 = secrets.randbelow(height)
        x2 = secrets.randbelow(width)
        y2 = secrets.randbelow(height)
        line_color = (
            130 + secrets.randbelow(80),
            140 + secrets.randbelow(80),
            160 + secrets.randbelow(80),
        )
        draw.line((x1, y1, x2, y2), fill=line_color, width=1 + secrets.randbelow(2))

    # 2. 逐字符渲染带微小角度偏转与色彩抖动的文字
    font = ImageFont.load_default(size=30)
    step_x = max(24, (width - 24) // max(1, len(code)))
    for idx, ch in enumerate(code):
        char_layer = Image.new("RGBA", (42, 46), (0, 0, 0, 0))
        char_draw = ImageDraw.Draw(char_layer)
        ink_color = (
            15 + secrets.randbelow(85),
            25 + secrets.randbelow(85),
            60 + secrets.randbelow(110),
            255,
        )
        char_draw.text((8, 4), ch, font=font, fill=ink_color)
        angle = secrets.randbelow(33) - 16  # -16 ~ +16 度
        rotated = char_layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        paste_x = 12 + idx * step_x + secrets.randbelow(5) - 2
        paste_y = max(1, (height - rotated.height) // 2 + secrets.randbelow(7) - 3)
        canvas.paste(rotated, (paste_x, paste_y), rotated)

    # 3. 轻微噪点
    for _ in range(60):
        px = secrets.randbelow(width)
        py = secrets.randbelow(height)
        dot_color = (
            110 + secrets.randbelow(100),
            110 + secrets.randbelow(100),
            130 + secrets.randbelow(100),
        )
        draw.point((px, py), fill=dot_color)

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
