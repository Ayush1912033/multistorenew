from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from openpyxl import load_workbook

REQUIRED = ("StoreCode", "BO_IP", "CounterCount")
OPTIONAL = ("CounterIPs",)


def _parse_counter_ips(raw_value, line, count):
    """Parse optional custom counter IPs separated by commas, semicolons, or newlines."""
    if raw_value is None or not str(raw_value).strip():
        return []

    raw_text = str(raw_value).strip()
    parts = [part.strip() for part in re.split(r"[,;\n\r]+", raw_text) if part.strip()]
    parsed = []
    seen = set()

    for raw_ip in parts:
        try:
            ip = ipaddress.ip_address(raw_ip)
            if ip.version != 4:
                raise ValueError()
        except ValueError:
            raise ValueError(f"Row {line}: invalid custom counter IPv4 address {raw_ip!r}.")

        ip_text = str(ip)
        if ip_text in seen:
            raise ValueError(f"Row {line}: duplicate custom counter IP {ip_text}.")
        seen.add(ip_text)
        parsed.append(ip_text)

    if len(parsed) > count:
        raise ValueError(
            f"Row {line}: CounterIPs contains {len(parsed)} IPs, but CounterCount is {count}. "
            "Provide no more custom IPs than CounterCount."
        )
    return parsed


def read_stores(path: str | Path):
    workbook = load_workbook(Path(path), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError("Excel file is empty.")

        names = [str(v).strip() if v is not None else "" for v in header]
        missing = [name for name in REQUIRED if name not in names]
        if missing:
            raise ValueError(
                "Excel must contain columns: StoreCode, BO_IP, CounterCount. Missing: "
                + ", ".join(missing)
            )

        indexes = {name: names.index(name) for name in REQUIRED}
        custom_index = names.index("CounterIPs") if "CounterIPs" in names else None
        output, seen = [], set()

        for line, values in enumerate(rows, start=2):
            if not values or all(v is None or str(v).strip() == "" for v in values):
                continue

            code = str(values[indexes["StoreCode"]]).strip()
            raw_ip = str(values[indexes["BO_IP"]]).strip()
            raw_count = values[indexes["CounterCount"]]
            raw_custom = values[custom_index] if custom_index is not None and custom_index < len(values) else None

            if not code or code.lower() == "none":
                raise ValueError(f"Row {line}: StoreCode is required.")
            if code in seen:
                raise ValueError(f"Row {line}: duplicate StoreCode {code}.")
            seen.add(code)

            try:
                ip = ipaddress.ip_address(raw_ip)
                if ip.version != 4:
                    raise ValueError()
            except ValueError:
                raise ValueError(f"Row {line}: invalid IPv4 BO_IP {raw_ip}.")

            try:
                count = int(raw_count)
                if str(raw_count).strip() != str(count) and not isinstance(raw_count, int):
                    # Reject fractional Excel values such as 3.5 instead of silently truncating.
                    if float(raw_count) != count:
                        raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(f"Row {line}: CounterCount must be a whole number.")

            if count < 1 or 112 + count - 1 > 254:
                raise ValueError(f"Row {line}: CounterCount must be 1 to {254 - 112 + 1}.")

            custom_ips = _parse_counter_ips(raw_custom, line, count)
            output.append({
                "StoreCode": code,
                "BO_IP": str(ip),
                "CounterCount": count,
                "CounterIPs": custom_ips,
            })

        if not output:
            raise ValueError("No store rows found in Excel.")
        return output
    finally:
        workbook.close()


import re
