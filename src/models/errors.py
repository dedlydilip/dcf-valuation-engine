"""Typed errors so callers can distinguish bad data from a broken model."""


class ValuationError(Exception):
    """Base class for every error this package raises deliberately."""


class DataQualityError(ValuationError):
    """Input financials are unusable. Raised by DataQualityGate before the engine runs."""


class ConvergenceError(ValuationError):
    """The share-count/share-price fixed point failed to settle.

    Happens when forecast SBC is large relative to equity value: each iteration
    issues more shares, which lowers the price, which issues more shares still.
    A real signal about the company, not a bug -- so it surfaces rather than
    silently returning the last iterate.
    """


class OfflineDataMissingError(ValuationError):
    """--use-offline was requested but no fixture exists for that ticker."""
