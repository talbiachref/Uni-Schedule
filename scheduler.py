import logging

import database
from uni import fetch_timetables

log = logging.getLogger("scheduler")


async def check_for_changes(context):
    by_group = database.subscribers_by_group()
    if not by_group:
        return

    try:
        results = await fetch_timetables(list(by_group))
    except Exception:
        log.exception("Auto check failed")
        return

    for group, res in results.items():
        if isinstance(res, Exception):
            log.warning("Group %s failed: %s", group, res)
            continue

        path, new_hash, title = res
        old_hash = database.get_hash(group)
        if new_hash == old_hash:
            continue

        database.set_hash(group, new_hash)
        if old_hash is None:  
            continue

        log.info("Change detected for %s", group)
        for chat_id in by_group[group]:
            try:
                with open(path, "rb") as f:
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=f"🔔 Your timetable has changed!\n{title or group}",
                    )
            except Exception:
                log.exception("Could not send to %s", chat_id)


def setup(app, minutes: int):
    app.job_queue.run_repeating(check_for_changes, interval=minutes * 60, first=60)