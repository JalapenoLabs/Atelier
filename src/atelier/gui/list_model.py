"""A Qt list model over a list of pydantic models, so QML delegates can bind to the models' fields by name."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel
from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, QPersistentModelIndex, Qt, Signal

logger = logging.getLogger(__name__)


class RowListModel(QAbstractListModel):
    """Exposes each row's fields as roles named after them (`model.name`, `model.depth` in a delegate)."""

    countChanged = Signal()

    def __init__(self, row_type: type[BaseModel]) -> None:
        super().__init__()
        self._field_names = list(row_type.model_fields)
        self._role_by_field = {name: Qt.ItemDataRole.UserRole + offset for offset, name in enumerate(self._field_names)}
        self._rows: list[BaseModel] = []

    @property
    def rows(self) -> list[BaseModel]:
        """The rows, in order."""
        return list(self._rows)

    def set_rows(self, rows: list[Any]) -> None:
        """Replace every row. Views reset, which is right for lists that are rebuilt whole."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.countChanged.emit()

    def insert_row(self, position: int, row: BaseModel) -> None:
        """Add one row at `position`, leaving the others as they are."""
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.insert(position, row)
        self.endInsertRows()
        self.countChanged.emit()

    def replace_row(self, position: int, row: BaseModel) -> None:
        """Replace one row in place."""
        self._rows[position] = row
        model_index = self.index(position, 0)
        self.dataChanged.emit(model_index, model_index)

    def update_rows(self, changed: Mapping[int, BaseModel]) -> None:
        """Replace several rows in place with one change signal over their span, rather than one per row."""
        if not changed:
            return
        for position, row in changed.items():
            self._rows[position] = row
        self.dataChanged.emit(self.index(min(changed), 0), self.index(max(changed), 0))

    def sync_rows(self, rows: list[Any], key: Callable[[Any], str]) -> None:
        """Turn the rows into `rows` by removing, inserting and replacing rows, so views keep their scroll and items.

        Rows are matched by `key`. A change that reorders rows still present resets the views instead, as
        `set_rows` does.
        """
        wanted_keys = {key(row) for row in rows}
        for position in reversed(range(len(self._rows))):
            if key(self._rows[position]) not in wanted_keys:
                self.beginRemoveRows(QModelIndex(), position, position)
                del self._rows[position]
                self.endRemoveRows()

        present_keys = {key(row) for row in self._rows}
        changed: dict[int, BaseModel] = {}
        for position, row in enumerate(rows):
            is_same_row = position < len(self._rows) and key(self._rows[position]) == key(row)
            if is_same_row:
                if self._rows[position] != row:
                    changed[position] = row
                continue
            if key(row) in present_keys:
                logger.debug("[RowListModel] Rows moved; resetting the views")
                self.set_rows(rows)
                return
            self.beginInsertRows(QModelIndex(), position, position)
            self._rows.insert(position, row)
            self.endInsertRows()
        self.update_rows(changed)
        self.countChanged.emit()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        """Return the number of rows; a list has no children."""
        if parent.isValid():
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Return one field of one row, by role."""
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            logger.debug(f"[RowListModel] Asked for row {index.row()} of {len(self._rows)}")
            return None
        field_index = role - Qt.ItemDataRole.UserRole
        if not 0 <= field_index < len(self._field_names):
            return None
        return getattr(self._rows[index.row()], self._field_names[field_index])

    def roleNames(self) -> dict[int, QByteArray]:
        """Name each role after its field."""
        return {role: QByteArray(name.encode()) for name, role in self._role_by_field.items()}
