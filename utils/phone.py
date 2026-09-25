"""Phone numbers: one canonical form, whatever way someone typed them.

A technician is identified by their WhatsApp number, and the same person gets
typed in as "0300 1234567", "+92 300 1234567" and "923001234567". Numbers are
stored in international form (+923001234567) together with the country they
belong to: the same number, any spelling, is then the same person, and a
WhatsApp link can be built from it.

The country matters more than it looks. "6475550123" is a Toronto mobile
number, but read as Pakistani it's a valid landline in Dera Ghazi Khan — and a
WhatsApp message would go to a stranger. So only mobile numbers are accepted:
typed against the wrong country, a number like that is refused, not
misfiled.
"""
import re

import phonenumbers

# The picker. Pakistan first (almost everyone), then where HQ's people and
# partners are. Any other country works by typing the number with its +code.
COUNTRIES = (
    ("PK", "Pakistan"),
    ("AE", "United Arab Emirates"),
    ("SA", "Saudi Arabia"),
    ("QA", "Qatar"),
    ("OM", "Oman"),
    ("KW", "Kuwait"),
    ("BH", "Bahrain"),
    ("AF", "Afghanistan"),
    ("CN", "China"),
    ("TR", "Türkiye"),
    ("GB", "United Kingdom"),
    ("US", "United States"),
    ("CA", "Canada"),
    ("AU", "Australia"),
    ("DE", "Germany"),
    ("MY", "Malaysia"),
)
COUNTRY_NAMES = dict(COUNTRIES)

_WHATSAPP_TYPES = (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


def normalize(raw: str, country: str = "PK") -> tuple[str, str, str]:
    """(e164, key, country) for a WhatsApp number, e.g. ('+923001234567',
    '923001234567', 'PK'). Raises ValueError with a message for a person.
    A number typed with its own +code wins over the picked country."""
    text = (raw or "").strip()
    if not any(ch.isdigit() for ch in text):
        raise ValueError("That phone number has no digits in it.")
    if text.startswith("00"):
        text = "+" + text[2:]
    region = (country or "PK").upper()
    where = COUNTRY_NAMES.get(region, region)
    try:
        number = phonenumbers.parse(text, None if text.startswith("+") else region)
    except phonenumbers.NumberParseException:
        raise ValueError("That doesn't look like a phone number.") from None
    if not phonenumbers.is_valid_number(number):
        raise ValueError(f"That isn't a valid {where} number. Check it, or type it with its +country code.")
    if phonenumbers.number_type(number) not in _WHATSAPP_TYPES:
        raise ValueError(f"That's a {where} landline, not a mobile number — pick the right country, "
                         "or type it with its +country code.")
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    return e164, e164[1:], phonenumbers.region_code_for_number(number) or region


def phone_key(raw: str) -> str:
    """The key for a number typed without a country, read as Pakistani — or
    just its digits if it isn't a Pakistani mobile number."""
    try:
        return normalize(raw, "PK")[1]
    except ValueError:
        return re.sub(r"\D", "", raw or "")


def display(e164: str, country: str | None = None) -> str:
    """'+92 300 1234567 · Pakistan' — how a number is shown everywhere."""
    try:
        number = phonenumbers.parse(e164 or "", None)
    except phonenumbers.NumberParseException:
        return e164 or "—"
    text = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    region = country or phonenumbers.region_code_for_number(number)
    name = COUNTRY_NAMES.get(region, region)
    return f"{text} · {name}" if name else text


def wa_digits(e164: str) -> str:
    """What wa.me wants: the international number, digits only."""
    return re.sub(r"\D", "", e164 or "")
