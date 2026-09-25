import io
import math
import logging
from typing import Optional, Union, List, Any
import httpx
from PIL import Image, ImageDraw, ImageFont

# Попытка импорта из существующего проекта, с фоллбэком при отсутствии
try:
    from stats import Rank, average_score
except ImportError:
    class Rank:
        def __init__(self, name: str, color: str):
            self.name = name
            self.color = color

    def average_score(stats: List[float]) -> float:
        return sum(stats) / len(stats) if stats else 0.0

logger = logging.getLogger(__name__)

# ==========================================
# ЦВЕТОВАЯ ПАЛИТРА И СТИЛИ
# ==========================================
BG_GRADIENT_TOP = (15, 23, 42)       # #0F172A
BG_GRADIENT_BOTTOM = (30, 41, 59)    # #1E293B
COLOR_WHITE = "#FFFFFF"
COLOR_GREY = "#94A3B8"
COLOR_GOLD = "#FFD700"
COLOR_GREEN = "#22C55E"
COLOR_RED = "#EF4444"
COLOR_BORDER = "#334155"
COLOR_PURPLE = "#8B5CF6"


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Подбор доступного шрифта с поддержкой кириллицы."""
    fonts_to_check = [
        ("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ("System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "System/Library/Fonts/Supplemental/Arial.ttf")
    ]
    
    for path in fonts_to_check:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
            
    return ImageFont.load_default()


def _create_background(width: int, height: int) -> Image.Image:
    """Генерация градиентного фона от #0F172A к #1E293B с рамкой."""
    base = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / height
        r = int(BG_GRADIENT_TOP[0] + (BG_GRADIENT_BOTTOM[0] - BG_GRADIENT_TOP[0]) * ratio)
        g = int(BG_GRADIENT_TOP[1] + (BG_GRADIENT_BOTTOM[1] - BG_GRADIENT_TOP[1]) * ratio)
        b = int(BG_GRADIENT_TOP[2] + (BG_GRADIENT_BOTTOM[2] - BG_GRADIENT_TOP[2]) * ratio)
        base.putpixel((0, y), (r, g, b))
    
    img = base.resize((width, height), Image.Resampling.BILINEAR).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    # Внешняя скругленная обводка карточки
    draw.rounded_rectangle((20, 20, width - 20, height - 20), radius=24, outline=COLOR_BORDER, width=3)
    return img


async def _fetch_avatar(url: Optional[str], size: int) -> Image.Image:
    """Загрузка аватара или создание заменяющей заглушки с знакам '?'."""
    avatar_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size, size), fill=255)

    if url:
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    raw_av = Image.open(io.BytesIO(res.content)).convert("RGBA")
                    raw_av = raw_av.resize((size, size), Image.Resampling.LANCZOS)
                    avatar_img.paste(raw_av, (0, 0), mask)
                    return avatar_img
        except Exception as e:
            logger.warning(f"Ошибка загрузки аватара ({url}): {e}")

    # Заглушка, если нет аватара
    draw = ImageDraw.Draw(avatar_img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(30, 41, 59, 255))
    font_q = _get_font(int(size * 0.45), bold=True)
    draw.text((size // 2, size // 2), "?", font=font_q, fill=COLOR_GREY, anchor="mm")
    avatar_img.putalpha(mask)
    return avatar_img


def _draw_rank_badge(draw: ImageDraw.ImageDraw, x: int, y: int, rank_obj: Any, anchor: str = "mm"):
    """Отрисовка плашки ранга."""
    rank_name = getattr(rank_obj, 'name', str(rank_obj)).upper()
    rank_color = getattr(rank_obj, 'color', "#3B82F6")
    
    font = _get_font(15, bold=True)
    bbox = font.getbbox(rank_name)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    
    pad_x, pad_y = 12, 5
    bw, bh = tw + pad_x * 2, th + pad_y * 2

    if anchor == "mm":
        rx = x - bw // 2
        ry = y - bh // 2
    elif anchor == "lt":
        rx, ry = x, y
    else:
        rx, ry = x - bw, y

    draw.rounded_rectangle((rx, ry, rx + bw, ry + bh), radius=8, fill=rank_color)
    draw.text((rx + bw // 2, ry + bh // 2), rank_name, font=font, fill=COLOR_WHITE, anchor="mm")


def _create_mogged_stamp() -> Image.Image:
    """Создание штампа MOGGED для проигравшей стороны."""
    sw, sh = 280, 80
    stamp = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stamp)
    
    # Прямоугольная рамка
    draw.rounded_rectangle((4, 4, sw - 4, sh - 4), radius=12, fill=(239, 68, 68, 40), outline=COLOR_RED, width=5)
    font = _get_font(34, bold=True)
    draw.text((sw // 2, sh // 2), "MOGGED", font=font, fill=COLOR_RED, anchor="mm")
    
    # Поворот штампа
    return stamp.rotate(-14, expand=True, resample=Image.Resampling.BICUBIC)


# ==========================================
# 1. КАРТОЧКА ВЫЗОВА (CHALLENGE CARD)
# ==========================================
async def make_challenge_card(
    challenger_name: str,
    photo_url: Optional[str],
    rank: Any
) -> io.BytesIO:
    """
    Генерация карточки открытого вызова (1080x800 px).
    """
    width, height = 1080, 800
    canvas = _create_background(width, height)
    draw = ImageDraw.Draw(canvas)

    # Заголовок
    font_title = _get_font(44, bold=True)
    font_sub = _get_font(24, bold=False)
    draw.text((width // 2, 60), "MOG BATTLE", font=font_title, fill=COLOR_GOLD, anchor="mm")
    draw.text((width // 2, 110), "OPEN CHALLENGE", font=font_sub, fill=COLOR_GREY, anchor="mm")

    # Игрок 1 (Вызывающий)
    c1_x, c_y = 270, 240
    av_size = 200
    
    # Аватар Вызывающего
    av1 = await _fetch_avatar(photo_url, av_size)
    draw.ellipse((c1_x - av_size // 2 - 4, c_y - av_size // 2 - 4, c1_x + av_size // 2 + 4, c_y + av_size // 2 + 4), outline=COLOR_GREEN, width=6)
    canvas.paste(av1, (c1_x - av_size // 2, c_y - av_size // 2), av1)
    
    # Подписи Вызывающего
    clean_name = challenger_name if challenger_name else "ИГРОК"
    username_str = f"@{clean_name.lower().replace(' ', '_')}"
    
    font_name = _get_font(26, bold=True)
    font_user = _get_font(18, bold=False)
    
    draw.text((c1_x, 380), clean_name[:16].upper(), font=font_name, fill=COLOR_WHITE, anchor="mm")
    draw.text((c1_x, 420), username_str[:20], font=font_user, fill=COLOR_GREY, anchor="mm")
    _draw_rank_badge(draw, c1_x, 465, rank, anchor="mm")

    # VS по центру
    font_vs = _get_font(56, bold=True)
    draw.text((width // 2, c_y), "VS", font=font_vs, fill=COLOR_WHITE, anchor="mm")

    # Игрок 2 (Заглушка)
    c2_x = 810
    av2 = await _fetch_avatar(None, av_size)
    draw.ellipse((c2_x - av_size // 2 - 4, c_y - av_size // 2 - 4, c2_x + av_size // 2 + 4, c_y + av_size // 2 + 4), outline=COLOR_PURPLE, width=6)
    canvas.paste(av2, (c2_x - av_size // 2, c_y - av_size // 2), av2)

    font_sub_opp = _get_font(16, bold=False)
    draw.text((c2_x, 380), "ЛЮБОЙ ИГРОК", font=font_name, fill=COLOR_WHITE, anchor="mm")
    draw.text((c2_x, 420), "ПЕРВЫЙ НАЖАВШИЙ ПРИМЕТ БАТТЛ", font=font_sub_opp, fill=COLOR_GREY, anchor="mm")

    # Нижняя плашка
    b_box = (80, 700, 1000, 770)
    draw.rounded_rectangle(b_box, radius=16, fill=(30, 41, 59, 200), outline=COLOR_BORDER, width=3)
    
    font_banner = _get_font(24, bold=True)
    draw.text((width // 2, 735), "⚔️ КТО ГОТОВ ПРИНЯТЬ ВЫЗОВ?", font=font_banner, fill=COLOR_GOLD, anchor="mm")

    # Сохранение в BytesIO JPEG
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=95)
    output.seek(0)
    return output


# ==========================================
# 2. КАРТОЧКА РЕЗУЛЬТАТА (RESULT CARD)
# ==========================================
async def make_result_card(
    p1_name: str,
    p1_photo: Optional[str],
    p1_rank: Any,
    p1_stats: List[float],
    p2_name: str,
    p2_photo: Optional[str],
    p2_rank: Any,
    p2_stats: List[float],
    p1_avatar_count: int = 1,
    p1_username_len: Optional[int] = None,
    p1_bio: Optional[str] = None,
    p1_gifts: int = 0,
    p1_nft: int = 0,
    p1_value: Optional[float] = None,
    p1_reg_date: Optional[str] = None,
    p1_tg_level: int = 1,
    p2_avatar_count: int = 1,
    p2_username_len: Optional[int] = None,
    p2_bio: Optional[str] = None,
    p2_gifts: int = 0,
    p2_nft: int = 0,
    p2_value: Optional[float] = None,
    p2_reg_date: Optional[str] = None,
    p2_tg_level: int = 1,
    **kwargs
) -> io.BytesIO:
    """
    Генерация карточки результатов боя (1080x1220 px).
    """
    width, height = 1080, 1220
    canvas = _create_background(width, height)
    draw = ImageDraw.Draw(canvas)

    # Заголовок
    font_title = _get_font(44, bold=True)
    font_sub = _get_font(24, bold=False)
    draw.text((width // 2, 60), "MOG BATTLE", font=font_title, fill=COLOR_GOLD, anchor="mm")
    draw.text((width // 2, 110), "RESULT", font=font_sub, fill=COLOR_GREY, anchor="mm")

    # Расчет итогового счета
    p1_score = average_score(p1_stats)
    p2_score = average_score(p2_stats)
    p1_wins = p1_score >= p2_score
    is_draw = bool(kwargs.get("is_draw"))

    # Данные колонок
    players_data = [
        {
            "x": 60, "name": p1_name, "photo": p1_photo, "rank": p1_rank, "stats": p1_stats,
            "score": p1_score, "is_winner": p1_wins,
            "subtexts": [
                f"{p1_avatar_count} фото",
                f"{p1_username_len or len(p1_name)} букв",
                p1_bio if p1_bio else "пусто",
                {True: "есть", False: "нет"}.get(kwargs.get("p1_premium"), "неизвестно"),
                f"{int(p1_value or 0)} из 4",
                f"≈{kwargs.get('p1_reg_year') or '?'}",
                f"{kwargs.get('p1_battles') or 0} батлов"
            ]
        },
        {
            "x": 580, "name": p2_name, "photo": p2_photo, "rank": p2_rank, "stats": p2_stats,
            "score": p2_score, "is_winner": not p1_wins,
            "subtexts": [
                f"{p2_avatar_count} фото",
                f"{p2_username_len or len(p2_name)} букв",
                p2_bio if p2_bio else "пусто",
                {True: "есть", False: "нет"}.get(kwargs.get("p2_premium"), "неизвестно"),
                f"{int(p2_value or 0)} из 4",
                f"≈{kwargs.get('p2_reg_year') or '?'}",
                f"{kwargs.get('p2_battles') or 0} батлов"
            ]
        }
    ]

    col_w, col_h = 440, 700
    col_y = 160

    # Разделитель VS между колонками
    font_vs = _get_font(28, bold=True)
    draw.text((width // 2, col_y + col_h // 2 - 20), "VS", font=font_vs, fill=COLOR_WHITE, anchor="mm")

    param_names = ["Аватар", "@username", "О себе", "Premium", "Оформление", "Дата рег.", "Опыт в боте"]

    for p in players_data:
        px = p["x"]
        is_win = p["is_winner"]
        stroke_color = COLOR_GREY if is_draw else (COLOR_GREEN if is_win else COLOR_RED)

        # Фон колонки
        draw.rounded_rectangle((px, col_y, px + col_w, col_y + col_h), radius=20, fill=(15, 23, 42, 230), outline=stroke_color, width=4)

        # Аватар 120x120
        av_img = await _fetch_avatar(p["photo"], 120)
        draw.ellipse((px + 18, col_y + 18, px + 142, col_y + 142), outline=stroke_color, width=4)
        canvas.paste(av_img, (px + 20, col_y + 20), av_img)

        # Имя, юзернейм, ранг
        font_pname = _get_font(22, bold=True)
        font_puser = _get_font(16, bold=False)
        clean_pname = p["name"] if p["name"] else "Игрок"
        
        draw.text((px + 160, col_y + 25), clean_pname[:14].upper(), font=font_pname, fill=COLOR_WHITE, anchor="lt")
        draw.text((px + 160, col_y + 60), f"@{clean_pname.lower().replace(' ', '_')[:16]}", font=font_puser, fill=COLOR_GREY, anchor="lt")
        _draw_rank_badge(draw, px + 160, col_y + 95, p["rank"], anchor="lt")

        # Итоговый счет игрока
        font_pscore = _get_font(38, bold=True)
        draw.text((px + col_w - 20, col_y + 30), f"{p['score']:.2f}", font=font_pscore, fill=stroke_color, anchor="rt")

        # Статус плашка (WINNER / MOGGED)
        status_text = "DRAW" if is_draw else ("WINNER" if is_win else "MOGGED")
        draw.rounded_rectangle((px + 20, col_y + 155, px + 160, col_y + 187), radius=8, fill=stroke_color)
        font_status = _get_font(16, bold=True)
        draw.text((px + 90, col_y + 171), status_text, font=font_status, fill=COLOR_WHITE, anchor="mm")

        # Таблица параметров (7 строк)
        row_start_y = col_y + 215
        row_h = 65

        font_lbl = _get_font(14, bold=False)
        font_sub_lbl = _get_font(11, bold=False)
        font_val = _get_font(14, bold=True)

        for i in range(7):
            ry = row_start_y + i * row_h
            val = p["stats"][i] if i < len(p["stats"]) else 0.0
            sub_txt = p["subtexts"][i]

            # Название и подтекст
            draw.text((px + 20, ry), param_names[i], font=font_lbl, fill=COLOR_WHITE, anchor="lt")
            draw.text((px + 20, ry + 18), str(sub_txt)[:18], font=font_sub_lbl, fill=COLOR_GREY, anchor="lt")

            # Шкала прогресса
            bar_x = px + 150
            bar_w = 200
            bar_y = ry + 8
            
            draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 8), radius=4, fill=(30, 41, 59, 255))
            
            fill_w = int(bar_w * min(max(val, 0.0) / 10.0, 1.0))
            if fill_w > 0:
                draw.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + 8), radius=4, fill=stroke_color)

            # Числовое значение
            draw.text((px + col_w - 20, ry + 2), f"{val:.2f}", font=font_val, fill=COLOR_WHITE, anchor="rt")

        # Штамп MOGGED поверх проигравшего
        if not is_win and not is_draw:
            stamp = _create_mogged_stamp()
            canvas.paste(stamp, (px + 100, col_y + 360), stamp)

    # Нижняя плашка
    info_y1 = col_y + col_h + 30
    info_y2 = info_y1 + 90
    draw.rounded_rectangle((60, info_y1, 1020, info_y2), radius=16, fill=(15, 23, 42, 255), outline=COLOR_BORDER, width=3)

    winner_player = players_data[0] if p1_wins else players_data[1]
    winner_username = f"@{winner_player['name'].lower().replace(' ', '_')}"
    diff = abs(p1_score - p2_score)

    font_bot = _get_font(24, bold=True)
    
    # Победитель слева
    if is_draw:
        draw.text((90, info_y1 + 45), "DRAW", font=font_bot, fill=COLOR_GREY, anchor="lm")
    else:
        draw.text((90, info_y1 + 45), f"WINNER {winner_username[:16]}", font=font_bot, fill=COLOR_GREEN, anchor="lm")
    # Счет по центру
    draw.text((width // 2, info_y1 + 45), f"{p1_score:.2f}  VS  {p2_score:.2f}", font=font_bot, fill=COLOR_WHITE, anchor="mm")
    # Разница справа
    draw.text((990, info_y1 + 45), f"Difference +{diff:.2f}", font=font_bot, fill=COLOR_GOLD, anchor="rm")

    # Сохранение в BytesIO JPEG
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=95)
    output.seek(0)
    return output



# ==========================================
# 3. ФИРМЕННАЯ КАРТОЧКА (заглушка для inline)
# ==========================================
def make_brand_card() -> io.BytesIO:
    width, height = 1080, 800
    canvas = _create_background(width, height)
    draw = ImageDraw.Draw(canvas)
    draw.text((width // 2, 300), "MOG BATTLE", font=_get_font(96, bold=True), fill=COLOR_GOLD, anchor="mm")
    draw.text((width // 2, 400), "ОТКРЫТЫЙ ВЫЗОВ", font=_get_font(40), fill=COLOR_WHITE, anchor="mm")
    draw.text((width // 2, 520), "Кто круче — проверим профили", font=_get_font(30), fill=COLOR_GREY, anchor="mm")
    draw.text((width // 2, 700), "@MOGGEDSTARSBOT", font=_get_font(34, bold=True), fill=COLOR_PURPLE, anchor="mm")
    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92)
    output.seek(0)
    return output
