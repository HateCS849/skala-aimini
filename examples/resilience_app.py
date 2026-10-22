"""An actual HTTP application with SQLite connections and an HTTP auth dependency.

This is a reproducible fixture, not Hangari, Spring, or HikariCP. Run on 8731.
"""
import asyncio
import logging
import os
import sqlite3
import json
import time
import tracemalloc
from uuid import uuid4
from contextlib import asynccontextmanager
from pathlib import Path
from logging.handlers import RotatingFileHandler

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from examples.process_metrics import process_metrics
from examples.redis_wire import command, gate

ROOT = Path(__file__).resolve().parent.parent


class DemoConfig(BaseModel):
    external_delay_seconds: float = Field(default=0, ge=0, le=30)
    external_status: int = Field(default=200, ge=200, le=599)
    pool_size: int = Field(default=10, ge=1, le=50)
    pool_wait_seconds: float = Field(default=.3, ge=.05, le=10)
    provider_timeout_seconds: float = Field(default=3, ge=.1, le=10)
    external_behavior: Literal["normal", "malformed", "disconnect"] = "normal"
    db_delay_ms: int = Field(default=0, ge=0, le=5000)
    db_lock_seconds: float = Field(default=0, ge=0, le=30)
    cache_unavailable: bool = False


class Pool:
    def __init__(self, path, size):
        self.queue = asyncio.Queue()
        self.active = self.pending = self.timeouts = 0
        self.size = size
        for _ in range(size):
            conn = sqlite3.connect(path, timeout=.1)
            conn.execute("PRAGMA journal_mode=WAL")
            self.queue.put_nowait(conn)

    @asynccontextmanager
    async def connection(self, timeout):
        self.pending += 1
        try:
            try:
                conn = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                self.timeouts += 1
                raise HTTPException(503, "DB connection acquisition timeout")
        finally:
            self.pending -= 1
        self.active += 1
        try:
            yield conn
        finally:
            conn.rollback()
            self.active -= 1
            self.queue.put_nowait(conn)

    def close(self):
        while not self.queue.empty():
            self.queue.get_nowait().close()


class Service:
    def __init__(self, directory, name):
        self.path = directory / (name + ".sqlite3")
        conn = sqlite3.connect(self.path)
        conn.executescript("CREATE TABLE IF NOT EXISTS foods(id INTEGER PRIMARY KEY, name TEXT);"
                           "INSERT OR IGNORE INTO foods VALUES(1, 'kimchi'),(2, 'bibimbap'),(3, 'tteokbokki');"
                           "CREATE TABLE IF NOT EXISTS error_logs(id INTEGER PRIMARY KEY, message TEXT);"
                           "CREATE TABLE IF NOT EXISTS inventory(scope TEXT PRIMARY KEY, stock INTEGER NOT NULL);"
                           "CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, scope TEXT, request_key TEXT, quantity INTEGER);"
                           "CREATE INDEX IF NOT EXISTS orders_scope ON orders(scope);")
        conn.commit()
        conn.close()
        self.config = DemoConfig()
        self.pool = Pool(self.path, self.config.pool_size)
        self.external_inflight = 0
        self.provider_attempts = self.logical_auth_requests = self.cache_errors = self.cache_hits = self.db_reads = self.db_lock_errors = 0
        self.lock_task = None
        self.fallback_lock = asyncio.Lock()
        self.fallback_value, self.fallback_until = None, 0
        self.leaked = []
        self.logs = asyncio.Queue(maxsize=32)
        self.logger = logging.getLogger("burstguard.demo." + name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(directory / (name + "-fallback.log"), maxBytes=1024 * 1024, backupCount=1)
        self.logger.addHandler(handler)
        self.task = asyncio.create_task(self.log_worker())

    async def log_worker(self):
        while True:
            message = await self.logs.get()
            try:
                async with self.pool.connection(self.config.pool_wait_seconds) as conn:
                    conn.execute("INSERT INTO error_logs(message) VALUES(?)", (message,))
                    conn.commit()
            except Exception:
                self.logger.info(message)
            finally:
                self.logs.task_done()

    async def stop(self):
        await self.set_db_lock(0)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.pool.close()
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)

    async def set_db_lock(self, seconds):
        if self.lock_task and not self.lock_task.done():
            self.lock_task.cancel()
            await asyncio.gather(self.lock_task, return_exceptions=True)
        self.lock_task = None
        if seconds:
            ready = asyncio.Event()
            async def hold():
                conn = sqlite3.connect(self.path, timeout=.1)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    ready.set()
                    await asyncio.sleep(seconds)
                finally:
                    conn.rollback()
                    conn.close()
            self.lock_task = asyncio.create_task(hold())
            waiter = asyncio.create_task(ready.wait())
            done, _ = await asyncio.wait([self.lock_task, waiter], return_when=asyncio.FIRST_COMPLETED)
            if self.lock_task in done:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await self.lock_task


@asynccontextmanager
async def lifespan(app):
    tracemalloc.start(1)
    directory = Path(os.getenv("BURSTGUARD_DEMO_DATA_DIR", ROOT / "data" / "demo"))
    directory.mkdir(parents=True, exist_ok=True)
    app.state.services = {name: Service(directory, name) for name in ("vulnerable", "isolated")}
    app.state.boot_id, app.state.started = uuid4().hex, time.monotonic()
    redis_port = int(os.getenv("BURSTGUARD_REDIS_PORT", "16379"))
    app.state.cache_gate = await asyncio.start_server(lambda r, w: gate(r, w, app.state.services, redis_port), "127.0.0.1", 8732)
    app.state.client = httpx.AsyncClient(trust_env=False, timeout=35, limits=httpx.Limits(max_connections=300, max_keepalive_connections=200))
    yield
    app.state.cache_gate.close()
    await app.state.cache_gate.wait_closed()
    await app.state.client.aclose()
    for service in app.state.services.values():
        await service.stop()
    tracemalloc.stop()


app = FastAPI(title="BurstGuard · 실제 HTTP 장애 재현용 예제", lifespan=lifespan)


def service(request, variant):
    if variant not in request.app.state.services:
        raise HTTPException(404, "Unknown variant")
    return request.app.state.services[variant]


@app.get("/health")
async def health(request: Request):
    return {"ok": True, "fixture": "burstguard-v2", "boot_id": request.app.state.boot_id, "scope": "Synthetic SQLite fixture, not actual Hangari"}


@app.get("/{variant}/__burstguard/control")
async def config(variant: str, request: Request):
    return {"adapter": "burstguard-demo-v1", **service(request, variant).config.model_dump()}


@app.post("/{variant}/__burstguard/control")
async def configure(variant: str, request: Request, body: dict):
    state = service(request, variant)
    valid_keys = DemoConfig.model_fields
    if any(k not in valid_keys and k != "adapter" for k in body):
        raise HTTPException(422, "Unknown config key")
    try:
        updated = DemoConfig(**(state.config.model_dump() | {k: v for k, v in body.items() if k in valid_keys}))
    except ValueError:
        raise HTTPException(422, "Invalid demo config")
    if updated.pool_size != state.pool.size:
        if state.pool.active or state.pool.pending or state.external_inflight:
            raise HTTPException(409, "Wait for in-flight requests before resizing pool")
        state.pool.close()
        state.pool = Pool(state.path, updated.pool_size)
    state.config = updated
    if updated.db_lock_seconds != 0 or "db_lock_seconds" in body:
        await state.set_db_lock(updated.db_lock_seconds)
    return {"ok": True, **state.config.model_dump()}


@app.get("/{variant}/__burstguard/metrics")
async def metrics(variant: str, request: Request):
    state = service(request, variant)
    return {"pool_active": state.pool.active, "pool_max": state.pool.size, "pool_pending": state.pool.pending,
            "pool_timeouts": state.pool.timeouts, "external_inflight": state.external_inflight,
            "provider_attempts": state.provider_attempts, "logical_auth_requests": state.logical_auth_requests,
            "cache_errors": state.cache_errors, "cache_hits": state.cache_hits, "db_reads": state.db_reads, "db_lock_errors": state.db_lock_errors,
            "uptime_seconds": round(time.monotonic() - request.app.state.started, 2), **process_metrics()}


@app.post("/{variant}/provider/apple")
async def provider(variant: str, request: Request):
    state = service(request, variant)
    # Read the fault at request start, so clearing it does not magically release old requests.
    delay, status = state.config.external_delay_seconds, state.config.external_status
    state.external_inflight += 1
    try:
        await asyncio.sleep(delay)
        if state.config.external_behavior == "malformed":
            return Response(content=b'{"sub":', media_type="application/json")
        if state.config.external_behavior == "disconnect":
            async def broken():
                yield b'{"sub":'
                raise ConnectionResetError("Synthetic upstream disconnect")
            return StreamingResponse(broken(), media_type="application/json")
        if status != 200:
            raise HTTPException(status, "Synthetic external provider error", headers={"Retry-After": "1"} if status == 429 else None)
        return {"sub": "synthetic-user"}
    finally:
        state.external_inflight -= 1


async def external_call(request, state, variant, timeout):
    # An actual HTTP round trip, not a mocked sleep inside the login handler.
    url = str(request.base_url).rstrip("/") + f"/{variant}/provider/apple"
    attempts = 6 if variant == "vulnerable" else 2
    async with asyncio.timeout(timeout):
        for turn in range(attempts):
            state.provider_attempts += 1
            try:
                response = await request.app.state.client.post(url, json={"code": "synthetic"}, timeout=timeout)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("sub"), str):
                    raise ValueError("Invalid provider response")
                return body
            except (httpx.HTTPError, ValueError):
                if turn + 1 == attempts:
                    raise
                if variant != "vulnerable":
                    await asyncio.sleep(.2 * (turn + 1))


@app.post("/{variant}/login")
async def login(variant: str, request: Request):
    state = service(request, variant)
    state.logical_auth_requests += 1
    if variant == "vulnerable":
        # The transaction and an actual SQLite connection stay open while waiting for HTTP.
        async with state.pool.connection(state.config.pool_wait_seconds) as conn:
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM foods").fetchone()
            try:
                await external_call(request, state, variant, 35)
                return {"access_token": "synthetic-token"}
            except (httpx.HTTPError, ValueError, TimeoutError):
                # A second checkout for sync failure logging worsens exhaustion.
                async with state.pool.connection(state.config.pool_wait_seconds) as log_conn:
                    log_conn.execute("INSERT INTO error_logs(message) VALUES('provider failed')")
                    log_conn.commit()
                raise HTTPException(502, "External login failed")
    try:
        await external_call(request, state, variant, state.config.provider_timeout_seconds)
    except (httpx.HTTPError, ValueError, TimeoutError):
        try:
            state.logs.put_nowait("provider failed")
        except asyncio.QueueFull:
            state.logger.info("provider failed (queue full)")
        raise HTTPException(504, "External login timed out or failed")
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        conn.execute("SELECT count(*) FROM foods").fetchone()
    return {"access_token": "synthetic-token"}


@app.get("/{variant}/foods")
async def foods(variant: str, request: Request):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        items = conn.execute("SELECT id,name FROM foods").fetchall()
        state.db_reads += 1
        await asyncio.sleep(.005 + state.config.db_delay_ms / 1000)
    return {"items": [{"id": row[0], "name": row[1]} for row in items]}


@app.get("/{variant}/search")
async def search(variant: str, request: Request, q: str = "kimchi"):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        items = conn.execute("SELECT id,name FROM foods WHERE name LIKE ?", ("%" + q + "%",)).fetchall()
        state.db_reads += 1
        await asyncio.sleep(.005 + state.config.db_delay_ms / 1000)
    return {"items": [{"id": row[0], "name": row[1]} for row in items]}


class InventoryReset(BaseModel):
    scope: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    stock: int = Field(default=5, ge=1, le=1000)


class Order(BaseModel):
    scope: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    key: str = Field(min_length=1, max_length=150)
    quantity: int = Field(default=1, ge=1, le=100)


@app.post("/{variant}/orders/reset")
async def reset_inventory(variant: str, request: Request, body: InventoryReset):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        # Never reset an existing namespace: old runs and unrelated data remain intact.
        if conn.execute("SELECT 1 FROM inventory WHERE scope=?", (body.scope,)).fetchone():
            raise HTTPException(409, "Namespace already exists")
        conn.execute("INSERT INTO inventory VALUES(?,?)", (body.scope, body.stock))
        conn.commit()
    return {"prepared": True}


@app.get("/{variant}/orders/state")
async def order_state(variant: str, request: Request, scope: str):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        row = conn.execute("SELECT stock FROM inventory WHERE scope=?", (scope,)).fetchone()
        if not row:
            raise HTTPException(404, "Unknown namespace")
        count, keys = conn.execute("SELECT count(*),count(distinct request_key) FROM orders WHERE scope=?", (scope,)).fetchone()
    return {"stock": row[0], "orders": count, "unique_keys": keys}


@app.post("/{variant}/orders")
async def order(variant: str, request: Request, body: Order):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        try:
            if variant == "isolated":
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT id FROM orders WHERE scope=? AND request_key=?", (body.scope, body.key)).fetchone()
                if existing:
                    return {"id": existing[0], "reused": True}
            row = conn.execute("SELECT stock FROM inventory WHERE scope=?", (body.scope,)).fetchone()
            if not row:
                raise HTTPException(404, "Unknown namespace")
            if row[0] < body.quantity:
                raise HTTPException(409, "Sold out")
            if variant == "vulnerable":
                await asyncio.sleep(.03)  # Read/check and update are separate, so concurrent requests see stale stock.
            conn.execute("UPDATE inventory SET stock=stock-? WHERE scope=?", (body.quantity, body.scope))
            cursor = conn.execute("INSERT INTO orders(scope,request_key,quantity) VALUES(?,?,?)", (body.scope, body.key, body.quantity))
            conn.commit()
            return {"id": cursor.lastrowid, "reused": False}
        except sqlite3.OperationalError:
            state.db_lock_errors += 1
            raise HTTPException(503, "SQLite write lock contention")


@app.post("/{variant}/db/write")
async def write_probe(variant: str, request: Request):
    state = service(request, variant)
    async with state.pool.connection(state.config.pool_wait_seconds) as conn:
        try:
            conn.execute("INSERT INTO error_logs(message) VALUES('synthetic-write-probe')")
            conn.commit()
            return {"saved": True}
        except sqlite3.OperationalError:
            state.db_lock_errors += 1
            raise HTTPException(503, "SQLite write lock contention")


@app.get("/{variant}/cached-foods")
async def cached_foods(variant: str, request: Request):
    state = service(request, variant)
    key = f"burstguard:{variant}:foods"
    try:
        value = await command(8732, "GET", key)
        if value:
            state.cache_hits += 1
            return json.loads(value)
        value = await foods(variant, request)
        await command(8732, "SET", key, json.dumps(value), "EX", 5)
        return value
    except (OSError, ConnectionError, TimeoutError, ValueError, asyncio.IncompleteReadError):
        state.cache_errors += 1
        if variant == "vulnerable":
            raise HTTPException(503, "Cache connection failed")
        # Bound cache-outage DB fallback: collapse simultaneous misses and cache briefly in process.
        async with state.fallback_lock:
            if time.monotonic() >= state.fallback_until:
                state.fallback_value = await foods(variant, request)
                state.fallback_until = time.monotonic() + .3
            return state.fallback_value


@app.get("/{variant}/leaky")
async def leaky(variant: str, request: Request):
    state = service(request, variant)
    allocation = bytearray(256 * 1024)
    allocation[:] = b"x" * len(allocation)  # Touch pages so the RSS metric reflects actual resident allocation.
    if variant == "vulnerable" and len(state.leaked) < 384:  # Fixture leak capped at 96MiB.
        state.leaked.append(allocation)
    return {"ok": True}
