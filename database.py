import os
import time

import psycopg
from dotenv import load_dotenv

load_dotenv()


def _connect():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "timetable_bot"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
    )


def _exec(sql, params=()):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []


def init():
    _exec(
        "CREATE TABLE IF NOT EXISTS subscribers ("
        "chat_id BIGINT PRIMARY KEY, group_name TEXT)"
    )
    _exec("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS username TEXT")
    _exec("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS full_name TEXT")
    _exec("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    _exec("CREATE TABLE IF NOT EXISTS known_groups (name TEXT PRIMARY KEY)")


def add_subscriber(chat_id: int, username: str | None = None, full_name: str | None = None):
    _exec(
        "INSERT INTO subscribers (chat_id, username, full_name) VALUES (%s, %s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET "
        "username = COALESCE(EXCLUDED.username, subscribers.username), "
        "full_name = COALESCE(EXCLUDED.full_name, subscribers.full_name)",
        (chat_id, username, full_name),
    )


def remove_subscriber(chat_id: int):
    _exec("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))


def save_user_info(chat_id: int, username: str | None, full_name: str | None):
    _exec(
        "INSERT INTO subscribers (chat_id, username, full_name) VALUES (%s, %s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET "
        "username = EXCLUDED.username, full_name = EXCLUDED.full_name",
        (chat_id, username, full_name),
    )


def users_missing_info() -> list[int]:
    return [r[0] for r in _exec("SELECT chat_id FROM subscribers WHERE full_name IS NULL")]


def set_group(chat_id: int, group: str | None):
    _exec(
        "INSERT INTO subscribers (chat_id, group_name) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET group_name = EXCLUDED.group_name",
        (chat_id, group),
    )


def get_group(chat_id: int) -> str | None:
    rows = _exec("SELECT group_name FROM subscribers WHERE chat_id = %s", (chat_id,))
    return rows[0][0] if rows else None


def subscribers_by_group() -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for chat_id, group in _exec(
        "SELECT chat_id, group_name FROM subscribers WHERE group_name IS NOT NULL"
    ):
        result.setdefault(group, []).append(chat_id)
    return result


def get_hash(group: str) -> str | None:
    rows = _exec("SELECT value FROM state WHERE key = %s", (f"hash:{group}",))
    return rows[0][0] if rows else None


def set_hash(group: str, value: str):
    _exec(
        "INSERT INTO state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (f"hash:{group}", value),
    )


def save_groups(names: list[str]):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM known_groups")
            cur.executemany(
                "INSERT INTO known_groups (name) VALUES (%s) ON CONFLICT DO NOTHING",
                [(n,) for n in names],
            )
            cur.execute(
                "INSERT INTO state (key, value) VALUES ('groups_updated', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(time.time()),),
            )


def get_groups() -> list[str]:
    return [r[0] for r in _exec("SELECT name FROM known_groups ORDER BY name")]


def groups_age() -> float:
    rows = _exec("SELECT value FROM state WHERE key = 'groups_updated'")
    return time.time() - float(rows[0][0]) if rows else float("inf")