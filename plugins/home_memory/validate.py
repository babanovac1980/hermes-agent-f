import re

# Forbidden characters for element/category/status names
INVALID_CHARS = re.compile(r'[\$\*\[\{\]\}\|\\<>\?/";:\t]')

# Forbidden characters for connection names (subset — connections allow more)
INVALID_CHARS_CONNECTION = re.compile(r'[\*\|<>?"\t]')

# Length limits (from C# source)
MAX_NAME = 100
MAX_NAME_CONNECTION = 150
MAX_SHORT_NAME = 50
MAX_PURPOSE = 200
MAX_NOTE = 200
MAX_POSITION = 200
MAX_DESCRIPTION = 4000
MAX_USER_MANUAL = 4000
MAX_ROUTE = 1000


def check_length(value: str | None, field: str, max_len: int) -> str | None:
    if value is not None and len(value) > max_len:
        return f"Error: '{field}' exceeds maximum length of {max_len} characters (got {len(value)})."
    return None


def normalize_clear(value: str | None) -> str | None:
    """Case-insensitive 'clear' → 'CLEAR'."""
    if value is not None and value.strip().lower() == "clear":
        return "CLEAR"
    return value


def normalize_singleline(value: str | None) -> str | None:
    """Collapse all newline variants to a space (Smartconstruct doesn't support line breaks in single-line fields)."""
    if value is None:
        return None
    return value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def normalize_multiline(value: str | None) -> str | None:
    """Normalise newlines to \\r\\n for Smartconstruct round-trip compatibility."""
    if value is None:
        return None
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
