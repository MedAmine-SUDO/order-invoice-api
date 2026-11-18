"""Amounts in French words, for the "Arrêtée la présente facture à la somme de..." line."""

from decimal import Decimal

UNITS = [
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix",
    "onze", "douze", "treize", "quatorze", "quinze", "seize",
]  # fmt: skip
TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _below_100(n: int, final: bool) -> str:
    if n <= 16:
        return UNITS[n]
    if n < 20:
        return "dix-" + UNITS[n - 10]
    tens, unit = divmod(n, 10)
    if tens in (7, 9):  # 70-79 and 90-99 count on from 60 and 80
        base = "soixante" if tens == 7 else "quatre-vingt"
        rest = n - (60 if tens == 7 else 80)
        joiner = " et " if rest == 11 and tens == 7 else "-"
        return base + joiner + _below_100(rest, final)
    if tens == 8:
        return "quatre-vingt" + (f"-{UNITS[unit]}" if unit else ("s" if final else ""))
    if unit == 0:
        return TENS[tens]
    return TENS[tens] + (" et un" if unit == 1 else f"-{UNITS[unit]}")


def _below_1000(n: int, final: bool) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        word = "cent" if hundreds == 1 else f"{UNITS[hundreds]} cent"
        parts.append(word + ("s" if hundreds > 1 and rest == 0 and final else ""))
    if rest or not hundreds:
        parts.append(_below_100(rest, final))
    return " ".join(parts)


def number_to_words(n: int) -> str:
    if n == 0:
        return UNITS[0]
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1000)
    parts = []
    if millions:
        parts.append(_below_1000(millions, True) + (" millions" if millions > 1 else " million"))
    if thousands:
        parts.append("mille" if thousands == 1 else _below_1000(thousands, False) + " mille")
    if units:
        parts.append(_below_1000(units, True))
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    """Decimal("1250.500") -> "mille deux cent cinquante dinars et cinq cents millimes"."""
    millimes_total = int((amount * 1000).to_integral_value())
    dinars, millimes = divmod(millimes_total, 1000)
    text = number_to_words(dinars) + (" dinars" if dinars > 1 else " dinar")
    if millimes:
        text += " et " + number_to_words(millimes) + (" millimes" if millimes > 1 else " millime")
    return text
