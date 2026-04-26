import os
import json
import logging
from pathlib import Path

import psycopg2
import redis
from fastapi import FastAPI, HTTPException

LOG_DIR = Path("/var/log/api")
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "api.log"), logging.StreamHandler()],
)
log = logging.getLogger("api")

PG = dict(
    host=os.environ["POSTGRES_HOST"],
    port=int(os.environ.get("POSTGRES_PORT", 5432)),
    dbname=os.environ["POSTGRES_DB"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)
r = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True,
)
QUEUE = os.environ.get("QUEUE_NAME", "jobs")

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/items")
def list_items():
    with psycopg2.connect(**PG) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS items (id serial PRIMARY KEY, name text)"
        )
        cur.execute("SELECT id, name FROM items ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
    return [{"id": i, "name": n} for i, n in rows]


@app.post("/api/jobs")
def enqueue_job(payload: dict):
    if "name" not in payload:
        raise HTTPException(400, "name required")
    r.lpush(QUEUE, json.dumps(payload))
    log.info("enqueued job: %s", payload)
    return {"queued": True}
