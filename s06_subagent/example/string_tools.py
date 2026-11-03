import re

def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    - Convert to lowercase
    - Replace spaces with hyphens
    - Remove non-alphanumeric characters (except hyphens)
    - Strip leading/trailing hyphens
    """
    text = text.lower()
    text = text.replace(" ", "-")
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = text.strip("-")
    return text
