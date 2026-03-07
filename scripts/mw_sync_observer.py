#!/usr/bin/env python3
"""
MoneyWiz 同步观测脚本

用途：
1) 先做一次 baseline 快照
2) 在 MoneyWiz 里手动新增/修改一笔账单并触发同步
3) 再做 diff 对比，观察数据库变化（尤其 ZSYNCCOMMAND）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time
from typing import Any


TABLES = [
    "ZSYNCOBJECT",
    "ZCATEGORYASSIGMENT",
    "ZSYNCCOMMAND",
    "ZTRANSACTIONBUDGETLINK",
    "ZWITHDRAWREFUNDTRANSACTIONLINK",
]


@dataclass
class TableWindow:
    table: str
    pk: str
    columns: list[str]
    limit: int


WINDOWS = [
    TableWindow(
        table="ZSYNCOBJECT",
        pk="Z_PK",
        columns=[
            "Z_PK",
            "Z_ENT",
            "Z_OPT",
            "ZGID",
            "ZACCOUNT2",
            "ZPAYEE2",
            "ZAMOUNT1",
            "ZDATE1",
            "ZDESC2",
            "ZNOTES1",
            "ZOBJECTCREATIONDATE",
            "ZOBJECTMODIFICATIONDATE",
        ],
        limit=500,
    ),
    TableWindow(
        table="ZCATEGORYASSIGMENT",
        pk="Z_PK",
        columns=[
            "Z_PK",
            "Z_ENT",
            "Z_OPT",
            "ZCATEGORY",
            "ZTRANSACTION",
            "Z36_TRANSACTION",
            "ZAMOUNT",
        ],
        limit=500,
    ),
    TableWindow(
        table="ZSYNCCOMMAND",
        pk="Z_PK",
        columns=[
            "Z_PK",
            "Z_ENT",
            "Z_OPT",
            "ZCOMMANDID",
            "ZISPENDING",
            "ZOBJECTTYPE",
            "ZOBJECTXMLDATATYPE",
            "ZORDER",
            "ZREVISION",
            "ZUSER",
            "ZOBJECTGID",
            "ZOBJECTXMLDATA",
        ],
        limit=1000,
    ),
]


def now_iso() -> str:
    return datetime.now().isoformat(sep=" ")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return f"<BLOB:{len(value)}>"
    return value


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row[0])


def get_table_summary(cur: sqlite3.Cursor, table: str) -> dict[str, Any]:
    if not table_exists(cur, table):
        return {"exists": False}

    count = cur.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()[0]
    max_pk = None
    if table_exists(cur, table):
        try:
            max_pk = cur.execute(f"SELECT MAX(Z_PK) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            max_pk = None

    summary: dict[str, Any] = {
        "exists": True,
        "count": count,
        "max_pk": max_pk,
    }

    if table == "ZSYNCCOMMAND":
        pending = cur.execute(
            "SELECT COUNT(*) FROM ZSYNCCOMMAND WHERE ZISPENDING = 1"
        ).fetchone()[0]
        summary["pending_count"] = pending

    return summary


def get_window_rows(
    cur: sqlite3.Cursor, window: TableWindow
) -> dict[str, dict[str, Any]]:
    if not table_exists(cur, window.table):
        return {}

    existing_columns = {
        row["name"]
        for row in cur.execute(f"PRAGMA table_info('{window.table}')").fetchall()
    }
    selected_columns = [c for c in window.columns if c in existing_columns]
    if window.pk not in selected_columns:
        selected_columns.insert(0, window.pk)

    if not selected_columns:
        return {}

    col_list = ", ".join(selected_columns)
    sql = (
        f"SELECT {col_list} FROM {window.table} "
        f"ORDER BY {window.pk} DESC LIMIT {window.limit}"
    )
    rows = cur.execute(sql).fetchall()
    result: dict[str, dict[str, Any]] = {}

    for row in rows:
        mapped = {k: to_jsonable(row[k]) for k in row.keys()}
        result[str(row[window.pk])] = mapped

    return result


def build_snapshot(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    snapshot = {
        "created_at": now_iso(),
        "db_path": str(db_path),
        "integrity_check": cur.execute("PRAGMA integrity_check").fetchone()[0],
        "tables": {table: get_table_summary(cur, table) for table in TABLES},
        "windows": {w.table: get_window_rows(cur, w) for w in WINDOWS},
    }

    con.close()
    return snapshot


def diff_rows(
    old_rows: dict[str, dict[str, Any]],
    new_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    old_keys = set(old_rows.keys())
    new_keys = set(new_rows.keys())
    inserted = sorted(new_keys - old_keys, key=lambda x: int(x), reverse=True)
    removed = sorted(old_keys - new_keys, key=lambda x: int(x), reverse=True)

    updated: list[dict[str, Any]] = []
    for key in sorted(old_keys & new_keys, key=lambda x: int(x), reverse=True):
        if old_rows[key] != new_rows[key]:
            changed_fields = []
            for field in set(old_rows[key].keys()) | set(new_rows[key].keys()):
                if old_rows[key].get(field) != new_rows[key].get(field):
                    changed_fields.append(
                        {
                            "field": field,
                            "old": old_rows[key].get(field),
                            "new": new_rows[key].get(field),
                        }
                    )
            updated.append({"pk": key, "changed_fields": changed_fields})

    return {
        "inserted_pks": inserted,
        "removed_pks": removed,
        "updated": updated,
    }


def compare_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    table_diff: dict[str, Any] = {}
    for table in TABLES:
        old_t = old["tables"].get(table, {"exists": False})
        new_t = new["tables"].get(table, {"exists": False})
        table_diff[table] = {
            "old": old_t,
            "new": new_t,
            "count_delta": (new_t.get("count") or 0) - (old_t.get("count") or 0),
            "max_pk_delta": (new_t.get("max_pk") or 0) - (old_t.get("max_pk") or 0),
        }

    window_diff: dict[str, Any] = {}
    for w in WINDOWS:
        old_rows = old["windows"].get(w.table, {})
        new_rows = new["windows"].get(w.table, {})
        window_diff[w.table] = diff_rows(old_rows, new_rows)

    return {
        "old_created_at": old.get("created_at"),
        "new_created_at": new.get("created_at"),
        "integrity_old": old.get("integrity_check"),
        "integrity_new": new.get("integrity_check"),
        "table_diff": table_diff,
        "window_diff": window_diff,
    }


def sample_runtime_state(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path, timeout=1.0)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    table_stats = {
        table: get_table_summary(cur, table)
        for table in ["ZSYNCOBJECT", "ZCATEGORYASSIGMENT", "ZSYNCCOMMAND"]
    }

    latest_sync_commands = []
    if table_stats["ZSYNCCOMMAND"].get("exists"):
        rows = cur.execute(
            """
            SELECT
                Z_PK,
                ZCOMMANDID,
                ZISPENDING,
                ZOBJECTTYPE,
                ZOBJECTXMLDATATYPE,
                ZOBJECTGID,
                ZREVISION,
                ZOBJECTXMLDATA
            FROM ZSYNCCOMMAND
            ORDER BY Z_PK DESC
            LIMIT 5
            """
        ).fetchall()
        for row in rows:
            mapped = {k: to_jsonable(row[k]) for k in row.keys()}
            raw_xml = mapped.get("ZOBJECTXMLDATA")
            if isinstance(raw_xml, str):
                mapped["ZOBJECTXMLDATA_PREVIEW"] = raw_xml[:240]
                mapped["ZOBJECTXMLDATA_LEN"] = len(raw_xml)
            mapped.pop("ZOBJECTXMLDATA", None)
            latest_sync_commands.append(mapped)

    latest_transactions = []
    if table_stats["ZSYNCOBJECT"].get("exists"):
        rows = cur.execute(
            """
            SELECT
                Z_PK,
                Z_ENT,
                Z_OPT,
                ZGID,
                ZACCOUNT2,
                ZPAYEE2,
                ZAMOUNT1,
                ZDATE1,
                ZDESC2,
                ZNOTES1
            FROM ZSYNCOBJECT
            WHERE Z_ENT IN (37, 45, 46, 47)
            ORDER BY ZDATE1 DESC, Z_PK DESC
            LIMIT 20
            """
        ).fetchall()
        latest_transactions = [
            {k: to_jsonable(row[k]) for k in row.keys()} for row in rows
        ]

    latest_category_assignments = []
    if table_stats["ZCATEGORYASSIGMENT"].get("exists"):
        rows = cur.execute(
            """
            SELECT Z_PK, Z_OPT, ZCATEGORY, ZTRANSACTION, Z36_TRANSACTION, ZAMOUNT
            FROM ZCATEGORYASSIGMENT
            ORDER BY Z_PK DESC
            LIMIT 20
            """
        ).fetchall()
        latest_category_assignments = [
            {k: to_jsonable(row[k]) for k in row.keys()} for row in rows
        ]

    tx_signature = "|".join(
        f"{r['Z_PK']}:{r['Z_OPT']}:{r.get('ZDESC2')}:{r.get('ZAMOUNT1')}:{r.get('ZDATE1')}"
        for r in latest_transactions
    )
    ca_signature = "|".join(
        f"{r['Z_PK']}:{r['Z_OPT']}:{r.get('ZTRANSACTION')}:{r.get('ZCATEGORY')}:{r.get('ZAMOUNT')}"
        for r in latest_category_assignments
    )
    sc_signature = "|".join(
        f"{r['Z_PK']}:{r.get('ZCOMMANDID')}:{r.get('ZISPENDING')}:{r.get('ZOBJECTTYPE')}:{r.get('ZOBJECTGID')}"
        for r in latest_sync_commands
    )

    con.close()
    return {
        "sample_at": now_iso(),
        "table_stats": table_stats,
        "latest_sync_commands": latest_sync_commands,
        "latest_transactions": latest_transactions,
        "latest_category_assignments": latest_category_assignments,
        "signatures": {
            "transactions": tx_signature,
            "category_assignments": ca_signature,
            "sync_commands": sc_signature,
        },
    }


def watch_runtime(
    db_path: Path,
    duration_seconds: int,
    interval_seconds: float,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    start = time.monotonic()

    while time.monotonic() - start < duration_seconds:
        samples.append(sample_runtime_state(db_path))
        time.sleep(interval_seconds)

    # 再补一次尾样本
    samples.append(sample_runtime_state(db_path))

    transitions: list[dict[str, Any]] = []
    for idx in range(1, len(samples)):
        prev = samples[idx - 1]
        curr = samples[idx]

        prev_stats = prev["table_stats"]
        curr_stats = curr["table_stats"]

        changed = {}
        for table in curr_stats.keys():
            p = prev_stats[table]
            c = curr_stats[table]
            if p != c:
                changed[table] = {"old": p, "new": c}

        if changed:
            transitions.append(
                {
                    "from": prev["sample_at"],
                    "to": curr["sample_at"],
                    "changed": changed,
                }
            )

        prev_sig = prev.get("signatures", {})
        curr_sig = curr.get("signatures", {})
        signature_changed = {}
        for key in ["transactions", "category_assignments", "sync_commands"]:
            if prev_sig.get(key) != curr_sig.get(key):
                signature_changed[key] = {
                    "old": prev_sig.get(key, "")[:400],
                    "new": curr_sig.get(key, "")[:400],
                }

        if signature_changed:
            transitions.append(
                {
                    "from": prev["sample_at"],
                    "to": curr["sample_at"],
                    "changed_signatures": signature_changed,
                }
            )

    return {
        "db_path": str(db_path),
        "started_at": samples[0]["sample_at"] if samples else now_iso(),
        "ended_at": samples[-1]["sample_at"] if samples else now_iso(),
        "duration_seconds": duration_seconds,
        "interval_seconds": interval_seconds,
        "sample_count": len(samples),
        "transitions": transitions,
        "samples": samples,
    }


def print_watch_report(payload: dict[str, Any]) -> None:
    print("=== MoneyWiz 实时观测报告 ===")
    print(f"db: {payload['db_path']}")
    print(f"time: {payload['started_at']} -> {payload['ended_at']}")
    print(
        f"duration={payload['duration_seconds']}s, interval={payload['interval_seconds']}s, "
        f"samples={payload['sample_count']}"
    )
    print(f"transitions={len(payload['transitions'])}")

    for item in payload["transitions"][:20]:
        print(f"- transition {item['from']} -> {item['to']}")
        if "changed" in item:
            for table, change in item["changed"].items():
                print(f"  {table}: {change['old']} -> {change['new']}")
        if "changed_signatures" in item:
            for section, change in item["changed_signatures"].items():
                print(f"  signature[{section}]: {change['old']} -> {change['new']}")


def print_human_report(diff: dict[str, Any]) -> None:
    print("=== MoneyWiz DB 变化报告 ===")
    print(f"old snapshot: {diff['old_created_at']}")
    print(f"new snapshot: {diff['new_created_at']}")
    print(f"integrity: {diff['integrity_old']} -> {diff['integrity_new']}")
    print()

    print("[表级变化]")
    for table, item in diff["table_diff"].items():
        print(
            f"- {table}: count_delta={item['count_delta']}, "
            f"max_pk_delta={item['max_pk_delta']}"
        )
        if table == "ZSYNCCOMMAND":
            old_pending = item["old"].get("pending_count")
            new_pending = item["new"].get("pending_count")
            print(f"  pending_count: {old_pending} -> {new_pending}")

    print()
    print("[窗口级变化]")
    for table, item in diff["window_diff"].items():
        ins = item["inserted_pks"]
        upd = item["updated"]
        rem = item["removed_pks"]
        print(f"- {table}: inserted={len(ins)}, updated={len(upd)}, removed={len(rem)}")
        if ins:
            print(f"  inserted pks(top10): {ins[:10]}")
        if upd:
            print(f"  updated pks(top10): {[u['pk'] for u in upd[:10]]}")


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def main() -> None:
    parser = argparse.ArgumentParser(description="MoneyWiz 同步观测脚本")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="生成 baseline 快照")
    snapshot_parser.add_argument("--db", required=True, help="sqlite 文件路径")
    snapshot_parser.add_argument(
        "--out",
        required=True,
        help="快照输出路径（json）",
    )

    diff_parser = subparsers.add_parser("diff", help="与 baseline 对比")
    diff_parser.add_argument("--db", required=True, help="sqlite 文件路径")
    diff_parser.add_argument("--base", required=True, help="baseline 快照路径")
    diff_parser.add_argument("--out", required=False, help="diff 输出路径（json）")

    watch_parser = subparsers.add_parser("watch", help="实时观测同步相关变化")
    watch_parser.add_argument("--db", required=True, help="sqlite 文件路径")
    watch_parser.add_argument(
        "--duration-seconds",
        type=int,
        default=120,
        help="观测总时长（秒）",
    )
    watch_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="采样间隔（秒）",
    )
    watch_parser.add_argument("--out", required=False, help="观测输出路径（json）")

    args = parser.parse_args()

    if args.command == "snapshot":
        snapshot = build_snapshot(Path(args.db))
        save_json(Path(args.out), snapshot)
        print(f"baseline snapshot saved: {args.out}")
        return

    if args.command == "diff":
        base = load_json(Path(args.base))
        current = build_snapshot(Path(args.db))
        diff = compare_snapshots(base, current)
        print_human_report(diff)
        if args.out:
            save_json(Path(args.out), diff)
            print(f"diff json saved: {args.out}")

    if args.command == "watch":
        if args.duration_seconds <= 0:
            raise ValueError("--duration-seconds must be > 0")
        if args.interval_seconds <= 0:
            raise ValueError("--interval-seconds must be > 0")

        payload = watch_runtime(
            Path(args.db),
            duration_seconds=args.duration_seconds,
            interval_seconds=args.interval_seconds,
        )
        print_watch_report(payload)
        if args.out:
            save_json(Path(args.out), payload)
            print(f"watch json saved: {args.out}")


if __name__ == "__main__":
    main()
