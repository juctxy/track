import re
import csv

pattern = re.compile(r"\*\*([^*]+)\*\*\s*\((\d+)\)")

users = {}  # user_id -> username (dicts keep insertion order)

with open("D:\pulls.txt", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Read from the bottom of the file up to the top
for line in reversed(lines):
    for username, user_id in pattern.findall(line):
        users.setdefault(user_id, username.strip())

with open("user_id.csv", "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["username", "user_id"])
    for user_id, username in users.items():
        writer.writerow([username, user_id])

print(f"Saved {len(users)} unique user IDs to output.csv")