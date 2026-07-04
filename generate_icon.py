from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
ICON_FILE = BASE_DIR / "icon.png"

img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.ellipse([4, 4, 60, 60], fill=(34, 139, 34))  # forest green
try:
    font = ImageFont.truetype("arial.ttf", 32)
except:
    font = ImageFont.load_default()
draw.text((20, 14), "S", fill="white", font=font)
ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
img.save(ICON_FILE)
print("icon.png created.")
