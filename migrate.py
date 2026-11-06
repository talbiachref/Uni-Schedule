import sqlite3

import database

database.init()

conn = sqlite3.connect("bot.db")
rows = conn.execute("SELECT chat_id, group_name FROM subscribers").fetchall()
conn.close()

for chat_id, group in rows:
    database.add_subscriber(chat_id)
    if group:
        database.set_group(chat_id, group)

print(f"Migrated {len(rows)} user(s) to PostgreSQL")