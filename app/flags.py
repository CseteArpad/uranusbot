from typing import Optional


def insufficient_rise(
    peak_since_entry: Optional[float],
    enough_rise_level: Optional[float],
) -> bool:
    """
    Rule 2: 'nem volt elég emelkedés' flag.
    True, ha volt peak, de az nem érte el az elég emelkedés szintjét.
    """
    if peak_since_entry is None:
        return False
    if enough_rise_level is None:
        return False
    return peak_since_entry < enough_rise_level


def allow_profitless_buy(last_exit_reason: Optional[str]) -> bool:
    """
    Rule 5: profit nélküli visszavétel jogosultság.
    Itt egyszerű (bővíthető) alap: csak bizonyos exit ok után engedjük.
    """
    if not last_exit_reason:
        return False

    return last_exit_reason in {
        "SELL_STD",
        "SELL_PROFITLESS",
    }
