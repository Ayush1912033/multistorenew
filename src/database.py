from __future__ import annotations
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def initialize(self):
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS stores (
                store_code TEXT PRIMARY KEY, bo_ip TEXT NOT NULL, counter_count INTEGER NOT NULL,
                target_path TEXT NOT NULL, update_path TEXT NOT NULL, transfer_mode TEXT NOT NULL,
                job_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
                next_retry TEXT, last_error TEXT DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (
                store_code TEXT NOT NULL, number INTEGER NOT NULL, ip TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending', message TEXT DEFAULT '', backup TEXT DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(store_code, number),
                FOREIGN KEY(store_code) REFERENCES stores(store_code) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, store_code TEXT, event_time TEXT NOT NULL,
                level TEXT NOT NULL, message TEXT NOT NULL
            );
            ''')
            # A Processing job was already attempted. Do not auto-run it again
            # after an application/PC shutdown when retries are disabled.
            c.execute("UPDATE stores SET status='Failed', last_error='Application closed during the attempt; automatic retry disabled.', next_retry=NULL, updated_at=? WHERE status='Processing'", (now_iso(),))

    def add_event(self, store_code, level, message):
        with self.connect() as c:
            c.execute('INSERT INTO events(store_code,event_time,level,message) VALUES(?,?,?,?)',
                      (store_code, now_iso(), level, str(message)))

    def replace_job(self, rows, target_path, update_path, transfer_mode, allow_auto_fill=True):
        now = now_iso()
        with self.connect() as c:
            c.execute('DELETE FROM stores')
            for row in rows:
                code, ip, count = str(row['StoreCode']).strip(), str(row['BO_IP']).strip(), int(row['CounterCount'])
                import uuid
                job_id = uuid.uuid4().hex[:16]
                c.execute('''INSERT INTO stores(store_code,bo_ip,counter_count,target_path,update_path,transfer_mode,job_id,status,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,?,'Pending',?,?)''',
                          (code, ip, count, target_path, update_path, transfer_mode, job_id, now, now))
                prefix = '.'.join(ip.split('.')[:3])
                custom_ips = [str(value).strip() for value in row.get('CounterIPs', []) if str(value).strip()]
                if len(custom_ips) > count:
                    raise ValueError(
                        f"Store {code}: {len(custom_ips)} custom CounterIPs exceed CounterCount ({count})."
                    )
                if len(custom_ips) < count and custom_ips and not allow_auto_fill:
                    raise ValueError(
                        f"Store {code}: only {len(custom_ips)} custom IPs were supplied for {count} counters."
                    )

                # Custom IPs are assigned first in the order listed. If confirmed by the GUI,
                # fill any remaining slots from .112 upward, skipping IPs already in the custom list.
                final_ips = list(custom_ips)
                used_ips = set(final_ips)
                next_octet = 112
                while len(final_ips) < count:
                    if next_octet > 254:
                        raise ValueError(
                            f"Store {code}: cannot generate {count} unique counter IPs from BO subnet {prefix}."
                        )
                    candidate = f'{prefix}.{next_octet}'
                    next_octet += 1
                    if candidate in used_ips:
                        continue
                    final_ips.append(candidate)
                    used_ips.add(candidate)

                for n, counter_ip in enumerate(final_ips, start=1):
                    c.execute(
                        "INSERT INTO counters(store_code,number,ip,status,updated_at) VALUES(?,?,?,'Pending',?)",
                        (code, n, counter_ip, now)
                    )

    def stores(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM stores ORDER BY store_code')]

    def counters(self, store_code, unfinished_only=False):
        query = 'SELECT * FROM counters WHERE store_code=?'
        if unfinished_only:
            query += " AND status != 'SUCCESS'"
        query += ' ORDER BY number'
        with self.connect() as c:
            return [dict(r) for r in c.execute(query, (store_code,))]

    def update_store(self, store_code, status, error='', next_retry=None):
        with self.connect() as c:
            c.execute('UPDATE stores SET status=?,last_error=?,next_retry=?,updated_at=? WHERE store_code=?',
                      (status, error, next_retry, now_iso(), store_code))

    def update_counter(self, store_code, number, status, message='', backup=''):
        with self.connect() as c:
            c.execute('UPDATE counters SET status=?,message=?,backup=?,attempts=attempts+1,updated_at=? WHERE store_code=? AND number=?',
                      (status, message, backup, now_iso(), store_code, number))

    def retry_failed(self):
        """Requeue failed stores and their unsuccessful counters on user request."""
        now = now_iso()
        with self.connect() as c:
            failed_stores = c.execute(
                "SELECT store_code FROM stores WHERE status IN ('Failed','Incomplete','Link Down')"
            ).fetchall()
            codes = [row['store_code'] for row in failed_stores]
            for code in codes:
                c.execute(
                    "UPDATE counters SET status='Pending',message='',backup='',updated_at=? "
                    "WHERE store_code=? AND status!='SUCCESS'",
                    (now, code),
                )
                c.execute(
                    "UPDATE stores SET status='Pending',last_error='',next_retry=NULL,updated_at=? "
                    "WHERE store_code=?",
                    (now, code),
                )
                c.execute(
                    "INSERT INTO events(store_code,event_time,level,message) VALUES(?,?,?,?)",
                    (code, now, 'INFO', 'Manual retry requested; unsuccessful counters requeued.'),
                )
            return len(codes)

    def reset(self):
        with self.connect() as c:
            c.execute('DELETE FROM events')
            c.execute('DELETE FROM stores')

    def summary(self):
        with self.connect() as c:
            rows = c.execute('SELECT status,COUNT(*) n FROM stores GROUP BY status').fetchall()
            return {r['status']: r['n'] for r in rows}
