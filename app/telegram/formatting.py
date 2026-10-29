from html import escape


def escaped(value, max_chars=3500):
    """Bound HTML without cutting an entity or a Unicode surrogate pair."""
    result = []
    used = 0
    for character in str(value):
        encoded = escape(character)
        width = max(len(encoded), len(character.encode("utf-16-le")) // 2)
        if used + width > max_chars:
            break
        result.append(encoded)
        used += width
    return "".join(result)
