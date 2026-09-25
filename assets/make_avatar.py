"""Рисует аватарку бота 640x640 в стиле карточек (запуск: python make_avatar.py)."""
from PIL import Image

import image_generator as ig

cv = ig.Canvas(640, 640, ig.BG)
cv.circle(320, 320, 300, fill=ig.CARD)
cv.circle(320, 320, 300, outline=ig.CARD2, width=4)
cv.text((320, 262), "MOG", 190, fill=ig.WHITE, bold=True)
cv.pill(320, 402, "BATTLE", 58, fg=ig.WHITE, bg=ig.BLUE, padx=44, h=98)
cv.text((320, 500), "@MOGGEDSTARSBOT", 26, fill=ig.LABEL2, bold=True)
out = cv.img.resize((640, 640), Image.Resampling.LANCZOS).convert("RGB")
out.save("avatar.png")
print("avatar.png", out.size)
