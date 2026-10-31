"""Errors of the vault core. The ``code`` is stable and machine-readable; the message never contains a passphrase,
a key, a name or any other secret, and a wrong passphrase is deliberately not told apart from a damaged header."""


class VaultError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
