"""Preview text tables before importing, without guessing optical units or k.

The original material importer remains responsible for sorting wavelengths,
duplicate checks, and optical validation. This adapter adds common desktop CSV
encodings/delimiters and retains the ORIGINAL file hash/line provenance.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import tempfile


@dataclass(frozen=True)
class MaterialTable:
    path: Path
    sha256: str
    encoding: str
    delimiter: str
    header: str | None
    rows: list[list[float]]
    line_numbers: list[int]

    @property
    def column_count(self):
        return len(self.rows[0])


def read_material_table(path, *, delimiter="auto", encoding="auto"):
    """Read a bounded local table; any malformed data row aborts the import."""
    path = Path(path).resolve()
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("材料表超过 20 MB；请先精简为需要的波长、n、k 列。")
    raw = path.read_bytes()
    codecs = (["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]) if encoding == "auto" else [encoding]
    decoded = None
    for codec in codecs:
        try:
            decoded = raw.decode(codec)
            encoding = codec
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if decoded is None:
        raise ValueError("无法读取材料表编码；请另存为 UTF-8 CSV / TXT 后导入。")
    if "\x00" in decoded:
        raise ValueError("材料表含空字节；请确认选择的是文本文件，并另存为 UTF-8。")
    lines = [(i, line.strip()) for i, line in enumerate(decoded.splitlines(), 1)
             if line.strip() and not line.lstrip().startswith(("#", "//", ";"))]
    if not lines:
        raise ValueError("材料表为空，或只有注释；需要至少两个不同波长的数据行。")
    if delimiter == "auto":
        first = re.split(r"#|//", lines[0][1], maxsplit=1)[0]
        delimiter = "comma" if "," in first else ("semicolon" if ";" in first else "whitespace")
    if delimiter not in ("comma", "semicolon", "whitespace"):
        raise ValueError("请选择逗号、分号或空格 / 制表符分隔。")
    rows, numbers, header = [], [], None
    for line_number, text in lines:
        comment_pattern = r"#|//" if delimiter == "semicolon" else r"#|//|;"
        text = re.split(comment_pattern, text, maxsplit=1)[0].strip()
        if not text:
            continue
        try:
            fields = re.split(r"\s+", text) if delimiter == "whitespace" else next(csv.reader([text], delimiter="," if delimiter == "comma" else ";", strict=True))
        except csv.Error as exc:
            raise ValueError(f"第 {line_number} 行：CSV 引号格式错误（{exc}）。") from None
        if any(not field.strip() for field in fields):
            raise ValueError(f"第 {line_number} 行：有空列；不能跳过缺失数据。缺少整列 k 时，请留空 k 列号。")
        try:
            row = [float(field.strip()) for field in fields]
        except ValueError:
            # One header line is permitted. Subsequent nonnumeric lines are
            # errors, never dropped as extra headers or interpolated silently.
            if not rows and header is None:
                if all(not re.search(r"\d", field) for field in fields) or any(token in text.lower() for token in ("wave", "lambda", "波长", "折射率")):
                    header = text
                    continue
            raise ValueError(f"第 {line_number} 行：含非数字或缺失值；请检查分隔符及小数点（应使用 .）。") from None
        if not all(map(math.isfinite, row)):
            raise ValueError(f"第 {line_number} 行：含 NaN / 无穷大；请提供有限数值。")
        if rows and len(row) != len(rows[0]):
            raise ValueError(f"第 {line_number} 行有 {len(row)} 列，前面的数据有 {len(rows[0])} 列；请补齐缺失值。")
        rows.append(row)
        numbers.append(line_number)
    if len(rows) < 2:
        raise ValueError("材料表至少需要两行、两个不同波长；常数材料可使用“新增常数 n / k”。")
    if len(rows[0]) < 2:
        raise ValueError("仅识别到 1 列；至少需要波长和 n 两列，请检查分隔符。")
    return MaterialTable(path, hashlib.sha256(raw).hexdigest(), encoding, delimiter, header, rows, numbers)


def columns_from_fields(wave, n, k, column_count):
    result = {}
    for key, text, label in (("wavelength", wave, "波长"), ("n", n, "n"), ("k", k, "k")):
        text = str(text).strip()
        if key == "k" and not text:
            continue
        if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= column_count:
            raise ValueError(f"{label} 列号必须是 1～{column_count} 的整数（缺少 k 时可留空）。")
        result[key] = int(text) - 1
    if len(set(result.values())) != len(result):
        raise ValueError("波长、n、k 必须使用不同的列，不能重复选择。")
    return result


def material_spec_from_table(table, *, wavelength_unit, columns=None, assume_lossless=False):
    """Delegate optical semantics to the existing strict importer."""
    from marksim.materials import import_material_txt
    if wavelength_unit not in ("nm", "um", "µm", "m"):
        raise ValueError("请选择原文件波长单位 nm、um 或 m；程序不会猜测单位。")
    if columns is None:
        if table.column_count not in (2, 3):
            raise ValueError(f"文件有 {table.column_count} 列；请明确选择波长、n 和可选 k 列。")
        columns = {"wavelength": 0, "n": 1, **({"k": 2} if table.column_count == 3 else {})}
    if (set(columns) - {"wavelength", "n", "k"} or not {"wavelength", "n"}.issubset(columns)
            or len(set(columns.values())) != len(columns)
            or any(type(index) is not int or not 0 <= index < table.column_count for index in columns.values())):
        raise ValueError(f"列号必须不同且在文件的 {table.column_count} 列范围内；必须指定波长与 n。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("\n".join("\t".join(repr(value) for value in row) for row in table.rows))
        try:
            spec = import_material_txt(temporary, wavelength_unit=wavelength_unit, columns=columns, assume_lossless=assume_lossless)
        except ValueError as exc:
            detail = str(exc)
            translations = {
                "Require vacuum wavelength>0, n>0 and passive k>=0": "要求真空波长 > 0、n > 0、被动材料 k ≥ 0。",
                "At least two distinct wavelength samples are required": "至少需要两个不同波长；常数材料请填写有效波长范围。",
            }
            if "Conflicting duplicate wavelength " in detail:
                detail = "同一波长存在相互冲突的 n / k：" + detail.removeprefix("Conflicting duplicate wavelength ")
            raise ValueError(translations.get(detail, detail.replace(str(temporary), table.path.name).replace(temporary.name, table.path.name))) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    spec["source"] = str(table.path)
    spec["source_sha256"] = table.sha256
    # If k is supplied, lossless permission is irrelevant and must not suggest
    # the measured absorption was zeroed.
    if spec["k"] is not None:
        spec["assume_lossless"] = False
    spec["import"].update(header=table.header, encoding=table.encoding, delimiter=table.delimiter,
                          original_line_numbers=table.line_numbers, original_numeric_rows=table.rows)
    return spec
