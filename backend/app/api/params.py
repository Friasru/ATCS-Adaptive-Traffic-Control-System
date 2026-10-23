def parse_id(value):
    """Positive integer from a query string value, or None if it is not one."""
    if value is None or not value.isascii() or not value.isdigit():
        return None
    return int(value)
