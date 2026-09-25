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
        conn.execute("DELETE FROM challenges WHERE created_at < strftime('%s','now') - 7*86400")
        conn.commit()
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
            UPDATE players SET wins = wins + 1, total_battles = total_battles + 1
            WHERE user_id = ?
        """, (winner_id,))
        conn.execute("""
            UPDATE players SET losses = losses + 1, total_battles = total_battles + 1
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
            "SELECT * FROM players ORDER BY wins DESC LIMIT ?", (limit,)
        ).fetchall()


def record_draw(a_id: int, b_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE players SET draws = draws + 1, total_battles = total_battles + 1 "
            "WHERE user_id IN (?, ?)", (a_id, b_id))
        conn.commit()


def add_challenge(cid: str, challenger_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO challenges (id, challenger_id, challenger_name) VALUES (?, ?, ?)",
                     (cid, challenger_id, name))
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
            "SELECT COUNT(*) + 1 FROM players WHERE wins > (SELECT wins FROM players WHERE user_id = ?)",
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
