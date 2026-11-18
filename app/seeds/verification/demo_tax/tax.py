"""Demo code for platform re-run verification. ``taxable_income`` has a deliberate bug: it ignores the allowance."""

ALLOWANCE = 10_000
RATE = 0.2


def taxable_income(income: int) -> int:
    return income


def tax_due(income: int) -> int:
    return round(taxable_income(income) * RATE)


def is_taxable(income: int) -> bool:
    return income > ALLOWANCE


def format_amount(amount: int) -> str:
    return f"NT${amount:,}"
