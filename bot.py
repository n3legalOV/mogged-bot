"""
MOG BATTLE — Telegram Profile Duel Bot (@MOGGEDSTARSBOT)
aiogram 3.x | Python 3.10+
"""

import asyncio
import glob
import logging
import os
import time
import uuid
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import BaseMiddleware
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
from stats import roll_battle, fetch_profile_stats, average_score, get_rank

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL файла виден токен
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CACHE_CHAT_ID = int(os.getenv("CACHE_CHAT_ID") or 0)
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/em07kid")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
BACKUP_DIR = "backups"
BACKUP_KEEP = 14

SHARE_URL = ("https://t.me/share/url?url=https://t.me/MOGGEDSTARSBOT"
             "&text=%E2%9A%94%EF%B8%8F%20%D0%9A%D1%82%D0%BE%20%D0%BA%D1%80%D1%83%D1%87%D0%B5%20%E2%80%94"
             "%20%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D0%B8%D0%BC%20%D0%BF%D1%80%D0%BE%D1%84%D0%B8%D0%BB%D0%B8")

class DuelFSM(StatesGroup):
    waiting_target = State()


# ---------- ХЕЛПЕРЫ ----------
def make_mention(username: str) -> str:
    """Создаёт кликабельную ссылку на профиль"""
    if not username:
        return "ник"
    return f'<a href="https://t.me/{username}">@{username}</a>'


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
        logger.error("Не удалось загрузить карточку в Telegram (chat_id=%s): %s", chat_id, e)
        return None


async def upload_card_to_telegram(bot: Bot, chat_id: int, card_buf) -> str | None:
    file_id = await _upload_card(bot, chat_id, card_buf)
    if not file_id and CACHE_CHAT_ID and chat_id != CACHE_CHAT_ID:
        file_id = await _upload_card(bot, CACHE_CHAT_ID, card_buf)  # игрок ещё не писал боту
    return file_id


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
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Вызов всем", switch_inline_query="mogg"),
         InlineKeyboardButton(text="🎯 Вызов игроку", switch_inline_query="@")],
        [InlineKeyboardButton(text="👤 Батл в личке", callback_data="private_duel"),
         InlineKeyboardButton(text="🏆 Топ", switch_inline_query="top")],
        [InlineKeyboardButton(text="📤 Поделиться ботом", url=SHARE_URL)],
        [InlineKeyboardButton(text="💜 Поддержать", url=SUPPORT_URL),
         InlineKeyboardButton(text="◀️ Назад", callback_data="back_start")],
    ])


def result_kb(battle_id: int, rematch: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if rematch:
        rows.append([InlineKeyboardButton(text="🔁 Реванш (1 раз)", callback_data=f"rm:{battle_id}")])
    rows.append([InlineKeyboardButton(text="⚔️ Новый вызов", switch_inline_query="mogg")])
    rows.append([InlineKeyboardButton(text="📤 Поделиться ботом", url=SHARE_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def accept_kb(challenge_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Принять вызов", callback_data=f"accept:{challenge_id}")],
    ])


def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ MogBattle", callback_data="open_menu")],
    ])


# ---------- ТЕКСТЫ ----------
def build_result_text(winner_name: str, loser_name: str, s1: float, s2: float,
                      stats1: list[float], stats2: list[float], draw: bool = False) -> str:
    if draw:
        return (f"🤝 <b>НИЧЬЯ</b>: {make_mention(winner_name)} и {make_mention(loser_name)}\n\n"
                f"📊 <code>{s1:.2f} vs {s2:.2f}</code>\n\n"
                f"<i>Профили на одном уровне. Реванш?</i>")
    labels = ["📸 Аватар", "📛 @username", "📝 О себе", "💎 Premium", "🎨 Оформление", "📅 Дата рег.", "📊 Опыт"]
    strong = []
    for i, (v1, v2) in enumerate(zip(stats1, stats2)):
        if s1 >= s2 and v1 > v2:
            strong.append(labels[i].lower())
        elif s2 > s1 and v2 > v1:
            strong.append(labels[i].lower())
    diff = abs(s1 - s2)
    strong_text = ""
    if len(strong) == 1:
        strong_text = f"💪 <b>{strong[0]}</b> решает всё."
    elif len(strong) > 1:
        strong_text = f"🔥 Сильнее всего: <b>{', '.join(strong[:-1])} и {strong[-1]}</b>."
    advantage_text = " 📈 Заметное преимущество!" if diff >= 0.8 else ""
    summary_line = f"{strong_text}{advantage_text}" if strong_text else ""
    return (f"🏆 <b>{make_mention(winner_name)}</b> <b>MOGGED</b> <b>{make_mention(loser_name)}</b>!\n\n"
            f"📊 <code>{s1:.2f} vs {s2:.2f}  •  +{diff:.2f}</code>\n"
            f"{summary_line}\n\n"
            f"<i>Сводка по аватару, нику, био, Premium, оформлению, дате регистрации и опыту в боте.</i>")


def build_top_text() -> str | None:
    rows = db.get_top(10)
    if not rows:
        return None
    lines = ["🏆 <b>ТОП МОГГЕРОВ</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        rank = get_rank(row["wins"])
        nick = row["username"]
        nick_link = make_mention(nick) if nick else f"id{row['user_id']}"
        medal = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} {nick_link} — <b>{row['wins']}</b> побед ({rank.name})")
    return "\n".join(lines)


# ---------- ЯДРО БАТЛА ----------
async def play_battle(bot: Bot, p1_id: int, p2_id: int, rematch: bool = False) -> tuple[BytesIO, str, int]:
    """Считает батл двух игроков, пишет результат в БД, возвращает (карточка, подпись, id батла)."""
    p1_chat = await bot.get_chat(p1_id)
    p2_chat = await bot.get_chat(p2_id)
    p1_name = p1_chat.username or p1_chat.first_name or str(p1_id)
    p2_name = p2_chat.username or p2_chat.first_name or str(p2_id)
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
        )

    p1_stats = await stats_of(p1_id, p1_chat)
    p2_stats = await stats_of(p2_id, p2_chat)
    p1_list, p2_list, outcome = roll_battle(p1_stats.to_list(), p2_stats.to_list())
    p1_score, p2_score = average_score(p1_list), average_score(p2_list)
    is_draw = outcome == "draw"
    p1_wins = outcome != "p2"

    if is_draw:
        db.record_draw(p1_id, p2_id)
    else:
        db.record_result(p1_id if p1_wins else p2_id, p2_id if p1_wins else p1_id)
    battle_id = db.log_battle(p1_id, p2_id, outcome, rematch_used=rematch)

    p1_rank = get_rank(db.get_player(p1_id)["wins"])
    p2_rank = get_rank(db.get_player(p2_id)["wins"])
    p1_photo = await get_photo_url(bot, p1_id)
    p2_photo = await get_photo_url(bot, p2_id)

    def bio(chat) -> str:
        b = getattr(chat, "bio", "") or ""
        return b[:18]

    card = await ig.make_result_card(
        p1_name, p1_photo, p1_rank, p1_list,
        p2_name, p2_photo, p2_rank, p2_list,
        p1_avatar_count=p1_stats.avatar_count, p1_username_len=p1_stats.username_len,
        p1_premium=p1_stats.has_premium, p1_reg_year=p1_stats.reg_year,
        p1_value=p1_stats.profile_value, p1_bio=bio(p1_chat), p1_battles=db.get_player(p1_id)["total_battles"],
        p2_avatar_count=p2_stats.avatar_count, p2_username_len=p2_stats.username_len,
        p2_premium=p2_stats.has_premium, p2_reg_year=p2_stats.reg_year,
        p2_value=p2_stats.profile_value, p2_bio=bio(p2_chat), p2_battles=db.get_player(p2_id)["total_battles"],
        is_draw=is_draw,
    )
    winner_name, loser_name = (p1_name, p2_name) if p1_wins else (p2_name, p1_name)
    caption = build_result_text(winner_name, loser_name, p1_score, p2_score, p1_list, p2_list, draw=is_draw)
    return card, caption, battle_id


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
    """Bot API отдаёт is_premium только в апдейтах самого игрока — запоминаем его в БД."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                db.upsert_player(user.id, user.username or "", bool(user.is_premium))
            except Exception as e:
                logger.warning("Не удалось запомнить игрока %s: %s", user.id, e)
        return await handler(event, data)


router = Router()
for _observer in (router.message, router.callback_query, router.inline_query):
    _observer.outer_middleware(TrackUserMiddleware())


# ---------- СТАРТ И МЕНЮ ----------
@router.message(CommandStart())
async def cmd_start(msg: Message) -> None:
    db.upsert_player(msg.from_user.id, msg.from_user.username or "")
    name = msg.from_user.first_name or msg.from_user.username or "чел"
    await msg.answer(
        f"👋 <b>{name}</b>, добро пожаловать!\n\n"
        "Здесь ты можешь сравнивать профили и доказывать, кто круче.\n"
        "Выбери игру ниже:",
        reply_markup=start_kb(),
    )


@router.callback_query(F.data == "open_menu")
async def cb_open_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>⚔️ MOGBATTLE</b>\n\n"
        "Сравни Telegram-профили, брось вызов друзьям и докажи, кто настоящий MOG.\n\n"
        "Бот оценивает сам профиль, а затем определяет победителя и создаёт карточку батла. "
        "На близких профилях возможна ничья.\n\n"
        "<b>Быстрый вызов из любого чата:</b>\n"
        "@MOGGEDSTARSBOT mogg — позвать на батл всех желающих\n\n"
        "Рейтинг игроков: /topmog · твоя статистика: /me",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "back_start")
async def cb_back_start(call: CallbackQuery) -> None:
    name = call.from_user.first_name or call.from_user.username or "чел"
    await call.message.edit_text(
        f"👋 <b>{name}</b>, добро пожаловать!\n\n"
        "Здесь ты можешь сравнивать профили и доказывать, кто круче.\n"
        "Выбери игру ниже:",
        reply_markup=start_kb(),
    )
    await call.answer()


# ---------- ТОП И СТАТИСТИКА ----------
@router.message(Command("topmog"))
async def cmd_top(msg: Message) -> None:
    await msg.answer(build_top_text() or "📭 Таблица пуста. Первый батл ещё не сыгран.")


@router.message(Command("me"))
async def cmd_me(msg: Message) -> None:
    u = msg.from_user
    db.upsert_player(u.id, u.username or "")
    row = db.get_player(u.id)
    wins, losses, draws = row["wins"], row["losses"], row["draws"] or 0
    total = wins + losses + draws
    winrate = round(wins * 100 / total) if total else 0
    rank = get_rank(wins)
    await msg.answer(
        f"👤 <b>{make_mention(u.username) if u.username else u.first_name}</b>\n\n"
        f"🎖 Ранг: <b>{rank.name}</b>\n"
        f"🏆 Победы: <b>{wins}</b>  ·  💀 Поражения: <b>{losses}</b>  ·  🤝 Ничьи: <b>{draws}</b>\n"
        f"📈 Винрейт: <b>{winrate}%</b> ({total} батлов)\n"
        f"📍 Место в топе: <b>#{db.get_place(u.id)}</b>",
        reply_markup=start_kb(),
    )


# ---------- АДМИНКА ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("stats"))
async def cmd_admin_stats(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    st = db.get_admin_stats()
    await msg.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Игроков: <b>{st['players']}</b> (за 24 ч: +{st['new_24h']})\n"
        f"⚔️ Батлов: <b>{st['battles']}</b> (за 24 ч: {st['battles_24h']}), из них ничьих: {st['draws']}\n"
        f"⏳ Открытых вызовов: {st['open_challenges']}"
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
    await msg.answer(f"📣 Рассылаю {len(ids)} игрокам…")
    sent = failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1  # не писал боту / заблокировал
        await asyncio.sleep(0.05)
    await msg.answer(f"✅ Доставлено: {sent}, не доставлено: {failed}")


# ---------- ЛИЧНЫЙ ПОЕДИНОК ----------
@router.callback_query(F.data == "private_duel")
async def cb_private_duel(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DuelFSM.waiting_target)
    await call.message.answer("Введи <b>@username</b> или числовой <b>ID</b> соперника:")
    await call.answer()


@router.message(DuelFSM.waiting_target)
async def fsm_got_target(msg: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    raw = (msg.text or "").strip().lstrip("@")

    try:
        target_id = int(raw)
    except ValueError:
        try:
            target_id = (await bot.get_chat(f"@{raw}")).id
        except Exception:
            await msg.answer("❌ Пользователь не найден. Попробуй ещё раз.")
            return

    if target_id == msg.from_user.id:
        await msg.answer("🪞 Нельзя сразиться с самим собой.")
        return

    try:
        card, caption, battle_id = await play_battle(bot, msg.from_user.id, target_id)
    except Exception as e:
        logger.warning("Батл в личке не удался (%s vs %s): %s", msg.from_user.id, target_id, e)
        await msg.answer("❌ Не вижу этого игрока. Он должен хотя бы раз написать боту.")
        return
    await msg.answer_photo(BufferedInputFile(card.read(), filename="result.jpg"),
                           caption=caption, reply_markup=result_kb(battle_id))


# ---------- INLINE-РЕЖИМ ----------
@router.inline_query()
async def inline_handler(query: InlineQuery, bot: Bot) -> None:
    user = query.from_user

    top_text = build_top_text() or "📭 Таблица пуста. Первый батл ещё не сыгран."
    top_result = InlineQueryResultArticle(
        id="top",
        title="🏆 Топ моггеров",
        description="Показать рейтинг игроков в чат",
        input_message_content=InputTextMessageContent(message_text=top_text, parse_mode="HTML"),
    )

    if query.query.strip().lower() in ("top", "топ", "topmog"):
        await query.answer([top_result], cache_time=5, is_personal=False)
        return

    db.upsert_player(user.id, user.username or "")
    rank = get_rank(db.get_player(user.id)["wins"])
    photo_url = await get_photo_url(bot, user.id)
    uname = user.username or user.first_name or str(user.id)

    challenge_id = str(uuid.uuid4())[:12]
    db.add_challenge(challenge_id, user.id, uname)

    card_buf = await ig.make_challenge_card(uname, photo_url, rank)
    file_id = await upload_card_to_telegram(bot, user.id, card_buf)
    caption_text = f"⚔️ <b>{make_mention(uname)}</b> бросает вызов!\n\nКто в этом чате достаточно MOG, чтобы принять его?"
    fallback_id = db.kv_get("brand_file_id")

    if file_id or fallback_id:
        challenge_result = InlineQueryResultCachedPhoto(
            id=challenge_id,
            photo_file_id=file_id or fallback_id,  # нет личной карточки — фирменная
            title="⚔️ Бросить открытый вызов",
            description=f"Битва против @{uname}",
            caption=caption_text,
            parse_mode="HTML",
            reply_markup=accept_kb(challenge_id),
        )
    else:
        challenge_result = InlineQueryResultArticle(
            id=challenge_id,
            title="⚔️ Бросить открытый вызов",
            description=f"Битва против @{uname}",
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
        await call.answer("⏳ Вызов истёк или уже принят другим игроком.", show_alert=True)
        return
    if call.from_user.id == data["challenger_id"]:
        await call.answer("🙅 Нельзя принять собственный вызов.", show_alert=True)
        return
    if not db.take_challenge(challenge_id):  # успел другой игрок
        await call.answer("⏳ Вызов уже принят другим игроком.", show_alert=True)
        return

    p1_id, p2_id = data["challenger_id"], call.from_user.id
    try:
        card, caption, battle_id = await play_battle(bot, p1_id, p2_id)
        await show_card_in_message(call, bot, card, caption, result_kb(battle_id))
        await call.answer("⚔️ Батл успешно завершен!")
    except Exception as e:
        logger.error("Ошибка батла по вызову: %s", e)
        await call.answer("❌ Ошибка генерации карточки результата.", show_alert=True)


# ---------- РЕВАНШ ----------
@router.callback_query(F.data.startswith("rm:"))
async def cb_rematch(call: CallbackQuery, bot: Bot) -> None:
    parts = call.data.split(":")
    battle = db.get_battle(int(parts[1])) if len(parts) == 2 and parts[1].isdigit() else None
    if not battle:
        await call.answer("⌛ Эта кнопка устарела — брось новый вызов.", show_alert=True)
        return
    p1_id, p2_id = battle["p1_id"], battle["p2_id"]
    if call.from_user.id not in (p1_id, p2_id):
        await call.answer("🔒 Реванш доступен только участникам батла.", show_alert=True)
        return
    if not db.use_rematch(battle["id"]):  # второй клик или второй игрок
        await call.answer("🔁 Реванш уже был сыгран.", show_alert=True)
        return
    try:
        card, caption, battle_id = await play_battle(bot, p1_id, p2_id, rematch=True)
        await show_card_in_message(call, bot, card, caption, result_kb(battle_id, rematch=False))
        await call.answer("🔁 Реванш сыгран!")
    except Exception as e:
        logger.error("Ошибка реванша: %s", e)
        await call.answer("❌ Не получилось сыграть реванш.", show_alert=True)


# ---------- ФОН: БЭКАПЫ И ЗАСТАВКА ----------
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


async def ensure_brand_card(bot: Bot) -> None:
    """Один раз заливает фирменную карточку-заглушку и запоминает её file_id."""
    if db.kv_get("brand_file_id") or not CACHE_CHAT_ID:
        return
    file_id = await _upload_card(bot, CACHE_CHAT_ID, ig.make_brand_card())
    if file_id:
        db.kv_set("brand_file_id", file_id)
        logger.info("Фирменная карточка загружена")


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
                await upd.callback_query.answer("⚠️ Что-то пошло не так, попробуй ещё раз.", show_alert=True)
            elif upd.message:
                await upd.message.answer("⚠️ Что-то пошло не так, попробуй ещё раз.")
        except Exception:
            pass
        return True

    asyncio.create_task(backup_loop())
    asyncio.create_task(ensure_brand_card(bot))  # не блокирует старт
    logger.info("MOG BATTLE запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
