"""Text that a model, a tool or another server wrote, made fit to show."""


def one_line(text: str) -> str:
    """The text on one line, with anything that is not printable shown as `?`.

    Such text can hold terminal control codes, and characters that reorder
    what is shown after them (right-to-left overrides). Neither is printable.
    """
    return "".join(char if char.isprintable() else "?" for char in " ".join(text.split()))
