"""Small date parser for administrator month commands."""

from datetime import date


def parse_month(value: str) -> date:
    normalized = value.strip().upper()
    if len(normalized) == 7 and normalized[4] == "-":
        return date.fromisoformat(normalized + "-01")
    months = (
        "JANEIRO",
        "FEVEREIRO",
        "MARÇO",
        "ABRIL",
        "MAIO",
        "JUNHO",
        "JULHO",
        "AGOSTO",
        "SETEMBRO",
        "OUTUBRO",
        "NOVEMBRO",
        "DEZEMBRO",
    )
    parts = normalized.split()
    return date(
        int(parts[1]) if len(parts) > 1 else date.today().year,
        months.index(parts[0]) + 1,
        1,
    )
