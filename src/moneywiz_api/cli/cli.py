import logging
import random
import readline
import rlcompleter
from code import InteractiveConsole
from collections import deque
from datetime import datetime
from decimal import Decimal
import json
import os
from os.path import expanduser
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from typing import Dict, List, Any, Optional

import click
import pandas as pd

from moneywiz_api.cli.helpers import ShellHelper
from moneywiz_api.moneywiz_api import MoneywizApi


def get_default_path() -> Path:
    return Path(
        expanduser(
            "~/Library/Containers/com.moneywiz.personalfinance/Data/Documents/.AppData/ipadMoneyWiz.sqlite"
        )
    )


def get_default_audit_log_path() -> Path:
    return Path(expanduser("~/.moneywiz_api/auto_bookkeeping.jsonl"))


def write_audit_log(log_path: Path, payload: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_main_moneywiz_db(db_path: Path) -> bool:
    path_text = str(db_path)
    return (
        "/Library/Containers/com.moneywiz.personalfinance/Data/Documents/.AppData/"
        in path_text
        or "/Library/Containers/com.moneywiz.personalfinance-setapp/Data/Documents/.AppData/"
        in path_text
    )


def get_sidecar_paths(db_path: Path) -> List[Path]:
    return [Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]


def ensure_safe_write_target(
    db_path: Path,
    *,
    allow_main_db_write: bool,
    skip_sidecar_check: bool,
) -> None:
    if is_main_moneywiz_db(db_path) and not allow_main_db_write:
        raise RuntimeError(
            "检测到你正在写 MoneyWiz 主库。"
            "默认禁止主库写入，请先用副本验证；"
            "若确认要写主库，请显式加 --allow-main-db-write。"
        )

    if skip_sidecar_check:
        return

    sidecar_states: List[str] = []
    for sidecar in get_sidecar_paths(db_path):
        if sidecar.exists():
            sidecar_states.append(f"{sidecar}({sidecar.stat().st_size} bytes)")

    con = sqlite3.connect(db_path, timeout=1.0)
    try:
        con.execute("PRAGMA busy_timeout = 1000")
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
    except sqlite3.OperationalError as error:
        raise RuntimeError(
            "数据库当前无法获取写锁（可能仍被 MoneyWiz 或其他进程占用）。"
            "请先彻底退出相关进程后重试。"
            "如你确认风险可控，可加 --skip-sidecar-check 跳过预检。"
            f" sidecars={sidecar_states}; error={error}"
        ) from error
    finally:
        con.close()


def create_db_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}.{timestamp}.sqlite"
    shutil.copy2(db_path, backup_path)
    return backup_path


def _get_running_moneywiz_processes() -> List[str]:
    try:
        completed = subprocess.run(
            ["pgrep", "-fl", "MoneyWiz"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return []

    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def ensure_moneywiz_not_running() -> None:
    processes = _get_running_moneywiz_processes()
    if processes:
        raise RuntimeError(
            "检测到 MoneyWiz 正在运行，为避免写库冲突已拒绝写入。"
            "请先关闭 MoneyWiz 后重试。"
            f" processes={processes}"
        )


def _detect_moneywiz_app_id() -> Optional[str]:
    candidates = [
        "com.moneywiz.personalfinance-setapp",
        "com.moneywiz.personalfinance",
    ]

    for app_id in candidates:
        check = subprocess.run(
            [
                "mdfind",
                f"kMDItemCFBundleIdentifier == '{app_id}' && kMDItemKind == 'Application'",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if check.returncode == 0 and check.stdout.strip():
            return app_id

    check_name = subprocess.run(
        ["osascript", "-e", 'id of app "MoneyWiz"'],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if check_name.returncode == 0:
        return "MoneyWiz"
    return None


def _run_applescript_for_app(app_id_or_name: str, action: str) -> Dict[str, Any]:
    if app_id_or_name == "MoneyWiz":
        script = f'tell application "MoneyWiz" to {action}'
    else:
        script = f'tell application id "{app_id_or_name}" to {action}'

    completed = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _get_sync_pending_count(db_path: Path) -> Optional[int]:
    con = None
    try:
        con = sqlite3.connect(db_path, timeout=1.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("PRAGMA busy_timeout = 1000")
        exists = cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM sqlite_master
            WHERE type='table' AND name='ZSYNCCOMMAND'
            """
        ).fetchone()["c"]
        if exists == 0:
            return None
        row = cur.execute(
            "SELECT COUNT(*) AS c FROM ZSYNCCOMMAND WHERE ZISPENDING = 1"
        ).fetchone()
        return int(row["c"])
    except Exception:
        return None
    finally:
        if con is not None:
            con.close()


def _wait_for_sync_idle(
    *,
    db_path: Path,
    timeout_seconds: int,
    poll_interval_seconds: int,
    stable_cycles: int,
) -> Dict[str, Any]:
    start = time.monotonic()
    stable_count = 0
    observed: List[Optional[int]] = []

    while time.monotonic() - start < timeout_seconds:
        pending_count = _get_sync_pending_count(db_path)
        observed.append(pending_count)

        if pending_count == 0:
            stable_count += 1
            if stable_count >= stable_cycles:
                return {
                    "completed": True,
                    "reason": "pending_zero_stable",
                    "observed": observed[-20:],
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                }
        else:
            stable_count = 0

        time.sleep(poll_interval_seconds)

    return {
        "completed": False,
        "reason": "timeout",
        "observed": observed[-20:],
        "elapsed_seconds": round(time.monotonic() - start, 2),
    }


def trigger_sync_via_applescript(
    *,
    db_path: Path,
    wait_mode: str,
    sync_wait_seconds: int,
    sync_timeout_seconds: int,
    sync_poll_interval_seconds: int,
    sync_stable_cycles: int,
) -> Dict[str, Any]:
    selected_app_id = _detect_moneywiz_app_id()

    if selected_app_id is None:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "No local MoneyWiz app detected for AppleScript sync",
        }

    activate_result = _run_applescript_for_app(selected_app_id, "activate")
    if activate_result["returncode"] != 0:
        return {
            "returncode": activate_result["returncode"],
            "stdout": activate_result["stdout"],
            "stderr": activate_result["stderr"],
            "app_id": selected_app_id,
            "wait_mode": wait_mode,
        }

    wait_result: Dict[str, Any]
    if wait_mode == "stateful":
        wait_result = _wait_for_sync_idle(
            db_path=db_path,
            timeout_seconds=sync_timeout_seconds,
            poll_interval_seconds=sync_poll_interval_seconds,
            stable_cycles=sync_stable_cycles,
        )
    else:
        time.sleep(sync_wait_seconds)
        wait_result = {
            "completed": True,
            "reason": "fixed_sleep",
            "observed": [],
            "elapsed_seconds": float(sync_wait_seconds),
        }

    quit_result = _run_applescript_for_app(selected_app_id, "quit")
    returncode = 0 if quit_result["returncode"] == 0 and wait_result["completed"] else 1

    return {
        "returncode": returncode,
        "stdout": activate_result["stdout"] or quit_result["stdout"],
        "stderr": activate_result["stderr"] or quit_result["stderr"],
        "app_id": selected_app_id,
        "wait_mode": wait_mode,
        "sync_wait_reason": wait_result["reason"],
        "sync_wait_elapsed_seconds": wait_result["elapsed_seconds"],
        "sync_pending_observed": wait_result["observed"],
    }


def trigger_sync_command(
    sync_command: str, timeout_seconds: int = 60
) -> Dict[str, Any]:
    completed = subprocess.run(
        sync_command,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def read_last_audit_events(log_path: Path, last: int) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []

    entries: deque[str] = deque(maxlen=last)
    with open(log_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                entries.append(line)

    events: List[Dict[str, Any]] = []
    for line in entries:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event": "invalid_log_line", "raw": line})
    return events


@click.command()
@click.argument(
    "DB_FILE_PATH",
    type=click.Path(writable=False, readable=True, exists=True),
    default=get_default_path(),
)
@click.option(
    "-d",
    "--demo-dump",
    is_flag=True,
    help="打印演示数据（只读，不写库）",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    help="日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL",
)
@click.option(
    "--add-transaction",
    is_flag=True,
    help="写入一条现金收支账单（自动去重）",
)
@click.option(
    "--kind",
    type=click.Choice(["expense", "income"], case_sensitive=False),
    help="账单类型：expense=支出，income=收入（配合 --add-transaction）",
)
@click.option(
    "--account-id",
    type=int,
    help="账户 ID（必填，配合 --add-transaction）",
)
@click.option(
    "--amount",
    type=float,
    help="金额（必填，传正数；支出/收入由 --kind 决定）",
)
@click.option(
    "--desc",
    "description",
    type=str,
    help="账单描述（必填）",
)
@click.option(
    "--payee-id",
    type=int,
    default=None,
    help="收/付款对象 ID（建议填写，增强 App 兼容性）",
)
@click.option(
    "--notes",
    type=str,
    default="",
    help="备注（可选，会写入账单备注）",
)
@click.option(
    "--category",
    "category_name",
    type=str,
    default=None,
    help="分类名称（可选；会按现有分类精确/模糊匹配）",
)
@click.option(
    "--datetime",
    "transaction_datetime",
    type=click.DateTime(formats=["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
    default=None,
    help="交易时间（可选，格式：YYYY-mm-dd HH:MM:SS 或 YYYY-mm-dd）",
)
@click.option(
    "--dedupe-window-seconds",
    type=int,
    default=600,
    show_default=True,
    help="去重时间窗口（秒）：同时间窗口内金额相同则视为重复",
)
@click.option(
    "--audit-log-path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=get_default_audit_log_path(),
    show_default=True,
    help="自动记账审计日志路径（JSONL，每行一条记录）",
)
@click.option(
    "--allow-main-db-write",
    is_flag=True,
    help="允许写入 MoneyWiz 主库（默认禁止，避免误操作）",
)
@click.option(
    "--skip-sidecar-check",
    is_flag=True,
    help="跳过写锁预检查（不建议）",
)
@click.option(
    "--backup-before-write/--no-backup-before-write",
    default=True,
    show_default=True,
    help="写入前自动备份数据库",
)
@click.option(
    "--backup-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(expanduser("~/.moneywiz_api/backups")),
    show_default=True,
    help="写入前备份文件目录",
)
@click.option(
    "--trigger-sync",
    is_flag=True,
    help="写入成功后触发同步命令",
)
@click.option(
    "--sync-command",
    type=str,
    default=None,
    help="同步命令（未传时读取环境变量 MONEYWIZ_SYNC_COMMAND）",
)
@click.option(
    "--sync-timeout-seconds",
    type=int,
    default=60,
    show_default=True,
    help="同步命令超时时间（秒）",
)
@click.option(
    "--sync-mode",
    type=click.Choice(["command", "applescript"], case_sensitive=False),
    default="applescript",
    show_default=True,
    help="同步触发模式：applescript(打开-等待-关闭) 或 command",
)
@click.option(
    "--sync-wait-seconds",
    type=int,
    default=20,
    show_default=True,
    help="applescript 同步模式中打开 MoneyWiz 后等待秒数",
)
@click.option(
    "--sync-wait-mode",
    type=click.Choice(["fixed", "stateful"], case_sensitive=False),
    default="stateful",
    show_default=True,
    help="applescript 等待模式：fixed=固定等待，stateful=轮询同步状态",
)
@click.option(
    "--sync-poll-interval-seconds",
    type=int,
    default=2,
    show_default=True,
    help="stateful 模式轮询间隔秒数",
)
@click.option(
    "--sync-stable-cycles",
    type=int,
    default=3,
    show_default=True,
    help="stateful 模式判定同步完成所需连续稳定次数",
)
@click.option(
    "--show-auto-logs",
    is_flag=True,
    help="查看自动记账审计日志并退出",
)
@click.option(
    "--last",
    "last_n_logs",
    type=int,
    default=20,
    show_default=True,
    help="查看最近 N 条审计日志（配合 --show-auto-logs）",
)
def main(
    db_file_path,
    demo_dump,
    log_level,
    add_transaction,
    kind,
    account_id,
    amount,
    description,
    payee_id,
    notes,
    category_name,
    transaction_datetime,
    dedupe_window_seconds,
    audit_log_path,
    allow_main_db_write,
    skip_sidecar_check,
    backup_before_write,
    backup_dir,
    trigger_sync,
    sync_command,
    sync_timeout_seconds,
    sync_mode,
    sync_wait_seconds,
    sync_wait_mode,
    sync_poll_interval_seconds,
    sync_stable_cycles,
    show_auto_logs,
    last_n_logs,
):
    """
    MoneyWiz 命令行入口。

    常见用法：
    1) 只读交互：不带 --add-transaction 时进入交互 shell。
    2) 自动记账：使用 --add-transaction + --kind + --account-id + --amount + --desc。
    3) 审计查询：使用 --show-auto-logs 查看自动记账历史。
    """

    # Configure logging level
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    logging.basicConfig(level=numeric_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    if show_auto_logs:
        if last_n_logs <= 0:
            raise ValueError("--last must be a positive integer")
        events = read_last_audit_events(audit_log_path, last_n_logs)
        if not events:
            click.secho(f"No audit logs found at {audit_log_path}", fg="yellow")
            return

        click.secho(
            f"Showing {len(events)} auto-bookkeeping log(s) from {audit_log_path}",
            fg="cyan",
        )
        for event in events:
            click.echo(json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2))
        return

    db_path = Path(db_file_path)

    if add_transaction:
        if kind is None:
            raise ValueError("--kind is required when using --add-transaction")
        if account_id is None:
            raise ValueError("--account-id is required when using --add-transaction")
        if amount is None:
            raise ValueError("--amount is required when using --add-transaction")
        if description is None:
            raise ValueError("--desc is required when using --add-transaction")
        if amount <= 0:
            raise ValueError("--amount must be a positive number")
        if dedupe_window_seconds < 0:
            raise ValueError("--dedupe-window-seconds must be >= 0")
        if sync_timeout_seconds <= 0:
            raise ValueError("--sync-timeout-seconds must be a positive integer")
        if sync_wait_seconds <= 0:
            raise ValueError("--sync-wait-seconds must be a positive integer")
        if sync_poll_interval_seconds <= 0:
            raise ValueError("--sync-poll-interval-seconds must be a positive integer")
        if sync_stable_cycles <= 0:
            raise ValueError("--sync-stable-cycles must be a positive integer")

        ensure_moneywiz_not_running()

        ensure_safe_write_target(
            db_path,
            allow_main_db_write=allow_main_db_write,
            skip_sidecar_check=skip_sidecar_check,
        )

        backup_path = None
        if backup_before_write:
            backup_path = create_db_backup(db_path, backup_dir)

        moneywiz_api = MoneywizApi(db_path)

        if transaction_datetime is None:
            transaction_datetime = datetime.now()

        accessor = moneywiz_api.accessor
        get_account_currency = getattr(accessor, "get_account_currency")
        add_cash_transaction = getattr(accessor, "add_cash_transaction")

        currency = get_account_currency(account_id)
        if currency is None:
            raise ValueError(f"Account {account_id} not found or has no currency")

        audit_event: Dict[str, Any] = {
            "event": "auto_bookkeeping_write",
            "run_at": datetime.now().isoformat(),
            "status": "failed",
            "db_path": str(db_path),
            "kind": kind.lower(),
            "account_id": account_id,
            "amount": float(amount),
            "description": description,
            "payee_id": payee_id,
            "notes": notes,
            "category_name": category_name,
            "transaction_datetime": transaction_datetime.isoformat(),
            "original_currency": currency,
            "dedupe_window_seconds": dedupe_window_seconds,
            "allow_main_db_write": allow_main_db_write,
            "skip_sidecar_check": skip_sidecar_check,
            "backup_before_write": backup_before_write,
            "backup_path": str(backup_path) if backup_path else None,
            "trigger_sync": trigger_sync,
            "sync_mode": sync_mode.lower(),
            "sync_wait_seconds": sync_wait_seconds,
            "sync_wait_mode": sync_wait_mode.lower(),
            "sync_poll_interval_seconds": sync_poll_interval_seconds,
            "sync_stable_cycles": sync_stable_cycles,
        }

        try:
            transaction_id, inserted = add_cash_transaction(
                kind=kind.lower(),
                account_id=account_id,
                amount=Decimal(str(amount)),
                description=description,
                transaction_datetime=transaction_datetime,
                original_currency=currency,
                notes=notes,
                payee_id=payee_id,
                dedupe_window_seconds=dedupe_window_seconds,
                category_name=category_name,
            )
            created_record = accessor.get_record(transaction_id)
            audit_event["status"] = "success"
            audit_event["action"] = "inserted" if inserted else "deduplicated"
            audit_event["created_id"] = transaction_id
            audit_event["created_gid"] = created_record.gid

            if trigger_sync:
                if sync_mode.lower() == "applescript":
                    sync_result = trigger_sync_via_applescript(
                        db_path=db_path,
                        wait_mode=sync_wait_mode.lower(),
                        sync_wait_seconds=sync_wait_seconds,
                        sync_timeout_seconds=sync_timeout_seconds,
                        sync_poll_interval_seconds=sync_poll_interval_seconds,
                        sync_stable_cycles=sync_stable_cycles,
                    )
                    audit_event["sync_command"] = "osascript: activate->wait-mode->quit"
                else:
                    final_sync_command = sync_command or os.getenv(
                        "MONEYWIZ_SYNC_COMMAND"
                    )
                    if not final_sync_command:
                        raise RuntimeError(
                            "--trigger-sync 已开启，但未提供 --sync-command，"
                            "且环境变量 MONEYWIZ_SYNC_COMMAND 未设置。"
                        )
                    sync_result = trigger_sync_command(
                        final_sync_command,
                        timeout_seconds=sync_timeout_seconds,
                    )
                    audit_event["sync_command"] = final_sync_command

                audit_event["sync_timeout_seconds"] = sync_timeout_seconds
                audit_event["sync_returncode"] = sync_result["returncode"]
                audit_event["sync_stdout"] = sync_result["stdout"][:2000]
                audit_event["sync_stderr"] = sync_result["stderr"][:2000]
                if "app_id" in sync_result:
                    audit_event["sync_app_id"] = sync_result["app_id"]
                if "wait_mode" in sync_result:
                    audit_event["sync_wait_mode"] = sync_result["wait_mode"]
                if "sync_wait_reason" in sync_result:
                    audit_event["sync_wait_reason"] = sync_result["sync_wait_reason"]
                if "sync_wait_elapsed_seconds" in sync_result:
                    audit_event["sync_wait_elapsed_seconds"] = sync_result[
                        "sync_wait_elapsed_seconds"
                    ]
                if "sync_pending_observed" in sync_result:
                    audit_event["sync_pending_observed"] = sync_result[
                        "sync_pending_observed"
                    ]
                audit_event["sync_status"] = (
                    "success" if sync_result["returncode"] == 0 else "failed"
                )
                if sync_result["returncode"] != 0:
                    raise RuntimeError(
                        "同步命令执行失败，"
                        f"returncode={sync_result['returncode']}, stderr={sync_result['stderr']}"
                    )

            write_audit_log(audit_log_path, audit_event)
        except Exception as error:
            audit_event["error"] = str(error)
            write_audit_log(audit_log_path, audit_event)
            raise

        if inserted:
            click.secho(f"Created transaction with id={transaction_id}", fg="green")
        else:
            click.secho(
                f"Skipped duplicate, existing transaction id={transaction_id}",
                fg="yellow",
            )
        if backup_path is not None:
            click.secho(f"Database backup created at {backup_path}", fg="cyan")
        if trigger_sync:
            click.secho("Sync command finished successfully", fg="green")
        click.secho(f"Audit log written to {audit_log_path}", fg="cyan")
        return

    moneywiz_api = MoneywizApi(db_path)

    (
        accessor,
        account_manager,
        payee_manager,
        category_manager,
        transaction_manager,
        investment_holding_manager,
        tag_manager,
    ) = (
        moneywiz_api.accessor,
        moneywiz_api.account_manager,
        moneywiz_api.payee_manager,
        moneywiz_api.category_manager,
        moneywiz_api.transaction_manager,
        moneywiz_api.investment_holding_manager,
        moneywiz_api.tag_manager,
    )

    helper = ShellHelper(moneywiz_api)

    names: Dict[str, str] = {
        f"{moneywiz_api=}".split("=")[0]: "MoneyWiz API",
        f"{accessor=}".split("=")[0]: "MoneyWiz Database Accessor",
        f"{account_manager=}".split("=")[0]: "Account Manager",
        f"{payee_manager=}".split("=")[0]: "Payee Manager",
        f"{category_manager=}".split("=")[0]: "Category Manager",
        f"{transaction_manager=}".split("=")[0]: "Transaction Manager",
        f"{investment_holding_manager=}".split("=")[0]: "Investment Holding Manager",
        f"{tag_manager=}".split("=")[0]: "Tag Manager",
        f"{helper=}".split("=")[0]: "Shell Helper",
    }

    banner: List[str] = [
        f"Read-only MoneyWiz Shell on {db_file_path}",
        "",
        "Available components:",
        *[f"- {component:30}  {desc}" for component, desc in names.items()],
        "===================================================================",
    ]

    if demo_dump:
        _users_table = helper.users_table()
        click.secho("Users Table", fg="yellow")
        click.secho("--------------------------------", fg="yellow")
        click.secho(_users_table.to_string(index=False))
        click.secho("--------------------------------\n", fg="yellow")

        _userid_list = _users_table["id"].tolist()
        _userid_list.remove(1)
        _user_id = random.choice(_userid_list)

        _categories_table = helper.categories_table(_user_id)
        click.secho(f"Categories Table for User {_user_id}", fg="yellow")
        click.secho("--------------------------------", fg="yellow")
        click.secho(
            _categories_table[["id", "name", "type"]].sample(5).to_string(index=False)
        )
        click.secho("--------------------------------\n", fg="yellow")

        _accounts_table = helper.accounts_table(_user_id)
        click.secho(f"Accounts Table for User {_user_id}", fg="yellow")
        click.secho("--------------------------------", fg="yellow")
        click.secho(_accounts_table[["id", "name"]].sample(5).to_string(index=False))
        click.secho("--------------------------------\n", fg="yellow")

    _vars = globals()
    _vars.update(locals())

    pd.options.display.max_rows = None
    pd.options.display.max_colwidth = None

    readline.set_completer(rlcompleter.Completer(_vars).complete)
    readline.parse_and_bind("tab: complete")
    InteractiveConsole(_vars).interact(banner="\n".join(banner))


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
