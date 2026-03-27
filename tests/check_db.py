"""Quick peek at what landed in the database."""

import sqlite3
from pathlib import Path

db_path = Path("data/morning_brief.db")
if not db_path.exists():
    print(f"Database not found at {db_path}")
    print("Run the pipeline first: PYTHONPATH=. python3 -m src.main")
    raise SystemExit(1)

db = sqlite3.connect(db_path)
count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
print(f"Total articles: {count}")

cats = db.execute(
    "SELECT category, COUNT(*) FROM articles GROUP BY category ORDER BY COUNT(*) DESC"
).fetchall()
for cat, n in cats:
    print(f"  {cat}: {n}")

db.close()
