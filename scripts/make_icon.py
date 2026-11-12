"""生成应用图标 assets/icon.ico。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(exist_ok=True)

size = 512
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 16, size - 16, size - 16], radius=104,
                    fill=(58, 116, 250, 255))
d.ellipse([-60, 300, 260, 620], fill=(124, 92, 255, 180))
# 云
d.rounded_rectangle([120, 300, 392, 384], radius=42, fill=(255, 255, 255, 255))
d.ellipse([136, 232, 268, 364], fill=(255, 255, 255, 255))
d.ellipse([214, 196, 372, 354], fill=(255, 255, 255, 255))
# 播放三角
d.polygon([(238, 288), (238, 392), (330, 340)], fill=(58, 116, 250, 255))

img.save(OUT / "icon.png")
img.save(OUT / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                  (128, 128), (256, 256)])
print("saved", OUT / "icon.ico")
