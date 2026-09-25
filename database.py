import datetime
import sqlite3
import logging
from typing import Optional

DB_PATH = "ghostclash.db"

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT    DEFAULT '',
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                total_battles INTEGER DEFAULT 0
            )
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(players)")}
        if "draws" not in cols:
            conn.execute("ALTER TABLE players ADD COLUMN draws INTEGER DEFAULT 0")
        for col, ddl in (("streak", "INTEGER DEFAULT 0"), ("best_streak", "INTEGER DEFAULT 0"),
                         ("invited_by", "INTEGER"), ("invites", "INTEGER DEFAULT 0"),
                         ("hidden", "INTEGER DEFAULT 0"), ("banned", "INTEGER DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE players ADD COLUMN {col} {ddl}")
        if "is_premium" not in cols:
            conn.execute("ALTER TABLE players ADD COLUMN is_premium INTEGER")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE players ADD COLUMN created_at INTEGER")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS battles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER DEFAULT (strftime('%s','now')),
                p1_id   INTEGER,
                p2_id   INTEGER,
                outcome TEXT,
                rematch_used INTEGER DEFAULT 0
            )
        """)
        bcols = {r[1] for r in conn.execute("PRAGMA table_info(battles)")}
        if "rematch_used" not in bcols:
            conn.execute("ALTER TABLE battles ADD COLUMN rematch_used INTEGER DEFAULT 0")
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS challenges (
                id              TEXT PRIMARY KEY,
                challenger_id   INTEGER NOT NULL,
                challenger_name TEXT    DEFAULT '',
                created_at      INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_players_wins ON players(wins DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_battles_ts ON battles(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_battles_pair ON battles(p1_id, p2_id, ts)")
        if "target_username" not in {r[1] for r in conn.execute("PRAGMA table_info(challenges)")}:
            conn.execute("ALTER TABLE challenges ADD COLUMN target_username TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_challenges_created ON challenges(created_at)")
        conn.commit()
    cleanup_challenges()
    logger.info("DB initialised")


def upsert_player(user_id: int, username: str = "", is_premium: Optional[bool] = None) -> None:
    """is_premium известен только из апдейтов самого игрока; None не затирает сохранённое."""
    prem = None if is_premium is None else int(bool(is_premium))
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO players (user_id, username, is_premium, created_at)
            VALUES (?, ?, ?, strftime('%s','now'))
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                is_premium = COALESCE(excluded.is_premium, players.is_premium)
        """, (user_id, username or "", prem))
        conn.commit()


def record_result(winner_id: int, loser_id: int) -> None:
    with get_conn() as conn:
        conn.execute("""
            UPDATE players SET wins = wins + 1, total_battles = total_battles + 1,
                streak = COALESCE(streak, 0) + 1,
                best_streak = MAX(COALESCE(best_streak, 0), COALESCE(streak, 0) + 1)
            WHERE user_id = ?
        """, (winner_id,))
        conn.execute("""
            UPDATE players SET losses = losses + 1, total_battles = total_battles + 1, streak = 0
            WHERE user_id = ?
        """, (loser_id,))
        conn.commit()


def get_player(user_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_top(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE hidden = 0 AND banned = 0 ORDER BY wins DESC, losses ASC LIMIT ?", (limit,)
        ).fetchall()


def record_draw(a_id: int, b_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE players SET draws = draws + 1, total_battles = total_battles + 1 "
            "WHERE user_id IN (?, ?)", (a_id, b_id))
        conn.commit()


def add_challenge(cid: str, challenger_id: int, name: str, target_username: Optional[str] = None) -> None:
    """target_username — адресный вызов: принять может только игрок с этим ником."""
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO challenges (id, challenger_id, challenger_name, target_username) "
                     "VALUES (?, ?, ?, ?)", (cid, challenger_id, name, target_username))
        conn.commit()


def peek_challenge(cid: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM challenges WHERE id = ?", (cid,)).fetchone()


def take_challenge(cid: str) -> Optional[sqlite3.Row]:
    """Атомарно забирает вызов: второй одновременный клик получит None."""
    with get_conn() as conn:
        row = conn.execute("DELETE FROM challenges WHERE id = ? RETURNING *", (cid,)).fetchone()
        conn.commit()
        return row


def get_place(user_id: int) -> int:
    """Место в общем топе (по победам); одинаковые победы делят место."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) + 1 FROM players WHERE hidden = 0 AND banned = 0 "
            "AND wins > (SELECT wins FROM players WHERE user_id = ?)",
            (user_id,)).fetchone()
        return row[0]


def log_battle(p1_id: int, p2_id: int, outcome: str, rematch_used: bool = False) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO battles (p1_id, p2_id, outcome, rematch_used) VALUES (?, ?, ?, ?)",
                           (p1_id, p2_id, outcome, int(rematch_used)))
        conn.commit()
        return cur.lastrowid


def get_battle(battle_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM battles WHERE id = ?", (battle_id,)).fetchone()


def use_rematch(battle_id: int) -> bool:
    """Атомарно отмечает реванш использованным: True только у первого нажавшего."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE battles SET rematch_used = 1 WHERE id = ? AND rematch_used = 0", (battle_id,))
        conn.commit()
        return cur.rowcount == 1


def get_admin_stats() -> dict:
    with get_conn() as conn:
        one = lambda q: conn.execute(q).fetchone()[0] or 0
        return {
            "players": one("SELECT COUNT(*) FROM players"),
            "new_24h": one("SELECT COUNT(*) FROM players WHERE created_at > strftime('%s','now') - 86400"),
            "battles": one("SELECT COUNT(*) FROM battles"),
            "battles_24h": one("SELECT COUNT(*) FROM battles WHERE ts > strftime('%s','now') - 86400"),
            "draws": one("SELECT COUNT(*) FROM battles WHERE outcome = 'draw'"),
            "open_challenges": one("SELECT COUNT(*) FROM challenges"),
        }


def all_player_ids() -> list[int]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM players")]


def kv_get(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None


def kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))
        conn.commit()


def backup_to(path: str) -> None:
    """Согласованная копия базы (sqlite backup API безопасен при работающем боте)."""
    with get_conn() as src, sqlite3.connect(path) as dst:
        src.backup(dst)


CHALLENGE_TTL = 48 * 3600


def cleanup_challenges() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM challenges WHERE created_at < strftime('%s','now') - ?", (CHALLENGE_TTL,))
        conn.commit()
        return cur.rowcount


def _week_start_ts() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    monday = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


def get_week_top(limit: int = 10) -> list[sqlite3.Row]:
    """Победы с понедельника 00:00 UTC (из журнала батлов)."""
    since = _week_start_ts()
    with get_conn() as conn:
        return conn.execute("""
            SELECT p.user_id, p.username, COUNT(*) AS wins FROM (
                SELECT p1_id AS uid FROM battles WHERE outcome = 'p1' AND ts >= ?
                UNION ALL
                SELECT p2_id FROM battles WHERE outcome = 'p2' AND ts >= ?
            ) w JOIN players p ON p.user_id = w.uid
            WHERE p.hidden = 0 AND p.banned = 0
            GROUP BY p.user_id ORDER BY wins DESC LIMIT ?
        """, (since, since, limit)).fetchall()


def get_history(user_id: int, limit: int = 5) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT b.ts, b.p1_id, b.p2_id, b.outcome, a.username AS u1, c.username AS u2
            FROM battles b
            LEFT JOIN players a ON a.user_id = b.p1_id
            LEFT JOIN players c ON c.user_id = b.p2_id
            WHERE b.p1_id = ? OR b.p2_id = ?
            ORDER BY b.id DESC LIMIT ?
        """, (user_id, user_id, limit)).fetchall()


def pair_battles_recent(a_id: int, b_id: int, seconds: int) -> int:
    with get_conn() as conn:
        return conn.execute("""
            SELECT COUNT(*) FROM battles
            WHERE ((p1_id = ? AND p2_id = ?) OR (p1_id = ? AND p2_id = ?))
              AND ts > strftime('%s','now') - ?
        """, (a_id, b_id, b_id, a_id, seconds)).fetchone()[0]


def is_fresh_player(user_id: int, seconds: int = 30) -> bool:
    """Игрок только что появился (для реферальной награды) и ещё не привязан к пригласившему."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM players WHERE user_id = ? AND invited_by IS NULL AND total_battles = 0 "
            "AND created_at >= strftime('%s','now') - ?", (user_id, seconds)).fetchone()
        return row is not None


def add_referral(new_id: int, inviter_id: int) -> bool:
    if new_id == inviter_id:
        return False
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM players WHERE user_id = ?", (inviter_id,)).fetchone():
            return False
        cur = conn.execute("UPDATE players SET invited_by = ? WHERE user_id = ? AND invited_by IS NULL",
                           (inviter_id, new_id))
        if cur.rowcount != 1:
            return False
        conn.execute("UPDATE players SET invites = COALESCE(invites, 0) + 1 WHERE user_id = ?", (inviter_id,))
        conn.commit()
        return True


def toggle_hidden(user_id: int) -> bool:
    with get_conn() as conn:
        conn.execute("UPDATE players SET hidden = 1 - COALESCE(hidden, 0) WHERE user_id = ?", (user_id,))
        conn.commit()
        return bool(conn.execute("SELECT hidden FROM players WHERE user_id = ?", (user_id,)).fetchone()[0])


def set_banned(user_id: int, banned: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE players SET banned = ? WHERE user_id = ?", (int(banned), user_id))
        conn.commit()


def is_banned(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT banned FROM players WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row[0])
