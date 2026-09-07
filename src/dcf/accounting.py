"""Shared operating cash-tax calculation for projections and simulation."""

import numpy as np


def cash_tax_schedule(ebit, tax_rate, starting_nol=0.0):
    """Unrestricted NOL carryforward, without expiry or annual utilization limits.

    Inputs have time on the final axis. This is a disclosed simplification; jurisdiction
    limits require an analyst-supplied schedule, rather than an invented tax benefit.
    """
    ebit = np.asarray(ebit, dtype=float)
    if not np.isfinite(ebit).all():
        raise ValueError("EBIT must be finite")
    opening = np.zeros_like(ebit)
    used = np.zeros_like(ebit)
    closing = np.zeros_like(ebit)
    taxes = np.zeros_like(ebit)
    nol = np.full(ebit.shape[:-1], starting_nol, dtype=float)
    for i in range(ebit.shape[-1]):
        opening[..., i] = nol
        used[..., i] = np.minimum(nol, np.maximum(ebit[..., i], 0))
        taxes[..., i] = (np.maximum(ebit[..., i], 0) - used[..., i]) * tax_rate
        nol = nol - used[..., i] + np.maximum(-ebit[..., i], 0)
        closing[..., i] = nol
    return taxes, opening, used, closing
