from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def font(size, bold=False):
    candidates = [
        Path("C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def profile_card(profile, avatar=None):
    image = Image.new("RGB", (1100, 480), "#111018")
    draw = ImageDraw.Draw(image)
    for y in range(480):
        draw.line((0, y, 1100, y), fill=(20 + int(18 * y / 480), 17, 33 + int(35 * y / 480)))
    draw.rounded_rectangle((30, 30, 1070, 450), radius=35, outline="#65519a", width=2)
    draw.text((70, 65), "PLAYEROK CONTROL CENTER", font=font(23, True), fill="#bc9aff")
    draw.ellipse((65, 150, 285, 370), fill="#44335e")
    avatar_loaded = False
    if avatar:
        try:
            with Image.open(BytesIO(avatar)) as source:
                if source.width * source.height <= 25_000_000:
                    cropped = ImageOps.fit(source.convert("RGB"), (220, 220))
                    mask = Image.new("L", (220, 220))
                    ImageDraw.Draw(mask).ellipse((0, 0, 219, 219), fill=255)
                    image.paste(cropped, (65, 150), mask)
                    avatar_loaded = True
        except (OSError, Image.DecompressionBombError):
            pass  # Visible fallback initials, never fake avatar data.
    if not avatar_loaded:
        draw.text((145, 210), (profile.get("username") or "?")[:1].upper(), font=font(70), fill="white")
    draw.text((330, 155), str(profile.get("username") or "Без имени")[:28], font=font(44, True), fill="white")
    rating = profile.get("rating")
    draw.text(
        (333, 225),
        "Рейтинг: " + (str(rating) if rating is not None else "нет данных"),
        font=font(30),
        fill="#d6bfff",
    )
    reviews = profile.get("reviews_count")
    draw.text(
        (333, 280),
        "Отзывы: " + (str(reviews) if reviews is not None else "нет данных"),
        font=font(26),
        fill="#bdb7ce",
    )
    draw.text((333, 355), "Данные профиля Playerok", font=font(20), fill="#a096b9")
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()
