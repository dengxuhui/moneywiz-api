from typing import Dict, Callable

from moneywiz_api.model.payee import Payee
from moneywiz_api.managers.record_manager import RecordManager


class PayeeManager(RecordManager[Payee]):
    def __init__(self):
        super().__init__()
        self._user_gid_to_id: Dict[tuple[int, str], int] = {}

    @property
    def ents(self) -> Dict[str, Callable]:
        return {
            "Payee": Payee,
        }

    def add(self, record: Payee) -> None:
        self._records[record.id] = record

        user_gid_key = (record.user, record.gid)
        if user_gid_key in self._user_gid_to_id:
            raise RuntimeError(
                f"Duplicate user+gid for {record}, existing record Id {self._user_gid_to_id[user_gid_key]}"
            )

        self._user_gid_to_id[user_gid_key] = record.id
        if record.gid not in self._gid_to_id:
            self._gid_to_id[record.gid] = record.id
