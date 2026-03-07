#!/usr/bin/env python3
"""
高频抓取 MoneyWiz 同步队列样本（用于逆向 ZSYNCCOMMAND）

用法示例：
python3 scripts/mw_sync_harvest.py \
  --db "/Users/xxx/.../ipadMoneyWiz.sqlite" \
  --duration-seconds 60 \
  --interval-seconds 0.2 \
  --out-dir "/Users/xxx/Downloads/mw_sync_samples"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def now_iso() -> str:
    return datetime.now().isoformat(sep=" ")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<BLOB:{len(value)}>"
    return value


def fetch_sync_commands(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    rows = cur.execute(
        """
        SELECT
            Z_PK,
            Z_ENT,
            Z_OPT,
            ZCOMMANDID,
            ZISPENDING,
            ZOBJECTTYPE,
            ZOBJECTXMLDATATYPE,
            ZORDER,
            ZREVISION,
            ZUSER,
            ZOBJECTGID,
            ZOBJECTXMLDATA
        FROM ZSYNCCOMMAND
        ORDER BY Z_PK DESC
        """
    ).fetchall()
    return [{k: to_jsonable(row[k]) for k in row.keys()} for row in rows]


def fetch_linked_object(cur: sqlite3.Cursor, gid: str) -> dict[str, Any] | None:
    row = cur.execute(
        """
        SELECT *
        FROM ZSYNCOBJECT
        WHERE ZGID = ?
        LIMIT 1
        """,
        [gid],
    ).fetchone()
    if not row:
        return None
    return {k: to_jsonable(row[k]) for k in row.keys()}


def fetch_linked_assignments(
    cur: sqlite3.Cursor, transaction_pk: int
) -> list[dict[str, Any]]:
    rows = cur.execute(
        """
        SELECT *
        FROM ZCATEGORYASSIGMENT
        WHERE ZTRANSACTION = ?
        ORDER BY Z_PK
        """,
        [transaction_pk],
    ).fetchall()
    return [{k: to_jsonable(row[k]) for k in row.keys()} for row in rows]


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def run_harvest(
    db_path: Path, duration_seconds: int, interval_seconds: float, out_dir: Path
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    captures = 0
    last_signature = None

    while time.monotonic() - start < duration_seconds:
        con = sqlite3.connect(str(db_path), timeout=1.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        try:
            table_exists = cur.execute(
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name='ZSYNCCOMMAND'"
            ).fetchone()["c"]
            if not table_exists:
                time.sleep(interval_seconds)
                continue

            count_row = cur.execute("SELECT COUNT(*) AS c FROM ZSYNCCOMMAND").fetchone()
            sync_count = int(count_row["c"])
            if sync_count <= 0:
                time.sleep(interval_seconds)
                continue

            commands = fetch_sync_commands(cur)
            signature = "|".join(
                f"{c.get('Z_PK')}:{c.get('ZCOMMANDID')}:{c.get('ZOBJECTTYPE')}:{c.get('ZOBJECTGID')}:{c.get('ZREVISION')}"
                for c in commands
            )
            if signature == last_signature:
                time.sleep(interval_seconds)
                continue
            last_signature = signature

            linked_objects: list[dict[str, Any]] = []
            linked_assignments: dict[str, list[dict[str, Any]]] = {}

            for cmd in commands:
                object_gid = cmd.get("ZOBJECTGID")
                if not isinstance(object_gid, str):
                    continue
                obj = fetch_linked_object(cur, object_gid)
                if obj:
                    linked_objects.append(obj)
                    obj_pk = obj.get("Z_PK")
                    obj_ent = obj.get("Z_ENT")
                    if isinstance(obj_pk, int) and obj_ent in (37, 45, 46, 47):
                        linked_assignments[str(obj_pk)] = fetch_linked_assignments(
                            cur, obj_pk
                        )

            payload = {
                "captured_at": now_iso(),
                "db_path": str(db_path),
                "sync_count": sync_count,
                "commands": commands,
                "linked_objects": linked_objects,
                "linked_assignments": linked_assignments,
            }

            out_file = out_dir / f"sync-capture-{now_ts()}.json"
            save_json(out_file, payload)
            captures += 1
            print(f"captured: {out_file.name} (sync_count={sync_count})")
        finally:
            con.close()

        time.sleep(interval_seconds)

    print(f"done, captures={captures}, out_dir={out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="高频抓取 ZSYNCCOMMAND 样本")
    parser.add_argument("--db", required=True, help="sqlite 文件路径")
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument("--out-dir", required=True, help="抓取结果输出目录")
    args = parser.parse_args()

    if args.duration_seconds <= 0:
        raise ValueError("--duration-seconds must be > 0")
    if args.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be > 0")

    run_harvest(
        db_path=Path(args.db),
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
        out_dir=Path(args.out_dir),
    )


if __name__ == "__main__":
    main()
