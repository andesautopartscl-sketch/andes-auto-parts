import re


def clean_rut(raw: str | None) -> str:
    compact = re.sub(r"[^0-9kK]", "", (raw or ""))
    if not compact:
        return ""
    dv = compact[-1].upper()
    body = re.sub(r"\D", "", compact[:-1])
    if not body:
        return ""
    return f"{body}{dv}"


def format_rut(raw: str | None) -> str:
    normalized = clean_rut(raw)
    if len(normalized) < 2:
        return ""
    body = normalized[:-1]
    dv = normalized[-1]
    chunks = []
    while body:
        chunks.append(body[-3:])
        body = body[:-3]
    return f"{'.'.join(reversed(chunks))}-{dv}"


def _compute_dv(body: str) -> str:
    factors = [2, 3, 4, 5, 6, 7]
    total = 0
    for idx, digit in enumerate(reversed(body)):
        total += int(digit) * factors[idx % len(factors)]
    remainder = 11 - (total % 11)
    if remainder == 11:
        return "0"
    if remainder == 10:
        return "K"
    return str(remainder)


def is_valid_rut(raw: str | None) -> bool:
    normalized = clean_rut(raw)
    if len(normalized) < 8:
        return False
    body = normalized[:-1]
    dv = normalized[-1]
    if not body.isdigit():
        return False
    return _compute_dv(body) == dv
