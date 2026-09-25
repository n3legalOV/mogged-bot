"""
Карточки MOG BATTLE в стиле iOS (тёмная тема): сгруппированные карточки, системные цвета,
капсулы-статусы, рендер в 2x и уменьшение для гладких краёв.
"""
import io
import logging
import time
from functools import lru_cache
from typing import Any, List, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

try:
    from stats import Rank, average_score
except ImportError:  # запуск отдельно от проекта
    class Rank:
        def __init__(self, name: str, color: str):
            self.name = name
            self.color = color

    def average_score(stats: List[float]) -> float:
        return sum(stats) / len(stats) if stats else 0.0

logger = logging.getLogger(__name__)

# ---------- палитра iOS (dark) ----------
BG = "#000000"
CARD = "#1C1C1E"
CARD2 = "#2C2C2E"
SEP = "#38383A"
WHITE = "#FFFFFF"
LABEL2 = "#8E8E93"
FILL_OFF = "#48484A"
GREEN = "#30D158"
RED = "#FF453A"
BLUE = "#0A84FF"
ORANGE = "#FF9F0A"

S = 2  # коэффициент суперсэмплинга

_FONT_DIRS = {
    False: ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    True: ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    for path in _FONT_DIRS[bold]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(fg: str, bg: str, a: float) -> str:
    f, b = _rgb(fg), _rgb(bg)
    return "#%02x%02x%02x" % tuple(round(f[i] * a + b[i] * (1 - a)) for i in range(3))


class Canvas:
    """Рисует в координатах 1x, внутри держит картинку в 2x."""

    def __init__(self, w: int, h: int, bg: str = BG):
        self.w, self.h = w, h
        self.img = Image.new("RGBA", (w * S, h * S), bg)
        self.d = ImageDraw.Draw(self.img)

    def rr(self, box, r, fill=None, outline=None, width=0):
        x0, y0, x1, y1 = box
        self.d.rounded_rectangle((x0 * S, y0 * S, x1 * S, y1 * S), radius=r * S,
                                 fill=fill, outline=outline, width=width * S)

    def circle(self, cx, cy, r, fill=None, outline=None, width=0):
        self.d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S),
                       fill=fill, outline=outline, width=width * S)

    def text(self, xy, s, size, fill=WHITE, bold=False, anchor="mm"):
        self.d.text((xy[0] * S, xy[1] * S), s, font=_font(size * S, bold), fill=fill, anchor=anchor)

    def width_of(self, s, size, bold=False) -> float:
        return _font(size * S, bold).getlength(s) / S

    def fit(self, s: str, max_w: int, size: int, bold=False) -> str:
        if self.width_of(s, size, bold) <= max_w:
            return s
        while len(s) > 1 and self.width_of(s + "…", size, bold) > max_w:
            s = s[:-1]
        return s + "…"

    def pill(self, cx, cy, s, size, fg, bg, padx=22, h=None, bold=True):
        w = self.width_of(s, size, bold) + padx * 2
        h = h or size + 20
        self.rr((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), h / 2, fill=bg)
        self.text((cx, cy), s, size, fill=fg, bold=bold)

    def avatar(self, img: Image.Image, cx, cy, size):
        self.img.alpha_composite(img, (int((cx - size / 2) * S), int((cy - size / 2) * S)))

    def line(self, x0, x1, y, fill=SEP, width=1):
        self.d.line((x0 * S, y * S, x1 * S, y * S), fill=fill, width=width * S)

    def to_jpeg(self, quality: int = 93) -> io.BytesIO:
        out = io.BytesIO()
        self.img.resize((self.w, self.h), Image.Resampling.LANCZOS).convert("RGB").save(
            out, format="JPEG", quality=quality, subsampling=0)
        out.seek(0)
        return out


# ---------- аватарки ----------
_AV_CACHE: dict[str, tuple[float, bytes]] = {}
_AV_TTL = 1800
_AV_MAX = 300


async def _fetch_avatar_bytes(url: str) -> Optional[bytes]:
    hit = _AV_CACHE.get(url)
    if hit and time.monotonic() - hit[0] < _AV_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        res = await client.get(url)
    if res.status_code != 200:
        return None
    if len(_AV_CACHE) >= _AV_MAX:
        _AV_CACHE.pop(next(iter(_AV_CACHE)))
    _AV_CACHE[url] = (time.monotonic(), res.content)
    return res.content


async def _fetch_avatar(url: Optional[str], px: int, placeholder: str = "?") -> Image.Image:
    """Круглая аватарка px×px (в 2x-пикселях) или серый круг с символом."""
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, px - 1, px - 1), fill=255)
    if url:
        try:
            content = await _fetch_avatar_bytes(url)
            if content:
                raw = Image.open(io.BytesIO(content)).convert("RGBA").resize((px, px), Image.Resampling.LANCZOS)
                out.paste(raw, (0, 0), mask)
                return out
        except Exception as e:
            logger.warning("Ошибка загрузки аватара: %s", e)
    d = ImageDraw.Draw(out)
    d.ellipse((0, 0, px - 1, px - 1), fill=_rgb(CARD2))
    d.text((px // 2, px // 2), placeholder, font=_font(int(px * 0.42), True), fill=_rgb(LABEL2), anchor="mm")
    out.putalpha(mask)
    return out


def _rank_pill(cv: Canvas, cx, cy, rank: Any, size: int = 22):
    name = str(getattr(rank, "name", rank)).upper()
    color = getattr(rank, "color", BLUE)
    cv.pill(cx, cy, name, size, fg=WHITE, bg=color, padx=18, h=size + 16)


# ==========================================
# 1. КАРТОЧКА ВЫЗОВА
# ==========================================
async def make_challenge_card(challenger_name: str, photo_url: Optional[str], rank: Any) -> io.BytesIO:
    W, H = 1080, 720
    cv = Canvas(W, H)
    cv.rr((40, 40, W - 40, H - 40), 44, fill=CARD)

    cv.text((W / 2, 112), "MOG BATTLE", 26, fill=LABEL2, bold=True)
    cv.text((W / 2, 170), "Открытый вызов", 54, bold=True)

    cy, r = 385, 100
    left_x, right_x = 300, 780
    av = await _fetch_avatar(photo_url, r * 2 * S, "•")
    cv.circle(left_x, cy, r + 9, outline=GREEN, width=5)
    cv.avatar(av, left_x, cy, r * 2)

    cv.circle(right_x, cy, r + 9, outline=SEP, width=4)
    cv.avatar(await _fetch_avatar(None, r * 2 * S, "?"), right_x, cy, r * 2)

    cv.text((W / 2, cy), "VS", 34, fill=LABEL2, bold=True)

    name = challenger_name or "Игрок"
    cv.text((left_x, 528), cv.fit(name, 380, 36, True), 36, bold=True)
    _rank_pill(cv, left_x, 578, rank)
    cv.text((right_x, 528), "Любой игрок", 36, bold=True)
    cv.text((right_x, 578), "первый нажавший", 24, fill=LABEL2)

    cv.pill(W / 2, 646, "@MOGGEDSTARSBOT", 24, fg=BLUE, bg=_mix(BLUE, CARD, 0.16), padx=26, h=48)
    return cv.to_jpeg()


# ==========================================
# 2. КАРТОЧКА РЕЗУЛЬТАТА
# ==========================================
ROW_LABELS = ["Аватар", "Ник", "О себе", "Premium", "Оформление", "Дата рег.", "Опыт"]


async def make_result_card(
    p1_name: str, p1_photo: Optional[str], p1_rank: Any, p1_stats: List[float],
    p2_name: str, p2_photo: Optional[str], p2_rank: Any, p2_stats: List[float],
    p1_avatar_count: int = 1, p1_username_len: Optional[int] = None, p1_bio: Optional[str] = None,
    p1_value: Optional[float] = None,
    p2_avatar_count: int = 1, p2_username_len: Optional[int] = None, p2_bio: Optional[str] = None,
    p2_value: Optional[float] = None,
    **kwargs,
) -> io.BytesIO:
    W, H = 1080, 1490
    cv = Canvas(W, H)
    is_draw = bool(kwargs.get("is_draw"))
    s1, s2 = average_score(p1_stats), average_score(p2_stats)
    p1_wins = s1 >= s2

    cv.text((W / 2, 74), "MOG BATTLE", 26, fill=LABEL2, bold=True)
    cv.text((W / 2, 122), "Результат батла", 44, bold=True)

    # --- шапка: два игрока ---
    cv.rr((40, 170, W - 40, 690), 44, fill=CARD)
    cv.text((W / 2, 352), "VS", 30, fill=LABEL2, bold=True)
    players = [
        (290, p1_name, p1_photo, p1_rank, s1, p1_wins),
        (790, p2_name, p2_photo, p2_rank, s2, not p1_wins),
    ]
    for cx, name, photo, rank, score, is_win in players:
        color = BLUE if is_draw else (GREEN if is_win else RED)
        r = 84
        cv.circle(cx, 300, r + 9, outline=color, width=5)
        cv.avatar(await _fetch_avatar(photo, r * 2 * S, "?"), cx, 300, r * 2)
        cv.text((cx, 432), cv.fit(name or "Игрок", 420, 34, True), 34, bold=True)
        _rank_pill(cv, cx, 482, rank, 20)
        cv.text((cx, 566), f"{score:.2f}", 76, fill=WHITE if (is_win or is_draw) else LABEL2, bold=True)
        status = "Ничья" if is_draw else ("Победа" if is_win else "Поражение")
        cv.pill(cx, 636, status, 24, fg=color, bg=_mix(color, CARD, 0.18), padx=26, h=48)

    # --- сравнение по параметрам ---
    top, row_h = 720, 88
    cv.rr((40, top, W - 40, top + 7 * row_h + 32), 44, fill=CARD)

    def subs(n: str, avatars, ulen, bio, name, value) -> List[str]:
        prem = {True: "есть", False: "нет"}.get(kwargs.get(f"{n}_premium"), "неизвестно")
        return [f"{avatars} фото", f"{ulen or len(name or '')} букв", (bio or "пусто")[:16],
                prem, f"{int(value or 0)} из 4", f"≈{kwargs.get(f'{n}_reg_year') or '?'}",
                f"{kwargs.get(f'{n}_battles') or 0} батлов"]

    sub1 = subs("p1", p1_avatar_count, p1_username_len, p1_bio, p1_name, p1_value)
    sub2 = subs("p2", p2_avatar_count, p2_username_len, p2_bio, p2_name, p2_value)
    bar_max, left_edge, right_edge = 330, 440, 640

    for i in range(7):
        y = top + 16 + i * row_h
        cy = y + row_h / 2
        if i:
            cv.line(90, W - 90, y, SEP)
        v1, v2 = p1_stats[i], p2_stats[i]
        c1 = BLUE if v1 == v2 else (GREEN if v1 > v2 else FILL_OFF)
        c2 = BLUE if v1 == v2 else (GREEN if v2 > v1 else FILL_OFF)

        cv.text((W / 2, cy - 6), ROW_LABELS[i], 24, fill=WHITE, bold=True)
        for side, v, col, sub in ((-1, v1, c1, sub1[i]), (1, v2, c2, sub2[i])):
            edge = left_edge if side < 0 else right_edge
            anchor = "rm" if side < 0 else "lm"
            strong = col in (GREEN, BLUE)
            cv.text((edge, cy - 24), f"{v:.1f}", 32, fill=WHITE if strong else LABEL2, bold=True, anchor=anchor)
            bx0, bx1 = (edge - bar_max, edge) if side < 0 else (edge, edge + bar_max)
            cv.rr((bx0, cy + 2, bx1, cy + 12), 5, fill=CARD2)
            fill_w = max(10, int(bar_max * min(max(v, 0.0) / 10.0, 1.0)))
            if side < 0:
                cv.rr((edge - fill_w, cy + 2, edge, cy + 12), 5, fill=col)
            else:
                cv.rr((edge, cy + 2, edge + fill_w, cy + 12), 5, fill=col)
            cv.text((edge, cy + 30), sub, 18, fill=LABEL2, anchor=anchor)

    # --- подпись: карточку пересылают, это и есть реклама ---
    cv.text((W / 2, 1418), "Сравни свой профиль", 22, fill=LABEL2)
    cv.pill(W / 2, 1452, "@MOGGEDSTARSBOT", 24, fg=BLUE, bg=_mix(BLUE, BG, 0.16), padx=26, h=44)
    return cv.to_jpeg()


# ==========================================
# 3. ФИРМЕННАЯ КАРТОЧКА (заглушка для inline)
# ==========================================
def make_brand_card() -> io.BytesIO:
    W, H = 1080, 720
    cv = Canvas(W, H)
    cv.rr((40, 40, W - 40, H - 40), 44, fill=CARD)
    cv.text((W / 2, 250), "MOG BATTLE", 104, bold=True)
    cv.text((W / 2, 360), "Открытый вызов", 46, fill=WHITE, bold=True)
    cv.text((W / 2, 430), "Кто круче — проверим профили", 30, fill=LABEL2)
    cv.pill(W / 2, 560, "@MOGGEDSTARSBOT", 30, fg=BLUE, bg=_mix(BLUE, CARD, 0.16), padx=34, h=60)
    return cv.to_jpeg()
