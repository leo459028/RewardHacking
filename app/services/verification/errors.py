"""Refusals of the verification services; ``app/main.py`` renders them as ``{"error": {"code": ...}}``."""

from __future__ import annotations


class VerificationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
