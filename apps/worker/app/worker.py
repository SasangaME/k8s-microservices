import json
import os
import time

import psycopg2
import redis

PG = dict(
    host=os.environ["POSTGRES_HOST"],
    port=int(os.environ.get("POSTGRES_PORT", 5432)),
    dbname=os.environ.get("POSTGRES_DB", "appdb"),
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)
r = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True,
)
QUEUE = os.environ.get("QUEUE_NAME", "jobs")


def ensure_schema():
    with psycopg2.connect(**PG) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS items (id serial PRIMARY KEY, name text)"
        )


def handle(job: dict):
    with psycopg2.connect(**PG) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO items (name) VALUES (%s)", (job["name"],))
    print(f"processed job {job}", flush=True)


def main():
    while True:
        try:
            ensure_schema()
            break
        except Exception as e:
            print(f"db not ready: {e}", flush=True)
            time.sleep(2)

    print(f"worker listening on queue={QUEUE}", flush=True)
    while True:
        _, raw = r.brpop(QUEUE)
        try:
            handle(json.loads(raw))
        except Exception as e:
            print(f"job failed: {e}", flush=True)


if __name__ == "__main__":
    main()
