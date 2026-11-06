import database

users = database._exec(
    "SELECT chat_id, COALESCE(group_name, '(no group yet)') FROM subscribers ORDER BY group_name"
)
print(f"\n{len(users)} user(s):\n")
for chat_id, group in users:
    print(f"  {chat_id}  ->  {group}")

print("\nUsers per group:")
for group, count in database._exec(
    "SELECT group_name, COUNT(*) FROM subscribers "
    "WHERE group_name IS NOT NULL GROUP BY group_name ORDER BY COUNT(*) DESC"
):
    print(f"  {group}: {count}")