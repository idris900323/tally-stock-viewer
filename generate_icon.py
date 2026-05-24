from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.ellipse([4, 4, 60, 60], fill=(34, 139, 34))  # forest green
try:
    font = ImageFont.truetype("arial.ttf", 32)
except:
    font = ImageFont.load_default()
draw.text((20, 14), "S", fill="white", font=font)
img.save(r"C:\tally_stock\icon.png")
print("icon.png created.")
