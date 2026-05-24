import os
from typing import Iterable, Sequence, Tuple, Any, List

import pandas as pd
import openpyxl


def _normalize_value(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _pandas_read_excel(file_path: str, usecols=None, engine=None):
    if engine:
        return pd.read_excel(file_path, engine=engine, usecols=usecols)
    return pd.read_excel(file_path, usecols=usecols)


def load_excel_rows(file_path: str, usecols=None, min_row: int = 1, engines: Sequence = ("xlrd", None)) -> List[Tuple[Any, ...]]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    for engine in engines:
        try:
            df = _pandas_read_excel(file_path, usecols=usecols, engine=engine)
            if min_row > 1:
                df = df.iloc[min_row - 1 :]
            rows = []
            for row in df.itertuples(index=False):
                rows.append(tuple("" if pd.isna(value) else value for value in row))
            return rows
        except Exception:
            continue

    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        max_col = None
        usecols_list = None
        if usecols is not None:
            if isinstance(usecols, int):
                usecols_list = [usecols]
                max_col = usecols + 1
            else:
                usecols_list = list(usecols)
                max_col = max(usecols_list) + 1

        rows = []
        for row in worksheet.iter_rows(min_row=min_row, max_col=max_col, values_only=True):
            values = tuple("" if value is None else value for value in row)
            if usecols_list is not None:
                values = tuple(values[i] for i in usecols_list)
            rows.append(values)
        return rows
    finally:
        workbook.close()


def load_excel_column(file_path: str, col_index: int = 0, min_row: int = 1, engines: Sequence = ("xlrd", None)) -> List[Any]:
    rows = load_excel_rows(file_path, usecols=[col_index], min_row=min_row, engines=engines)
    return [row[0] if row else "" for row in rows]
