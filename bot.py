"""
MOG BATTLE — Telegram Profile Duel Bot (@MOGGEDSTARSBOT)
aiogram 3.x | Python 3.10+
"""

import asyncio
import datetime
import glob
import html
import logging
import os
import random
import time
import uuid
from io import BytesIO
from typing import NamedTuple
from types import SimpleNamespace
from urllib.parse import quote

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputMediaPhoto,
    InputTextMessageContent,
    Message,
)
from dotenv import load_dotenv

import database as db
import image_generator as ig
from aiogram.types import BotCommand
from stats import roll_battle, fetch_profile_stats, average_score, get_rank, get_score_rank

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL файла виден токен
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CACHE_CHAT_ID = int(os.getenv("CACHE_CHAT_ID") or 0)
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/em07kid")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BOT_LINK = "https://t.me/MOGGEDSTARSBOT"
BACKUP_DIR = "backups"
BACKUP_KEEP = 14
PAIR_LIMIT = 6          # батлов одной пары за окно (против накрутки топа)
PAIR_WINDOW = 3600
CARD_CACHE_TTL = 600    # секунд держим file_id карточки вызова
MSK = datetime.timezone(datetime.timedelta(hours=3))

_card_cache: dict[int, tuple[float, str, str]] = {}   # user_id -> (время, file_id, ранг)
_last_spar: dict[int, float] = {}

NPCS = [  # тренировочные соперники: (имя, оценки по 7 параметрам)
    ("Sub3_Bot", [3.0, 2.5, 3.0, 2.0, 2.0, 3.0, 2.0]),
    ("Mtn_Bot", [6.0, 6.5, 6.0, 4.0, 5.0, 6.0, 5.0]),
    ("Chad_Bot", [8.0, 8.5, 8.0, 7.0, 7.0, 8.0, 7.0]),
]


class DuelFSM(StatesGroup):
    waiting_target = State()


class BattleLimit(Exception):
    pass


# ---------- ХЕЛПЕРЫ ----------
def make_mention(username: str) -> str:
    """Создаёт кликабельную ссылку на профиль"""
    if not username:
        return "ник"
    return f'<a href="https://t.me/{username}">@{username}</a>'


def display(username: str | None, first_name: str | None, uid: int) -> str:
    """HTML-имя: ссылка по нику или экранированное имя (в имени может быть разметка)."""
    if username:
        return f"<b>{make_mention(username)}</b>"
    return f"<b>{html.escape(first_name or str(uid))}</b>"


def share_url(user_id: int | None = None, text: str | None = None) -> str:
    link = BOT_LINK + (f"?start=ref_{user_id}" if user_id else "")
    text = text or "Кто круче — проверим профили. MOG BATTLE"
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text)}"


async def get_photo_url(bot: Bot, user_id: int) -> str | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count == 0:
            return None
        file = await bot.get_file(photos.photos[0][-1].file_id)
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    except Exception as e:
        logger.warning("Фото не получено для %s: %s", user_id, e)
        return None


async def _upload_card(bot: Bot, chat_id: int, card_buf) -> str | None:
    try:
        card_buf.seek(0)
        sent = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(card_buf.read(), filename="card.jpg"))
        file_id = sent.photo[-1].file_id
        await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        return file_id
    except Exception as e:
        logger.warning("Не удалось загрузить карточку (chat_id=%s): %s", chat_id, e)
        return None


async def upload_card_to_telegram(bot: Bot, chat_id: int, card_buf) -> str | None:
    """file_id общий для всего бота, поэтому заливаем сразу в служебный чат, а не в личку игрока."""
    if CACHE_CHAT_ID:
        file_id = await _upload_card(bot, CACHE_CHAT_ID, card_buf)
        if file_id:
            return file_id
    return await _upload_card(bot, chat_id, card_buf)


async def safe_chat(bot: Bot, uid: int):
    """getChat, а если Telegram не отдаёт профиль — минимум из нашей БД."""
    try:
        return await bot.get_chat(uid)
    except Exception:
        row = db.get_player(uid)
        return SimpleNamespace(id=uid, username=(row["username"] if row else "") or "", first_name=None, bio="")


def chat_decor(chat) -> int:
    """Сколько из 4 элементов оформления профиля заполнено (реальные данные getChat)."""
    marks = [
        getattr(chat, "emoji_status_custom_emoji_id", None),
        getattr(chat, "profile_accent_color_id", None) is not None
        or getattr(chat, "profile_background_custom_emoji_id", None),
        getattr(chat, "birthdate", None),
        getattr(chat, "personal_chat", None) or getattr(chat, "business_intro", None),
    ]
    return sum(1 for m in marks if m)


# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вызвать всех", switch_inline_query="mogg"),
         InlineKeyboardButton(text="Батл в личке", callback_data="private_duel")],
        [InlineKeyboardButton(text="Спарринг с ботом", callback_data="spar"),
         InlineKeyboardButton(text="Топ", switch_inline_query="top")],
        [InlineKeyboardButton(text="Пригласить друзей", url=share_url(user_id))],
        [InlineKeyboardButton(text="Поддержать", url=SUPPORT_URL)],
    ])


def result_kb(battle_id: int, rematch: bool = True, share_text: str | None = None) -> InlineKeyboardMarkup:
    first = []
    if rematch:
        first.append(InlineKeyboardButton(text="Реванш (1 раз)", callback_data=f"rm:{battle_id}"))
    first.append(InlineKeyboardButton(text="Как считается", callback_data="how"))
    rows = [first,
            [InlineKeyboardButton(text="Новый вызов", switch_inline_query="mogg"),
             InlineKeyboardButton(text="Поделиться результатом", url=share_url(text=share_text))]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def spar_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ещё спарринг", callback_data="spar")],
        [InlineKeyboardButton(text="Вызвать всех", switch_inline_query="mogg"),
         InlineKeyboardButton(text="Поделиться", url=share_url())],
    ])


def accept_kb(challenge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять вызов", callback_data=f"accept:{challenge_id}")],
    ])


# ---------- ТЕКСТЫ ----------
WELCOME = (
    "<b>MOG BATTLE</b>\n\n"
    "Сравниваем Telegram-профили: аватар, ник, био, Premium, оформление, возраст аккаунта.\n"
    "Бросай вызов, побеждай и поднимайся в рейтинге.\n\n"
    "Быстрый вызов в любом чате: <code>@MOGGEDSTARSBOT mogg</code>\n"
    "В группе: ответь на сообщение командой /duel\n\n"
    "/me · /topmog · /week · /history · /invite"
)


HOW_SHORT = ("7 параметров с весами: ник, Premium и возраст аккаунта весят больше. "
             "К счёту добавляется «форма дня» ±0.8, поэтому бывают апсеты. Разница меньше 0.15 — ничья.")

HOW_FULL = (
    "<b>Как считается батл</b>\n\n"
    "У каждого 7 оценок от 0 до 10: аватар, ник, био, Premium, оформление профиля, дата регистрации и опыт в боте.\n"
    "Итог — среднее с весами: ник ×1.3, Premium и возраст аккаунта ×1.2, аватар ×1.0, био и оформление ×0.8, опыт ×0.7.\n\n"
    "К оценкам каждого добавляется «форма дня» — случайный сдвиг до ±0.8. Поэтому слабый профиль иногда обыгрывает "
    "сильного (это называется апсет), а результат нельзя предсказать заранее.\n"
    "Если счёт различается меньше чем на 0.15 — ничья.\n\n"
    "<i>Дата регистрации оценивается по ID аккаунта, Premium бот узнаёт, когда игрок сам что-то ему пишет.</i>"
)


def build_result_text(winner: str, loser: str, s1: float, s2: float,
                      stats1: list[float], stats2: list[float], draw: bool = False, extra: str = "") -> str:
    """winner/loser — уже готовый HTML (см. display)."""
    tail = f"\n\n{extra}" if extra else ""
    if draw:
        return (f"<b>Ничья</b>: {winner} и {loser}\n\n"
                f"<code>{s1:.2f} : {s2:.2f}</code>\n"
                f"Профили на одном уровне. Реванш?{tail}")
    labels = ["аватар", "ник", "био", "Premium", "оформление", "дата регистрации", "опыт"]
    strong = []
    for i, (v1, v2) in enumerate(zip(stats1, stats2)):
        if (s1 >= s2 and v1 > v2) or (s2 > s1 and v2 > v1):
            strong.append(labels[i])
    diff = abs(s1 - s2)
    if len(strong) == 1:
        strong_text = f"Решил: <b>{strong[0]}</b>."
    elif len(strong) > 1:
        strong_text = f"Сильнее всего: <b>{', '.join(strong[:-1])} и {strong[-1]}</b>."
    else:
        strong_text = ""
    if diff >= 0.8:
        strong_text = f"{strong_text} Заметное преимущество.".strip()
    body = f"\n{strong_text}" if strong_text else ""
    return (f"{winner} MOGGED {loser}\n\n"
            f"<code>{s1:.2f} : {s2:.2f}</code>  разница +{diff:.2f}{body}{tail}")


def build_top_text() -> str | None:
    rows = db.get_top(10)
    if not rows:
        return None
    lines = ["<b>ТОП МОГГЕРОВ</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        nick = make_mention(row["username"]) if row["username"] else f"id{row['user_id']}"
        place = medals[i] if i < 3 else f"{i + 1}."
        streak = f"  серия {row['streak']}" if (row["streak"] or 0) >= 3 else ""
        lines.append(f"{place} {nick} — <b>{row['wins']}</b> ({get_rank(row['wins']).name}){streak}")
    return "\n".join(lines)


def build_week_text() -> str | None:
    rows = db.get_week_top(10)
    if not rows:
        return None
    lines = ["<b>ТОП НЕДЕЛИ</b>\n<i>победы с понедельника</i>\n"]
    for i, row in enumerate(rows, 1):
        nick = make_mention(row["username"]) if row["username"] else f"id{row['user_id']}"
        lines.append(f"{i}. {nick} — <b>{row['wins']}</b>")
    return "\n".join(lines)


# ---------- ЯДРО БАТЛА ----------
class BattleResult(NamedTuple):
    card: BytesIO
    caption: str
    battle_id: int
    share_text: str


async def play_battle(bot: Bot, p1_id: int, p2_id: int, rematch: bool = False) -> BattleResult:
    """Считает батл двух игроков, пишет результат в БД, возвращает (карточка, подпись, id батла)."""
    if db.pair_battles_recent(p1_id, p2_id, PAIR_WINDOW) >= PAIR_LIMIT:
        raise BattleLimit

    p1_chat = await safe_chat(bot, p1_id)
    p2_chat = await safe_chat(bot, p2_id)
    p1_name = p1_chat.username or p1_chat.first_name or str(p1_id)
    p2_name = p2_chat.username or p2_chat.first_name or str(p2_id)
    p1_html = display(p1_chat.username, p1_chat.first_name, p1_id)
    p2_html = display(p2_chat.username, p2_chat.first_name, p2_id)
    db.upsert_player(p1_id, p1_chat.username or "")
    db.upsert_player(p2_id, p2_chat.username or "")

    async def stats_of(uid: int, chat):
        row = db.get_player(uid)
        prem = row["is_premium"]  # None — бот ещё не видел этого игрока сам
        return await fetch_profile_stats(
            bot, uid,
            username=chat.username or "",
            bio=getattr(chat, "bio", "") or "",
            has_premium=None if prem is None else bool(prem),
            decor=chat_decor(chat),
            battles=row["total_battles"],
            wins=row["wins"],
            invites=row["invites"] or 0,
        )

    p1_stats = await stats_of(p1_id, p1_chat)
    p2_stats = await stats_of(p2_id, p2_chat)
    roll = roll_battle(p1_stats.to_list(), p2_stats.to_list())
    p1_list, p2_list, outcome = roll.a, roll.b, roll.outcome
    p1_score, p2_score = average_score(p1_list), average_score(p2_list)
    is_draw = outcome == "draw"
    p1_wins = outcome != "p2"

    old_rank = {p1_id: get_rank(db.get_player(p1_id)["wins"]).name, p2_id: get_rank(db.get_player(p2_id)["wins"]).name}
    if is_draw:
        db.record_draw(p1_id, p2_id)
    else:
        db.record_result(p1_id if p1_wins else p2_id, p2_id if p1_wins else p1_id)
    battle_id = db.log_battle(p1_id, p2_id, outcome, rematch_used=rematch)

    p1_row, p2_row = db.get_player(p1_id), db.get_player(p2_id)
    p1_rank, p2_rank = get_rank(p1_row["wins"]), get_rank(p2_row["wins"])
    extra = []
    if roll.upset:
        extra.append(f"<b>Апсет</b>: {p1_html if p1_wins else p2_html} обыграл более сильный профиль")
    if not is_draw:
        w_id, w_row, w_html = (p1_id, p1_row, p1_html) if p1_wins else (p2_id, p2_row, p2_html)
        new_rank = get_rank(w_row["wins"]).name
        if new_rank != old_rank[w_id]:
            extra.append(f"Новый ранг: <b>{new_rank}</b> — {w_html}")
        if (w_row["streak"] or 0) >= 3:
            extra.append(f"Серия побед: <b>{w_row['streak']}</b> — {w_html}")
    extra.append(f"<i>Форма дня: {p1_html} {roll.form1:+.1f} · {p2_html} {roll.form2:+.1f}</i>")

    p1_photo = await get_photo_url(bot, p1_id)
    p2_photo = await get_photo_url(bot, p2_id)

    def bio(chat) -> str:
        return (getattr(chat, "bio", "") or "")[:18]

    card = await ig.make_result_card(
        p1_name, p1_photo, p1_rank, p1_list,
        p2_name, p2_photo, p2_rank, p2_list,
        p1_avatar_count=p1_stats.avatar_count, p1_username_len=p1_stats.username_len,
        p1_premium=p1_stats.has_premium, p1_reg_year=p1_stats.reg_year,
        p1_value=p1_stats.profile_value, p1_bio=bio(p1_chat), p1_battles=p1_row["total_battles"],
        p2_avatar_count=p2_stats.avatar_count, p2_username_len=p2_stats.username_len,
        p2_premium=p2_stats.has_premium, p2_reg_year=p2_stats.reg_year,
        p2_value=p2_stats.profile_value, p2_bio=bio(p2_chat), p2_battles=p2_row["total_battles"],
        is_draw=is_draw, upset=roll.upset,
    )
    winner, loser = (p1_html, p2_html) if p1_wins else (p2_html, p1_html)
    caption = build_result_text(winner, loser, p1_score, p2_score, p1_list, p2_list,
                                draw=is_draw, extra="\n".join(extra))
    wn, ln = (p1_name, p2_name) if p1_wins else (p2_name, p1_name)
    if is_draw:
        share_text = f"{wn} и {ln}: ничья {p1_score:.2f}:{p2_score:.2f}"
    else:
        share_text = f"{wn} MOGGED {ln} {max(p1_score, p2_score):.2f}:{min(p1_score, p2_score):.2f}"
    return BattleResult(card, caption, battle_id, share_text + " в MOG BATTLE. А ты сможешь?")


async def play_spar(bot: Bot, user_id: int) -> tuple[BytesIO, str]:
    """Тренировка с ботом: не идёт ни в рейтинг, ни в статистику."""
    chat = await safe_chat(bot, user_id)
    row = db.get_player(user_id)
    prem = row["is_premium"] if row else None
    me = await fetch_profile_stats(
        bot, user_id, username=chat.username or "", bio=getattr(chat, "bio", "") or "",
        has_premium=None if prem is None else bool(prem), decor=chat_decor(chat),
        battles=row["total_battles"] if row else 0, wins=row["wins"] if row else 0,
        invites=(row["invites"] or 0) if row else 0)
    npc_name, npc_stats = random.choice(NPCS)
    roll = roll_battle(me.to_list(), list(npc_stats))
    my_list, npc_list, outcome = roll.a, roll.b, roll.outcome
    s1, s2 = average_score(my_list), average_score(npc_list)
    is_draw, i_win = outcome == "draw", outcome == "p1"
    name = chat.username or chat.first_name or str(user_id)
    card = await ig.make_result_card(
        name, await get_photo_url(bot, user_id), get_rank(row["wins"] if row else 0), my_list,
        npc_name, None, get_score_rank(s2), npc_list,
        p1_avatar_count=me.avatar_count, p1_username_len=me.username_len, p1_premium=me.has_premium,
        p1_reg_year=me.reg_year, p1_value=me.profile_value, p1_bio=(getattr(chat, "bio", "") or "")[:18],
        p1_battles=row["total_battles"] if row else 0,
        p2_premium=False, p2_value=0, p2_battles=0, p2_username_len=len(npc_name),
        is_draw=is_draw,
    )
    me_html = display(chat.username, chat.first_name, user_id)
    npc_html = f"<b>{npc_name}</b>"
    winner, loser = (me_html, npc_html) if i_win else (npc_html, me_html)
    caption = build_result_text(winner, loser, s1, s2, my_list, npc_list, draw=is_draw,
                                extra="<i>Тренировка: в рейтинг не идёт.</i>")
    return card, caption


async def show_card_in_message(call: CallbackQuery, bot: Bot, card, caption: str, kb: InlineKeyboardMarkup) -> None:
    """Подменяет сообщение с кнопкой (обычное или inline) на карточку результата."""
    target_chat_id = call.message.chat.id if call.message else call.from_user.id
    file_id = await upload_card_to_telegram(bot, target_chat_id, card)
    card.seek(0)

    async def edit(media: InputMediaPhoto) -> None:
        if call.inline_message_id:
            await bot.edit_message_media(inline_message_id=call.inline_message_id, media=media, reply_markup=kb)
        elif call.message:
            await call.message.edit_media(media=media, reply_markup=kb)

    if file_id:
        try:
            await edit(InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"))
            return
        except Exception as e:
            logger.warning("Не удалось обновить медиа по file_id: %s", e)
    try:
        card.seek(0)
        await edit(InputMediaPhoto(media=BufferedInputFile(card.read(), filename="result.jpg"),
                                   caption=caption, parse_mode="HTML"))
    except Exception as e:
        logger.warning("Не удалось обновить медиа, правлю текст: %s", e)  # вызов был текстовым
        if call.inline_message_id:
            await bot.edit_message_text(inline_message_id=call.inline_message_id, text=caption, reply_markup=kb)
        elif call.message:
            await call.message.edit_text(caption, reply_markup=kb)


class TrackUserMiddleware(BaseMiddleware):
    """Запоминает игрока и его Premium (Bot API отдаёт is_premium только в апдейтах самого игрока); баны."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                db.upsert_player(user.id, user.username or "", bool(user.is_premium))
                if user.id not in ADMIN_IDS and db.is_banned(user.id):
                    if isinstance(event, CallbackQuery):
                        await event.answer("Доступ ограничен.", show_alert=True)
                    elif isinstance(event, InlineQuery):
                        await event.answer([], cache_time=60)
                    return None
            except Exception as e:
                logger.warning("Не удалось запомнить игрока %s: %s", user.id, e)
        return await handler(event, data)


router = Router()
for _observer in (router.message, router.callback_query, router.inline_query):
    _observer.outer_middleware(TrackUserMiddleware())


# ---------- СТАРТ, МЕНЮ, РЕФЕРАЛКИ ----------
@router.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject, bot: Bot) -> None:
    user = msg.from_user
    arg = command.args or ""
    if arg.startswith("ref_") and arg[4:].isdigit() and db.is_fresh_player(user.id):
        inviter = int(arg[4:])
        if db.add_referral(user.id, inviter):
            try:
                await bot.send_message(
                    inviter, f"По твоей ссылке пришёл {display(user.username, user.first_name, user.id)}. "
                             "Опыт в боте +0.3.")
            except Exception:
                pass
    await msg.answer(WELCOME, reply_markup=main_menu_kb(user.id))


@router.callback_query(F.data.in_({"open_menu", "back_start"}))
async def cb_open_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(WELCOME, reply_markup=main_menu_kb(call.from_user.id))
    await call.answer()


@router.message(Command("invite"))
async def cmd_invite(msg: Message) -> None:
    uid = msg.from_user.id
    invites = db.get_player(uid)["invites"] or 0
    await msg.answer(
        "<b>Пригласи друзей</b>\n\n"
        f"Твоя ссылка:\n<code>{BOT_LINK}?start=ref_{uid}</code>\n\n"
        f"Приглашено: <b>{invites}</b>. За каждого «Опыт в боте» растёт на 0.3 (максимум +2).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить друзьям", url=share_url(uid))]]),
    )


# ---------- ТОПЫ И ПРОФИЛЬ ----------
@router.message(Command("topmog"))
async def cmd_top(msg: Message) -> None:
    await msg.answer(build_top_text() or "Таблица пуста. Первый батл ещё не сыгран.")


@router.message(Command("week"))
async def cmd_week(msg: Message) -> None:
    await msg.answer(build_week_text() or "На этой неделе ещё никто не побеждал.")


@router.message(Command("me"))
async def cmd_me(msg: Message) -> None:
    u = msg.from_user
    row = db.get_player(u.id)
    wins, losses, draws = row["wins"], row["losses"], row["draws"] or 0
    total = wins + losses + draws
    winrate = round(wins * 100 / total) if total else 0
    await msg.answer(
        f"{display(u.username, u.first_name, u.id)}\n\n"
        f"Ранг: <b>{get_rank(wins).name}</b>\n"
        f"Победы {wins} · поражения {losses} · ничьи {draws}\n"
        f"Винрейт: <b>{winrate}%</b> из {total}\n"
        f"Серия: <b>{row['streak'] or 0}</b> (рекорд {row['best_streak'] or 0})\n"
        f"Место в топе: <b>#{db.get_place(u.id)}</b>\n"
        f"Приглашено: {row['invites'] or 0}",
        reply_markup=main_menu_kb(u.id),
    )


@router.message(Command("history"))
async def cmd_history(msg: Message) -> None:
    uid = msg.from_user.id
    rows = db.get_history(uid, 5)
    if not rows:
        await msg.answer("Ты ещё не сыграл ни одного батла.")
        return
    lines = ["<b>Последние батлы</b>\n"]
    for r in rows:
        mine_first = r["p1_id"] == uid
        opp = (r["u2"] if mine_first else r["u1"]) or "аноним"
        if r["outcome"] == "draw":
            res = "ничья"
        elif (r["outcome"] == "p1") == mine_first:
            res = "победа"
        else:
            res = "поражение"
        when = datetime.datetime.fromtimestamp(r["ts"], MSK).strftime("%d.%m %H:%M")
        lines.append(f"{when}  {res} · vs @{html.escape(opp)}")
    await msg.answer("\n".join(lines))


@router.message(Command("how"))
async def cmd_how(msg: Message) -> None:
    await msg.answer(HOW_FULL)


@router.callback_query(F.data == "how")
async def cb_how(call: CallbackQuery) -> None:
    await call.answer(HOW_SHORT, show_alert=True)


@router.message(Command("hide"))
async def cmd_hide(msg: Message) -> None:
    hidden = db.toggle_hidden(msg.from_user.id)
    await msg.answer("Ты скрыт из топов. Батлы работают как раньше." if hidden
                     else "Ты снова в топах.")


# ---------- АДМИНКА ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("stats"))
async def cmd_admin_stats(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    st = db.get_admin_stats()
    await msg.answer(
        "<b>Статистика бота</b>\n\n"
        f"Игроков: <b>{st['players']}</b> (за 24 ч: +{st['new_24h']})\n"
        f"Батлов: <b>{st['battles']}</b> (за 24 ч: {st['battles_24h']}), ничьих: {st['draws']}\n"
        f"Открытых вызовов: {st['open_challenges']}"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot) -> None:
    if not is_admin(msg.from_user.id):
        return
    text = (msg.text or "").partition(" ")[2].strip()
    if not text:
        await msg.answer("Формат: <code>/broadcast текст сообщения</code>")
        return
    ids = db.all_player_ids()
    await msg.answer(f"Рассылаю {len(ids)} игрокам…")
    sent = failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1  # не писал боту / заблокировал
        await asyncio.sleep(0.05)
    await msg.answer(f"Доставлено: {sent}, не доставлено: {failed}")


@router.message(Command("ban", "unban"))
async def cmd_ban(msg: Message, command: CommandObject) -> None:
    if not is_admin(msg.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await msg.answer("Формат: <code>/ban 123456789</code> или <code>/unban 123456789</code>")
        return
    db.set_banned(int(arg), command.command == "ban")
    await msg.answer(("Заблокирован: " if command.command == "ban" else "Разблокирован: ") + arg)


# ---------- ПОЕДИНКИ: ЛИЧКА, /duel, СПАРРИНГ ----------
async def resolve_target(bot: Bot, raw: str) -> int | None:
    raw = raw.strip().lstrip("@")
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    try:
        return (await bot.get_chat(f"@{raw}")).id
    except Exception:
        return None


async def duel_and_reply(msg: Message, bot: Bot, target_id: int) -> None:
    if target_id == msg.from_user.id:
        await msg.answer("Нельзя сразиться с самим собой.")
        return
    try:
        res = await play_battle(bot, msg.from_user.id, target_id)
    except BattleLimit:
        await msg.answer(f"Вы уже сыграли {PAIR_LIMIT} батлов за час. Дай отдохнуть рейтингу.")
        return
    except Exception as e:
        logger.warning("Батл не удался (%s vs %s): %s", msg.from_user.id, target_id, e)
        await msg.answer("Не получилось. Соперник должен хотя бы раз написать боту.")
        return
    await msg.answer_photo(BufferedInputFile(res.card.read(), filename="result.jpg"), caption=res.caption,
                           reply_markup=result_kb(res.battle_id, share_text=res.share_text))


@router.callback_query(F.data == "private_duel")
async def cb_private_duel(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DuelFSM.waiting_target)
    await call.message.answer("Введи <b>@username</b> или числовой <b>ID</b> соперника:")
    await call.answer()


@router.message(DuelFSM.waiting_target)
async def fsm_got_target(msg: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    target_id = await resolve_target(bot, msg.text or "")
    if target_id is None:
        await msg.answer("Пользователь не найден. Попробуй ещё раз.")
        return
    await duel_and_reply(msg, bot, target_id)


@router.message(Command("duel"))
async def cmd_duel(msg: Message, command: CommandObject, bot: Bot) -> None:
    """/duel @username | /duel ID | ответом на сообщение (удобно в группах)."""
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if target and not target.is_bot:
        db.upsert_player(target.id, target.username or "", bool(target.is_premium))
        await duel_and_reply(msg, bot, target.id)
        return
    target_id = await resolve_target(bot, command.args or "")
    if target_id is None:
        await msg.answer("Ответь командой /duel на сообщение соперника или напиши <code>/duel @username</code>.")
        return
    await duel_and_reply(msg, bot, target_id)


async def run_spar(bot: Bot, user_id: int) -> tuple[BytesIO, str] | None:
    now = time.monotonic()
    if now - _last_spar.get(user_id, 0) < 5:
        return None
    _last_spar[user_id] = now
    return await play_spar(bot, user_id)


@router.message(Command("spar"))
async def cmd_spar(msg: Message, bot: Bot) -> None:
    res = await run_spar(bot, msg.from_user.id)
    if not res:
        await msg.answer("Подожди пару секунд.")
        return
    await msg.answer_photo(BufferedInputFile(res[0].read(), filename="spar.jpg"), caption=res[1], reply_markup=spar_kb())


@router.callback_query(F.data == "spar")
async def cb_spar(call: CallbackQuery, bot: Bot) -> None:
    res = await run_spar(bot, call.from_user.id)
    if not res:
        await call.answer("Подожди пару секунд.")
        return
    await call.message.answer_photo(BufferedInputFile(res[0].read(), filename="spar.jpg"),
                                    caption=res[1], reply_markup=spar_kb())
    await call.answer()


# ---------- INLINE-РЕЖИМ ----------
@router.inline_query()
async def inline_handler(query: InlineQuery, bot: Bot) -> None:
    user = query.from_user

    def article(rid: str, title: str, desc: str, text: str | None, fallback: str) -> InlineQueryResultArticle:
        return InlineQueryResultArticle(
            id=rid, title=title, description=desc,
            input_message_content=InputTextMessageContent(message_text=text or fallback, parse_mode="HTML"))

    top_result = article("top", "Топ моггеров", "Рейтинг игроков в чат", build_top_text(), "Таблица пуста.")
    week_result = article("week", "Топ недели", "Победы с понедельника", build_week_text(),
                          "На этой неделе ещё никто не побеждал.")
    q = query.query.strip().lower()
    if q in ("top", "топ", "topmog"):
        await query.answer([top_result, week_result], cache_time=5, is_personal=False)
        return
    if q in ("week", "неделя"):
        await query.answer([week_result, top_result], cache_time=5, is_personal=False)
        return

    rank = get_rank(db.get_player(user.id)["wins"])
    uname = user.username or user.first_name or str(user.id)
    challenge_id = str(uuid.uuid4())[:12]
    db.add_challenge(challenge_id, user.id, uname)
    caption_text = (f"{display(user.username, user.first_name, user.id)} бросает вызов.\n\n"
                    "Кто в этом чате достаточно MOG, чтобы принять его?")

    cached = _card_cache.get(user.id)
    if cached and time.monotonic() - cached[0] < CARD_CACHE_TTL and cached[2] == rank.name:
        file_id = cached[1]  # карточка не менялась — не рендерим и не заливаем заново
    else:
        photo_url = await get_photo_url(bot, user.id)
        card_buf = await ig.make_challenge_card(uname, photo_url, rank)
        file_id = await upload_card_to_telegram(bot, user.id, card_buf)
        if file_id:
            _card_cache[user.id] = (time.monotonic(), file_id, rank.name)
    file_id = file_id or db.kv_get("brand_file_id")  # нет личной карточки — фирменная

    if file_id:
        challenge_result = InlineQueryResultCachedPhoto(
            id=challenge_id, photo_file_id=file_id,
            title="Бросить открытый вызов", description=f"Батл против {uname}",
            caption=caption_text, parse_mode="HTML", reply_markup=accept_kb(challenge_id),
        )
    else:
        challenge_result = InlineQueryResultArticle(
            id=challenge_id, title="Бросить открытый вызов", description=f"Батл против {uname}",
            input_message_content=InputTextMessageContent(message_text=caption_text, parse_mode="HTML"),
            reply_markup=accept_kb(challenge_id),
        )
    await query.answer([challenge_result, top_result], cache_time=1, is_personal=True)


# ---------- ПРИНЯТИЕ ВЫЗОВА ----------
@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(call: CallbackQuery, bot: Bot) -> None:
    challenge_id = call.data.split(":", 1)[1]
    data = db.peek_challenge(challenge_id)

    if not data:
        await call.answer("Вызов истёк или уже принят другим игроком.", show_alert=True)
        return
    if call.from_user.id == data["challenger_id"]:
        await call.answer("Нельзя принять собственный вызов.", show_alert=True)
        return
    p1_id, p2_id = data["challenger_id"], call.from_user.id
    if db.pair_battles_recent(p1_id, p2_id, PAIR_WINDOW) >= PAIR_LIMIT:
        await call.answer(f"Вы уже сыграли {PAIR_LIMIT} батлов за час.", show_alert=True)
        return
    if not db.take_challenge(challenge_id):  # успел другой игрок
        await call.answer("Вызов уже принят другим игроком.", show_alert=True)
        return

    try:
        res = await play_battle(bot, p1_id, p2_id)
        await show_card_in_message(call, bot, res.card, res.caption,
                                   result_kb(res.battle_id, share_text=res.share_text))
        await call.answer("Батл сыгран!")
    except Exception as e:
        logger.error("Ошибка батла по вызову: %s", e)
        await call.answer("Ошибка генерации карточки результата.", show_alert=True)


# ---------- РЕВАНШ ----------
@router.callback_query(F.data.startswith("rm:"))
async def cb_rematch(call: CallbackQuery, bot: Bot) -> None:
    parts = call.data.split(":")
    battle = db.get_battle(int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
    if not battle:
        await call.answer("Эта кнопка устарела — брось новый вызов.", show_alert=True)
        return
    p1_id, p2_id = battle["p1_id"], battle["p2_id"]
    if call.from_user.id not in (p1_id, p2_id):
        await call.answer("Реванш доступен только участникам батла.", show_alert=True)
        return
    if db.pair_battles_recent(p1_id, p2_id, PAIR_WINDOW) >= PAIR_LIMIT:
        await call.answer(f"Вы уже сыграли {PAIR_LIMIT} батлов за час.", show_alert=True)
        return
    if not db.use_rematch(battle["id"]):  # второй клик или второй игрок
        await call.answer("Реванш уже был сыгран.", show_alert=True)
        return
    try:
        res = await play_battle(bot, p1_id, p2_id, rematch=True)
        await show_card_in_message(call, bot, res.card, res.caption,
                                   result_kb(res.battle_id, rematch=False, share_text=res.share_text))
        await call.answer("Реванш сыгран!")
    except Exception as e:
        logger.error("Ошибка реванша: %s", e)
        await call.answer("Не получилось сыграть реванш.", show_alert=True)


# ---------- ФОН: БЭКАПЫ, ЧИСТКА, ЗАСТАВКА ----------
async def backup_loop() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    while True:
        try:
            path = os.path.join(BACKUP_DIR, time.strftime("ghostclash-%Y%m%d-%H%M.db"))
            await asyncio.to_thread(db.backup_to, path)
            for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "ghostclash-*.db")))[:-BACKUP_KEEP]:
                os.remove(old)
            logger.info("Бэкап базы: %s", path)
        except Exception as e:
            logger.error("Бэкап не удался: %s", e)
        await asyncio.sleep(24 * 3600)


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            removed = await asyncio.to_thread(db.cleanup_challenges)
            for uid in [u for u, v in _card_cache.items() if time.monotonic() - v[0] > CARD_CACHE_TTL]:
                _card_cache.pop(uid, None)
            if removed:
                logger.info("Удалено просроченных вызовов: %s", removed)
        except Exception as e:
            logger.error("Чистка не удалась: %s", e)


async def ensure_brand_card(bot: Bot) -> None:
    """Один раз заливает фирменную карточку-заглушку и запоминает её file_id."""
    if db.kv_get("brand_file_id") or not CACHE_CHAT_ID:
        return
    file_id = await _upload_card(bot, CACHE_CHAT_ID, ig.make_brand_card())
    if file_id:
        db.kv_set("brand_file_id", file_id)
        logger.info("Фирменная карточка загружена")


async def setup_bot_profile(bot: Bot) -> None:
    """Меню команд и описание бота (видно на странице бота и в пустом чате). Поля бота — только наши."""
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="duel", description="Батл с игроком (ответом на сообщение)"),
            BotCommand(command="spar", description="Тренировка с ботом"),
            BotCommand(command="me", description="Мой ранг и статистика"),
            BotCommand(command="topmog", description="Топ моггеров"),
            BotCommand(command="week", description="Топ недели"),
            BotCommand(command="history", description="Мои последние батлы"),
            BotCommand(command="invite", description="Пригласить друзей"),
            BotCommand(command="how", description="Как считается батл"),
            BotCommand(command="hide", description="Скрыть себя из топов"),
        ])
        await bot.set_my_short_description("Батл Telegram-профилей: кто круче? Бросай вызов друзьям в любом чате.")
        await bot.set_my_description(
            "MOG BATTLE сравнивает Telegram-профили: аватар, ник, био, Premium, оформление и возраст аккаунта.\n\n"
            "Вызывай друзей в любом чате командой @MOGGEDSTARSBOT mogg, побеждай, поднимайся в рейтинге "
            "и получай ранги от Sub 3 до True Adam.")
    except Exception as e:
        logger.warning("Не удалось обновить профиль бота: %s", e)


# ---------- ЗАПУСК ----------
async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        logger.exception("Ошибка при обработке апдейта: %s", event.exception)
        upd = event.update
        try:
            if upd.callback_query:
                await upd.callback_query.answer("Что-то пошло не так, попробуй ещё раз.", show_alert=True)
            elif upd.message:
                await upd.message.answer("Что-то пошло не так, попробуй ещё раз.")
        except Exception:
            pass
        return True

    await setup_bot_profile(bot)
    asyncio.create_task(backup_loop())
    asyncio.create_task(cleanup_loop())
    asyncio.create_task(ensure_brand_card(bot))  # не блокирует старт
    logger.info("MOG BATTLE запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
