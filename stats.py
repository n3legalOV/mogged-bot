"""
stats.py — реальная оценка профиля с псевдо-реальными данными
"""
import hashlib
import random
import logging
from dataclasses import dataclass
from typing import Optional
from aiogram import Bot

logger = logging.getLogger(__name__)

RANKS = [
    (0,   2,  "Sub 3",      "#6B6B6B"),
    (3,   5,  "Sub 5",      "#CD7F32"),
    (6,   10, "Ltn",        "#A8A9AD"),
    (11,  20, "Mtn",        "#C0C0C0"),
    (21,  35, "Htn",        "#FFD700"),
    (36,  55, "Chad",       "#00BFFF"),
    (56,  10**9, "True Adam", "#FF2D2D"),
]
SCORE_RANKS = [
    (0.0,  2.0,  "Sub 3",      "#6B6B6B"),
    (2.0,  3.5,  "Sub 5",      "#CD7F32"),
    (3.5,  5.0,  "Ltn",        "#A8A9AD"),
    (5.0,  6.0,  "Mtn",        "#C0C0C0"),
    (6.0,  7.0,  "Htn",        "#FFD700"),
    (7.0,  8.5,  "Chad",       "#00BFFF"),
    (8.5,  10.01, "True Adam", "#FF2D2D"),
]
STAT_LABELS = ["Аватар","@username","О себе","Premium","Оформление","Дата рег.","Опыт в боте"]

@dataclass
class Rank:
    name: str
    color: str

@dataclass
class ProfileStats:
    avatar_score:   float
    username_score: float
    bio_score:      float
    gifts_score:    float
    value_score:    float
    reg_score:      float
    level_score:    float
    avatar_count:   int
    username_len:   int
    has_premium:    Optional[bool]
    reg_year:       int
    user_id:        int
    gifts_count:    int
    nft_count:      int
    profile_value:  float

    def to_list(self) -> list[float]:
        return [self.avatar_score, self.username_score, self.bio_score,
                self.gifts_score, self.value_score, self.reg_score, self.level_score]

def get_rank(wins: int) -> Rank:
    for lo, hi, name, color in RANKS:
        if lo <= wins <= hi:
            return Rank(name, color)
    return Rank("Sub 3", "#6B6B6B")

def get_score_rank(score: float) -> Rank:
    for lo, hi, name, color in SCORE_RANKS:
        if lo <= score < hi:
            return Rank(name, color)
    return Rank("Sub 3", "#6B6B6B")

def _estimate_reg_year(user_id: int) -> int:
    thresholds = [
        (100_000_000, 2013), (200_000_000, 2014), (400_000_000, 2015),
        (600_000_000, 2016), (800_000_000, 2017), (1_000_000_000, 2018),
        (1_500_000_000, 2019), (2_000_000_000, 2020), (3_000_000_000, 2021),
        (4_000_000_000, 2022), (5_500_000_000, 2023), (7_000_000_000, 2024),
    ]
    for threshold, year in thresholds:
        if user_id < threshold:
            return year
    return 2025

def _reg_score(user_id: int) -> tuple[float, int]:
    year = _estimate_reg_year(user_id)
    score = max(1.0, 10.0 - (year - 2013) * 0.75)
    return round(score, 2), year

def _username_score(username: Optional[str]) -> tuple[float, int]:
    if not username:
        return 0.0, 0
    length = len(username)
    if length <= 4: score = 10.0
    elif length <= 6: score = 8.5
    elif length <= 8: score = 7.0
    elif length <= 12: score = 5.5
    elif length <= 16: score = 3.5
    else: score = 1.5
    if username.isalpha():
        score = min(10.0, score + 0.8)
    if any(c.isdigit() for c in username):
        score -= 1.0
    if "_" in username:
        score -= 0.5
    score = max(0.5, score)
    return round(score, 2), length

def _exp_score(battles: int, wins: int, invites: int = 0) -> float:
    """Опыт в боте: батлы, победы и приглашённые друзья (реальные данные из БД)."""
    return round(min(10.0, 2.0 + battles * 0.25 + wins * 0.15 + min(2.0, invites * 0.3)), 2)

def _premium_score(has_premium: Optional[bool]) -> float:
    if has_premium is None:  # бот этого игрока ещё не видел — нейтрально
        return 4.0
    return 10.0 if has_premium else 2.0

def _decor_score(decor: int) -> float:
    """decor — сколько из 4 элементов оформления заполнено (0..4)."""
    return round(min(10.0, 2.0 + 2.0 * decor), 2)

async def fetch_profile_stats(
    bot: Bot, user_id: int,
    username: Optional[str] = None,
    bio: Optional[str] = None,
    has_premium: Optional[bool] = False,
    decor: int = 0,
    battles: int = 0,
    wins: int = 0,
    invites: int = 0,
) -> ProfileStats:
    avatar_count = 0
    avatar_score = 0.0
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=10)
        avatar_count = photos.total_count
        if avatar_count == 0: avatar_score = 0.0
        elif avatar_count == 1: avatar_score = 4.0
        elif avatar_count <= 3: avatar_score = 6.5
        elif avatar_count <= 7: avatar_score = 8.0
        else: avatar_score = 10.0
    except Exception as e:
        logger.warning("Фото %s: %s", user_id, e)

    u_score, u_len = _username_score(username)

    if not bio: bio_score = 3.0
    elif len(bio) < 10: bio_score = 5.0
    elif len(bio) < 50: bio_score = 7.5
    elif len(bio) < 120: bio_score = 8.5
    else: bio_score = 7.0  # простыня хуже цепкой строчки

    reg_sc, reg_year = _reg_score(user_id)
    gifts_score = _premium_score(has_premium)
    value_score = _decor_score(decor)

    return ProfileStats(
        avatar_score=round(avatar_score,2),
        username_score=u_score,
        bio_score=round(bio_score,2),
        gifts_score=round(gifts_score,2),
        value_score=value_score,
        reg_score=reg_sc,
        level_score=_exp_score(battles, wins, invites),
        avatar_count=avatar_count,
        username_len=u_len,
        has_premium=has_premium,
        reg_year=reg_year,
        user_id=user_id,
        gifts_count=0,
        nft_count=0,
        profile_value=float(decor),
    )

# вес параметров: аватар, ник, био, Premium, оформление, дата рег., опыт
WEIGHTS = [1.0, 1.3, 0.8, 1.2, 0.8, 1.2, 0.7]

def average_score(stats: list[float]) -> float:
    """Взвешенное среднее: ник, Premium и «возраст» аккаунта весят больше био и опыта."""
    w = WEIGHTS[:len(stats)]
    return round(sum(v * k for v, k in zip(stats, w)) / sum(w), 2)

DRAW_EPS = 0.15

def roll_battle(l1: list[float], l2: list[float]) -> tuple[list[float], list[float], str]:
    """Добавляет «форму дня» (случайный сдвиг) и определяет исход: p1 / p2 / draw."""
    def jitter(lst: list[float]) -> list[float]:
        shift = random.uniform(-0.8, 0.8)
        return [round(min(10.0, max(0.0, v + shift + random.uniform(-0.4, 0.4))), 2) for v in lst]
    a, b = jitter(l1), jitter(l2)
    s1, s2 = average_score(a), average_score(b)
    if abs(s1 - s2) < DRAW_EPS:
        return a, b, "draw"
    return a, b, "p1" if s1 > s2 else "p2"
