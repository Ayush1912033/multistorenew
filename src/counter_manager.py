from __future__ import annotations

import ipaddress
from dataclasses import dataclass


@dataclass(frozen=True)
class Counter:
    number: int
    ip: str
    source: str = "generated"


def generate_counters(bo_ip: str, count: int, start: int) -> list[Counter]:
    if count < 1:
        raise ValueError("Number of counters must be at least 1.")

    parsed = ipaddress.ip_address(bo_ip)
    if parsed.version != 4:
        raise ValueError("BO IP must be IPv4.")

    parts = str(parsed).split(".")
    result = []

    for number in range(1, count + 1):
        last = start + number - 1
        if not 1 <= last <= 254:
            raise ValueError(f"Invalid generated counter last octet: {last}")

        ip = ".".join(parts[:3] + [str(last)])
        result.append(Counter(number, ip))

    return result


def apply_custom_ips(counters, custom_text: str):
    overrides = {}

    for raw in custom_text.splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            raise ValueError(
                f"Invalid custom IP line '{line}'. Use counter_number=IP."
            )

        number_text, ip_text = line.split("=", 1)

        try:
            number = int(number_text.strip())
        except ValueError:
            raise ValueError(f"Invalid counter number: {number_text}")

        ip_text = ip_text.strip()

        try:
            parsed = ipaddress.ip_address(ip_text)
        except ValueError:
            raise ValueError(f"Invalid IP address: {ip_text}")

        if parsed.version != 4:
            raise ValueError("Only IPv4 counter addresses are supported.")

        overrides[number] = ip_text

    return [
        Counter(c.number, overrides[c.number], "custom")
        if c.number in overrides
        else c
        for c in counters
    ]
