"""A Qt list model over a list of pydantic models, so QML delegates can bind to the models' fields by name."""

from __future__ import annotations

import logging
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
