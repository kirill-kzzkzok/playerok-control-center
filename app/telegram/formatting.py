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


def html_pages(value, max_chars=2800):
    """Escape complete text into pages, without discarding any part of terms."""
    if max_chars < 6:
        raise ValueError("Недостаточный размер страницы")
    pages, current, used = [], [], 0
    for character in str(value):
        encoded = escape(character)
        width = max(len(encoded), len(character.encode("utf-16-le")) // 2)
        if used + width > max_chars:
            pages.append("".join(current))
            current, used = [], 0
        current.append(encoded)
        used += width
    if current:
        pages.append("".join(current))
    return pages or [""]
