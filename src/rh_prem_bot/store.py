from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import FEE_SCHEDULE_VERSION, Tier, Trade


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            market_id INTEGER NOT NULL, id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            usd_micros INTEGER NOT NULL, maker_account INTEGER NOT NULL,
            taker_account INTEGER NOT NULL, maker_tier TEXT NOT NULL,
            taker_tier TEXT NOT NULL, is_maker_ask INTEGER NOT NULL,
            maker_fee INTEGER, taker_fee INTEGER, schedule TEXT NOT NULL,
            PRIMARY KEY(market_id,id)
        );
        CREATE INDEX IF NOT EXISTS trades_time ON trades(market_id,timestamp_ms);
        CREATE INDEX IF NOT EXISTS trades_retention ON trades(timestamp_ms);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, market_id INTEGER NOT NULL,
            created_ms INTEGER NOT NULL, payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS alerts_market ON alerts(market_id,created_ms);
        CREATE TABLE IF NOT EXISTS deliveries (
            alert_id INTEGER NOT NULL REFERENCES alerts(id), chat_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_ms INTEGER NOT NULL DEFAULT 0, error TEXT,
            PRIMARY KEY(alert_id,chat_id)
        );
        """)

    def add_trades(self, trades: list[Trade]) -> int:
        before = self.db.total_changes
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        t.market_id,
                        t.id,
                        t.timestamp_ms,
                        t.usd_micros,
                        t.maker_account,
                        t.taker_account,
                        t.maker_tier,
                        t.taker_tier,
                        int(t.is_maker_ask),
                        t.maker_fee,
                        t.taker_fee,
                        FEE_SCHEDULE_VERSION,
                    )
                    for t in trades
                ],
            )
        return self.db.total_changes - before

    def trades(self, market_id: int, start_ms: int, end_ms: int) -> list[Trade]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE market_id=? AND timestamp_ms>? AND timestamp_ms<=? "
            "ORDER BY timestamp_ms,id",
            (market_id, start_ms, end_ms),
        )
        return [
            Trade(
                id=r["id"],
                market_id=r["market_id"],
                timestamp_ms=r["timestamp_ms"],
                usd_micros=r["usd_micros"],
                maker_account=r["maker_account"],
                taker_account=r["taker_account"],
                maker_tier=Tier(r["maker_tier"]),
                taker_tier=Tier(r["taker_tier"]),
                is_maker_ask=bool(r["is_maker_ask"]),
                maker_fee=r["maker_fee"],
                taker_fee=r["taker_fee"],
            )
            for r in rows
        ]

    def premium_sum(self, market_id: int, start_ms: int, end_ms: int) -> int:
        return self.db.execute(
            "SELECT COALESCE(SUM(usd_micros),0) FROM trades WHERE market_id=? "
            "AND timestamp_ms>? AND timestamp_ms<=? "
            "AND (maker_tier='premium' OR taker_tier='premium')",
            (market_id, start_ms, end_ms),
        ).fetchone()[0]

    def ranking(self, start_ms: int, end_ms: int) -> dict[int, tuple[float, float]]:
        rows = self.db.execute(
            "SELECT market_id,SUM(usd_micros) total, SUM(CASE WHEN maker_tier='premium' "
            "OR taker_tier='premium' THEN usd_micros ELSE 0 END) premium "
            "FROM trades WHERE timestamp_ms>? AND timestamp_ms<=? GROUP BY market_id",
            (start_ms, end_ms),
        )
        return {r["market_id"]: (r["premium"] / 1e6, r["total"] / 1e6) for r in rows}

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def create_alert(self, snapshot, chat_ids: tuple[int, ...], cooldown_ms: int) -> int | None:
        # Alert, cooldown, and durable delivery queue are one transaction.
        with self.db:
            last = self.db.execute(
                "SELECT MAX(created_ms) FROM alerts WHERE market_id=?",
                (snapshot.market_id,),
            ).fetchone()[0]
            if last is not None and snapshot.end_ms - last < cooldown_ms:
                return None
            cursor = self.db.execute(
                "INSERT INTO alerts(market_id,created_ms,payload) VALUES (?,?,?)",
                (snapshot.market_id, snapshot.end_ms, json.dumps(asdict(snapshot))),
            )
            alert_id = cursor.lastrowid
            self.db.executemany(
                "INSERT INTO deliveries(alert_id,chat_id) VALUES (?,?)",
                [(alert_id, c) for c in chat_ids],
            )
            return alert_id

    def alert(self, alert_id: int) -> dict | None:
        row = self.db.execute("SELECT payload FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self, now_ms: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT d.*,a.created_ms,a.payload FROM deliveries d JOIN alerts a ON a.id=d.alert_id "
            "WHERE d.status='pending' AND d.next_ms<=? ORDER BY a.id LIMIT 30",
            (now_ms,),
        ).fetchall()

    def delivery(self, alert_id: int, chat_id: int, status: str, *, next_ms=0, error=None):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status=?,attempts=attempts+1,next_ms=?,error=? "
                "WHERE alert_id=? AND chat_id=?",
                (status, next_ms, error, alert_id, chat_id),
            )

    def prune(self, before_ms: int):
        with self.db:
            self.db.execute("DELETE FROM trades WHERE timestamp_ms<?", (before_ms,))
            self.db.execute(
                "DELETE FROM deliveries WHERE alert_id IN "
                "(SELECT id FROM alerts WHERE created_ms<?)",
                (before_ms,),
            )
            self.db.execute("DELETE FROM alerts WHERE created_ms<?", (before_ms,))

    def close(self):
        self.db.close()
