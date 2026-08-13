"""One-time loader: creates and populates the Northwind schema in Postgres.

Usage:
    python scripts/load_northwind.py

Reads DATABASE_URL from the environment (or a local .env file) and runs
data/northwind.sql against it. Safe to re-run: the dump starts with
DROP TABLE IF EXISTS for every table.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUMP_PATH = os.path.join(REPO_ROOT, "data", "northwind.sql")


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL is not set. Add it to your .env file first (see .env.example).")

    with open(DUMP_PATH, "r", encoding="utf-8") as f:
        sql = f.read()

    print(f"Loading Northwind dataset from {DUMP_PATH} ...")
    with psycopg2.connect(database_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
    print("Done. 14 tables created and populated (customers, orders, order_details, products, etc).")


if __name__ == "__main__":
    main()
