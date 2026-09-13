"""Explicit, display-only credential masking; never serialize private objects."""


def mask_secret(value: object) -> str:
    """Expose five leading and three trailing characters of sufficiently long values.

    Short/empty values and structured or multiline content stay fully hidden.
    At least three original characters must remain concealed.
    """
    if not isinstance(value, str) or len(value) <= 10:
        return "***"
    if any(character.isspace() for character in value) or "…" in value:
        return "***"
    return f"{value[:5]}…{value[-3:]}"
