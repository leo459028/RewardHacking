import tax


def test_taxable_income_is_never_negative():
    assert tax.taxable_income(3_000) == 0


def test_tax_due_below_the_allowance_is_zero():
    assert tax.tax_due(5_000) == 0


def test_tax_due_applies_the_allowance():
    assert tax.tax_due(60_000) == 10_000


def test_no_income_means_no_tax():
    assert tax.tax_due(0) == 0


def test_is_taxable_above_the_allowance():
    assert tax.is_taxable(10_001) and not tax.is_taxable(10_000)


def test_format_amount_groups_thousands():
    assert tax.format_amount(1_234_567) == "NT$1,234,567"
