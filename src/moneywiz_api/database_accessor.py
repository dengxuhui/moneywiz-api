import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Any, Callable, Tuple, Optional
import random
from uuid import uuid4
from xml.sax.saxutils import escape

from moneywiz_api.model.record import Record
from moneywiz_api.model.raw_data_handler import RawDataHandler as RDH
from moneywiz_api.types import ENT_ID, ID, GID
from moneywiz_api.utils import get_date, get_datetime


class DatabaseAccessor:
    def __init__(self, db_path: Path):
        self._con = sqlite3.connect(db_path, uri=True)

        def dict_factory(cursor, row):
            record = {}
            for idx, col in enumerate(cursor.description):
                record[col[0]] = row[idx]
            return record

        self._con.row_factory = dict_factory

        self._ent_to_typename: Dict[ENT_ID, str] = self._load_primarykey()
        self._typename_to_ent: Dict[str, ENT_ID] = {
            v: k for k, v in self._ent_to_typename.items()
        }

    def _load_primarykey(self) -> Dict[int, str]:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT * FROM  "Z_PRIMARYKEY" ORDER BY "Z_ENT" LIMIT 1000 OFFSET 0;
        """
        )
        ent_to_typename: Dict[int, str] = {}
        for row in res.fetchall():
            ent_to_typename[row["Z_ENT"]] = row["Z_NAME"]
        return ent_to_typename

    def __repr__(self):
        return "\n".join(
            f"{key}: {value}" for key, value in self._ent_to_typename.items()
        )

    def typename_for(self, ent_id: ENT_ID) -> Optional[str]:
        return self._ent_to_typename.get(ent_id)

    def ent_for(self, typename: str) -> Optional[ENT_ID]:
        return self._typename_to_ent.get(typename)

    def query_objects(self, typenames: List[str]) -> List[Any]:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT * FROM ZSYNCOBJECT WHERE Z_ENT in (%s)
        """
            % (",".join("?" * len(typenames))),
            [self.ent_for(x) for x in typenames],
        )
        return res.fetchall()

    def get_record(self, pk_id: ID, constructor: Callable = Record):
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT * FROM ZSYNCOBJECT WHERE Z_PK = ?
        
        """,
            [pk_id],
        )

        return constructor(res.fetchone())

    def get_record_by_gid(self, gid: GID, constructor: Callable = Record):
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT * FROM ZSYNCOBJECT WHERE ZGID = ?
        
        """,
            [gid],
        )

        return constructor(res.fetchone())

    def get_category_assignment(self) -> Dict[ID, List[Tuple[ID, Decimal]]]:
        transaction_map: Dict[ID, List[Tuple[ID, Decimal]]] = defaultdict(list)
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT ZCATEGORY, ZTRANSACTION, ZAMOUNT  FROM ZCATEGORYASSIGMENT WHERE ZTRANSACTION IS NOT NULL
        
        """
        )
        for row in res.fetchall():
            transaction_map[row["ZTRANSACTION"]].append(
                (row["ZCATEGORY"], RDH.get_decimal(row, "ZAMOUNT"))
            )
        return transaction_map

    def get_refund_maps(self) -> Dict[ID, ID]:
        refund_to_withdraw: Dict[ID, ID] = {}
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT ZREFUNDTRANSACTION, ZWITHDRAWTRANSACTION  FROM ZWITHDRAWREFUNDTRANSACTIONLINK
        
        """
        )
        for row in res.fetchall():
            refund_to_withdraw[row["ZREFUNDTRANSACTION"]] = row["ZWITHDRAWTRANSACTION"]
        return refund_to_withdraw

    def get_tags_map(self) -> Dict[ID, List[ID]]:
        transactions_to_tags: Dict[ID, List[ID]] = defaultdict(list)
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT Z_36TRANSACTIONS, Z_35TAGS FROM  Z_36TAGS
        
        """
        )
        for row in res.fetchall():
            transactions_to_tags[row["Z_36TRANSACTIONS"]].append(row["Z_35TAGS"])
        return transactions_to_tags

    def get_users(self) -> Dict[ID, str]:
        users_map: Dict[ID, str] = {}
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT Z_PK, ZSYNCLOGIN FROM  "ZUSER"
        
        """
        )
        for row in res.fetchall():
            users_map[row["Z_PK"]] = row["ZSYNCLOGIN"]
        return users_map

    def get_account_currency(self, account_id: ID) -> Optional[str]:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT ZCURRENCYNAME FROM ZSYNCOBJECT WHERE Z_PK = ?
        """,
            [account_id],
        )
        row = res.fetchone()
        if not row:
            return None
        return row["ZCURRENCYNAME"]

    def get_account_meta(self, account_id: ID) -> Optional[Tuple[str, ENT_ID]]:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT ZCURRENCYNAME, Z_ENT FROM ZSYNCOBJECT WHERE Z_PK = ?
        """,
            [account_id],
        )
        row = res.fetchone()
        if not row:
            return None
        currency = row["ZCURRENCYNAME"]
        account_ent = row["Z_ENT"]
        if currency is None or account_ent is None:
            return None
        return currency, account_ent

    def has_record(self, record_id: ID) -> bool:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT Z_PK FROM ZSYNCOBJECT WHERE Z_PK = ?
        """,
            [record_id],
        )
        return res.fetchone() is not None

    def get_account_exchange_rate(self, account_id: ID) -> float:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT ZCURRENCYEXCHANGERATE
        FROM ZSYNCOBJECT
        WHERE ZACCOUNT2 = ? AND ZCURRENCYEXCHANGERATE IS NOT NULL
        ORDER BY ZDATE1 DESC, Z_PK DESC
        LIMIT 1
        """,
            [account_id],
        )
        row = res.fetchone()
        if not row:
            return 1.0
        rate = row["ZCURRENCYEXCHANGERATE"]
        if rate is None:
            return 1.0
        return float(rate)

    def _next_sync_command_id(self) -> ID:
        cur = self._con.cursor()
        row = cur.execute(
            "SELECT COALESCE(MAX(Z_PK), 0) + 1 AS NEXT_PK FROM ZSYNCCOMMAND"
        ).fetchone()
        return row["NEXT_PK"]

    def _next_sync_revision(self, user_id: ID) -> int:
        cur = self._con.cursor()
        row = cur.execute(
            """
            SELECT COALESCE(MAX(ZREVISION), 0) + 1 AS NEXT_REV
            FROM ZSYNCCOMMAND
            WHERE ZUSER = ?
            """,
            [user_id],
        ).fetchone()
        return int(row["NEXT_REV"])

    def _serialize_transaction_xml_data(self, transaction_id: ID) -> str:
        cur = self._con.cursor()
        row = cur.execute(
            """
            SELECT
                ZGID,
                ZSTATUS1,
                ZCHECKBOOKNUMBER,
                ZAMOUNT1,
                ZPRICEPERSHARE1,
                ZCURRENCYEXCHANGERATE,
                ZORIGINALAMOUNT,
                ZORIGINALCURRENCY,
                ZFLAGS1,
                ZMARKEDASNEWSINCEDATE,
                ZDATE1,
                ZDESC2,
                ZNOTES1,
                ZORIGINALFEE,
                ZFEE2,
                ZORIGINALEXCHANGERATE,
                ZRECONCILED,
                ZVOIDCHEQUE,
                ZACCOUNT2,
                Z9_ACCOUNT2,
                ZPAYEE2,
                Z_ENT,
                ZOBJECTCREATIONDATE,
                Z_OPT
            FROM ZSYNCOBJECT
            WHERE Z_PK = ?
            """,
            [transaction_id],
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"Cannot find transaction for xml serialization: {transaction_id}"
            )

        category_rows = cur.execute(
            """
            SELECT
                ca.ZAMOUNT,
                c.ZGID AS CATEGORY_GID,
                c.ZNAME2 AS CATEGORY_NAME
            FROM ZCATEGORYASSIGMENT ca
            JOIN ZSYNCOBJECT c ON c.Z_PK = ca.ZCATEGORY
            WHERE ZTRANSACTION = ?
            ORDER BY ca.Z_PK
            """,
            [transaction_id],
        ).fetchall()

        payee_name = ""
        payee_gid = ""
        if row["ZPAYEE2"] is not None:
            payee_row = cur.execute(
                "SELECT ZNAME5, ZGID FROM ZSYNCOBJECT WHERE Z_PK = ?",
                [row["ZPAYEE2"]],
            ).fetchone()
            if payee_row:
                if payee_row["ZNAME5"]:
                    payee_name = str(payee_row["ZNAME5"])
                if payee_row["ZGID"]:
                    payee_gid = str(payee_row["ZGID"])

        account_gid = ""
        if row["ZACCOUNT2"] is not None:
            account_row = cur.execute(
                "SELECT ZGID FROM ZSYNCOBJECT WHERE Z_PK = ?",
                [row["ZACCOUNT2"]],
            ).fetchone()
            if account_row and account_row["ZGID"]:
                account_gid = str(account_row["ZGID"])

        def value(key: str, default: Any = "") -> Any:
            v = row[key]
            return default if v is None else v

        transaction_type = (
            "Expense transaction"
            if int(value("Z_ENT", 47)) == 47
            else "Income transaction"
        )
        dt_obj = get_datetime(float(value("ZDATE1", 0)))
        dt_iso = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
        dt_offset = dt_obj.strftime("%z") or "+0000"
        categories_xml = "<categories></categories>"
        if category_rows:
            items = []
            for category_row in category_rows:
                items.append(
                    "<category objectGID='"
                    + escape(str(category_row["CATEGORY_GID"]))
                    + "' name='"
                    + escape(str(category_row["CATEGORY_NAME"]))
                    + "' amount='"
                    + escape(str(category_row["ZAMOUNT"]))
                    + "'></category>"
                )
            categories_xml = "<categories>" + "".join(items) + "</categories>"

        cutoff_utc = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
        object_creation_date_1970 = float(value("ZOBJECTCREATIONDATE", 0)) + cutoff_utc

        xml = (
            "<objectData "
            + f"status='{escape(str(value('ZSTATUS1', 2)))}' "
            + f"checkbookNumber='{escape(str(value('ZCHECKBOOKNUMBER', '')))}' "
            + f"amount='{escape(str(value('ZAMOUNT1', 0)))}' "
            + f"pricePerShare='{escape(str(value('ZPRICEPERSHARE1', 0)))}' "
            + f"currencyExchangeRate='{escape(str(value('ZCURRENCYEXCHANGERATE', 1)))}' "
            + f"original_amount='{escape(str(value('ZORIGINALAMOUNT', 0)))}' "
            + f"original_currency='{escape(str(value('ZORIGINALCURRENCY', 'CNY')))}' "
            + f"flags='{escape(str(value('ZFLAGS1', 0)))}' "
            + f"markedAsNewSinceDate='{escape(str(value('ZMARKEDASNEWSINCEDATE', '')))}' "
            + f"type='{escape(transaction_type)}' "
            + f"date='{escape(dt_iso + ' ' + dt_offset)}' "
            + "autoSkipLinkedScheduledTransactionGID='' "
            + f"originalFee='{escape(str(value('ZORIGINALFEE', 0)))}' "
            + f"payeeName='{escape(payee_name)}' "
            + f"account_gid='{escape(account_gid)}' "
            + f"reconciled='{escape('1' if int(value('ZRECONCILED', 0)) else '0')}' "
            + f"objectGID='{escape(str(value('ZGID', '')))}' "
            + f"isVoidCheque='{escape('1' if int(value('ZVOIDCHEQUE', 0)) else '0')}' "
            + f"notes='{escape(str(value('ZNOTES1', '')))}' "
            + f"desc='{escape(str(value('ZDESC2', '')))}' "
            + f"fee='{escape(str(value('ZFEE2', 0)))}' "
            + f"original_exchangeRate='{escape(str(value('ZORIGINALEXCHANGERATE', 1)))}' "
            + f"payeeGID='{escape(payee_gid)}' "
            + f"objectCreationDate1970='{escape(str(object_creation_date_1970))}'"
            + ">"
            + categories_xml
            + "<images></images>"
            + "<tags_list></tags_list>"
            + "</objectData>"
        )
        return xml

    def enqueue_transaction_sync_command(self, transaction_id: ID) -> ID:
        cur = self._con.cursor()
        tx = cur.execute(
            "SELECT ZGID, ZUSER FROM ZSYNCOBJECT WHERE Z_PK = ?",
            [transaction_id],
        ).fetchone()
        if not tx:
            raise RuntimeError(f"Cannot find transaction for enqueue: {transaction_id}")

        transaction_gid = tx["ZGID"]
        user_id = tx["ZUSER"] if tx["ZUSER"] is not None else 2

        command_id = self._next_sync_command_id()
        revision = self._next_sync_revision(user_id)
        xml_data = self._serialize_transaction_xml_data(transaction_id)

        cur.execute(
            """
            INSERT INTO ZSYNCCOMMAND (
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                command_id,
                7,
                1,
                0,
                0,
                1,
                0,
                0,
                revision,
                user_id,
                transaction_gid,
                xml_data,
            ],
        )
        return command_id

    def _next_pk_id(self) -> ID:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT COALESCE(MAX(Z_PK), 0) + 1 AS NEXT_PK FROM ZSYNCOBJECT
        """
        )
        row = res.fetchone()
        return row["NEXT_PK"]

    def _next_category_assignment_id(self) -> ID:
        cur = self._con.cursor()
        res = cur.execute(
            """
        SELECT COALESCE(MAX(Z_PK), 0) + 1 AS NEXT_PK FROM ZCATEGORYASSIGMENT
        """
        )
        row = res.fetchone()
        return row["NEXT_PK"]

    def find_category_id(
        self, category_name: str, *, for_expense: bool
    ) -> Optional[ID]:
        cur = self._con.cursor()
        type_value = 1 if for_expense else 2

        exact = cur.execute(
            """
        SELECT Z_PK
        FROM ZSYNCOBJECT
        WHERE Z_ENT = 19 AND ZTYPE2 = ? AND ZNAME2 = ?
        ORDER BY ZUSER3 DESC, Z_PK DESC
        LIMIT 1
        """,
            [type_value, category_name],
        ).fetchone()
        if exact:
            return exact["Z_PK"]

        fuzzy = cur.execute(
            """
        SELECT Z_PK
        FROM ZSYNCOBJECT
        WHERE Z_ENT = 19 AND ZTYPE2 = ? AND ZNAME2 LIKE ?
        ORDER BY ZUSER3 DESC, Z_PK DESC
        LIMIT 1
        """,
            [type_value, f"%{category_name}%"],
        ).fetchone()
        if fuzzy:
            return fuzzy["Z_PK"]

        return None

    def ensure_category_assignment(
        self,
        *,
        transaction_id: ID,
        transaction_ent_id: ENT_ID,
        category_id: ID,
        amount: Decimal,
    ) -> ID:
        cur = self._con.cursor()
        existing = cur.execute(
            """
        SELECT Z_PK
        FROM ZCATEGORYASSIGMENT
        WHERE ZTRANSACTION = ? AND ZCATEGORY = ?
        LIMIT 1
        """,
            [transaction_id, category_id],
        ).fetchone()
        if existing:
            cur.execute(
                """
            UPDATE ZCATEGORYASSIGMENT
            SET ZAMOUNT = ?, Z36_TRANSACTION = ?
            WHERE Z_PK = ?
            """,
                [float(amount), transaction_ent_id, existing["Z_PK"]],
            )
            return existing["Z_PK"]

        assignment_id = self._next_category_assignment_id()
        cur.execute(
            """
        INSERT INTO ZCATEGORYASSIGMENT (
            Z_PK,
            Z_ENT,
            Z_OPT,
            ZASSIGMENTNUMBER,
            ZCATEGORY,
            ZTRANSACTION,
            Z36_TRANSACTION,
            ZAMOUNT
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            [
                assignment_id,
                2,
                1,
                0,
                category_id,
                transaction_id,
                transaction_ent_id,
                float(amount),
            ],
        )
        return assignment_id

    def add_cash_transaction(
        self,
        *,
        kind: str,
        account_id: ID,
        amount: Decimal,
        description: str,
        transaction_datetime: datetime,
        original_currency: str,
        notes: Optional[str] = None,
        payee_id: Optional[ID] = None,
        dedupe_window_seconds: int = 600,
        dedupe_amount_tolerance: Decimal = Decimal("0.01"),
        category_name: Optional[str] = None,
        write_datetime_utc0: bool = True,
    ) -> Tuple[ID, bool]:
        kind_to_ent = {
            "expense": "WithdrawTransaction",
            "income": "DepositTransaction",
        }
        if kind not in kind_to_ent:
            raise ValueError(f"Unsupported kind: {kind}")

        if description is None:
            raise ValueError("description cannot be None")
        description = description.strip()

        if kind == "expense":
            normalized_amount = -abs(amount)
            flags = 0
        else:
            normalized_amount = abs(amount)
            flags = 1

        ent_id = self.ent_for(kind_to_ent[kind])
        if ent_id is None:
            raise RuntimeError(f"Cannot find ENT for {kind_to_ent[kind]}")

        account_meta = self.get_account_meta(account_id)
        if account_meta is None:
            raise ValueError(f"Account {account_id} not found or has no meta")
        _, account_ent = account_meta

        cur = self._con.cursor()

        def to_moneywiz_date(dt: datetime) -> float:
            if write_datetime_utc0:
                cutoff_utc = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
                return dt.timestamp() - cutoff_utc
            return get_date(dt)

        try:
            cur.execute("BEGIN IMMEDIATE")

            date_value = to_moneywiz_date(transaction_datetime)
            duplicate = cur.execute(
                """
            SELECT Z_PK
            FROM ZSYNCOBJECT
            WHERE Z_ENT IN (37, 47)
              AND ABS(ZAMOUNT1 - ?) <= ?
              AND ABS(ZDATE1 - ?) <= ?
            ORDER BY ABS(ZDATE1 - ?) ASC, Z_PK DESC
            LIMIT 1
            """,
                [
                    float(normalized_amount),
                    float(dedupe_amount_tolerance),
                    date_value,
                    dedupe_window_seconds,
                    date_value,
                ],
            ).fetchone()
            if duplicate:
                if category_name:
                    category_id = self.find_category_id(
                        category_name,
                        for_expense=(kind == "expense"),
                    )
                    if category_id is not None:
                        self.ensure_category_assignment(
                            transaction_id=duplicate["Z_PK"],
                            transaction_ent_id=ent_id,
                            category_id=category_id,
                            amount=normalized_amount,
                        )
                        self._con.commit()
                    else:
                        self._con.rollback()
                else:
                    self._con.rollback()
                return duplicate["Z_PK"], False

            next_id = self._next_pk_id()
            gid = (
                f"{str(uuid4()).upper()}-{random.randint(10000, 99999)}-"
                f"{uuid4().hex[:16].upper()}"
            )
            now_dt = (
                datetime.now(timezone.utc) if write_datetime_utc0 else datetime.now()
            )
            now_value = to_moneywiz_date(now_dt)
            exchange_rate = self.get_account_exchange_rate(account_id)

            cur.execute(
                """
            INSERT INTO ZSYNCOBJECT (
                Z_PK,
                Z_ENT,
                Z_OPT,
                ZFLAGS1,
                ZRECONCILED,
                ZSTATUS1,
                ZVOIDCHEQUE,
                ZACCOUNT2,
                Z9_ACCOUNT2,
                ZPAYEE2,
                ZOBJECTCREATIONDATE,
                ZAMOUNT1,
                ZCURRENCYEXCHANGERATE,
                ZDATE1,
                ZFEE2,
                ZORIGINALAMOUNT,
                ZORIGINALEXCHANGERATE,
                ZORIGINALFEE,
                ZPRICEPERSHARE1,
                ZGID,
                ZCHECKBOOKNUMBER,
                ZDESC2,
                ZNOTES1,
                ZORIGINALCURRENCY
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
                [
                    next_id,
                    ent_id,
                    1,
                    flags,
                    0,
                    2,
                    0,
                    account_id,
                    account_ent,
                    payee_id,
                    now_value,
                    float(normalized_amount),
                    exchange_rate,
                    date_value,
                    0.0,
                    float(normalized_amount),
                    1.0,
                    0.0,
                    0.0,
                    gid,
                    "",
                    description,
                    notes or "",
                    original_currency,
                ],
            )

            if category_name:
                category_id = self.find_category_id(
                    category_name,
                    for_expense=(kind == "expense"),
                )
                if category_id is not None:
                    self.ensure_category_assignment(
                        transaction_id=next_id,
                        transaction_ent_id=ent_id,
                        category_id=category_id,
                        amount=normalized_amount,
                    )

            self.enqueue_transaction_sync_command(next_id)
            self._con.commit()
            return next_id, True
        except Exception:
            self._con.rollback()
            raise
