"""Servis katmanı hataları."""


class ValidationError(Exception):
    """Form/iş kuralı hatası. errors: alan adı → mesaj ('_' = genel)."""

    def __init__(self, errors: dict[str, str], extra: dict | None = None):
        super().__init__("; ".join(errors.values()))
        self.errors = errors
        self.extra = extra or {}


class NotFound(Exception):
    pass
