class TransferError(ValueError):
    """Stable diagnostics without SQL parameters, credentials or personal data."""

    def __init__(self, code: str, *, table: str | None = None, column: str | None = None):
        super().__init__(code)
        self.code = code
        self.table = table
        self.column = column

    def report(self) -> dict:
        return {"code": self.code, "table": self.table, "column": self.column}
