from quant_research.errors import ValidationError


def build_greeting(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("greeting name must not be empty")
    return f"Hello, {cleaned}!"
