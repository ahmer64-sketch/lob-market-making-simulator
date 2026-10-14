"""Calibration of Avellaneda-Stoikov inputs from simulated (or real) data."""
import numpy as np


def mid_variance_rate(mids, dt=1.0, horizon=60.0):
    """Variance of mid changes per second, measured over `horizon` seconds (long enough to be
    diffusive: shorter horizons are inflated by bid-ask bounce, see the Phase 2 signature plot).
    `mids` is a list of arrays sampled every `dt` seconds (one per simulated day)."""
    step = int(round(horizon / dt))
    d = np.concatenate([m[step::step] - m[:-step:step] for m in mids])
    return float(d.var() / horizon)


def fit_intensity(distances, rates):
    """OLS fit of ln(rate) = ln(A) - k * distance. Returns A, k and R^2 of the log fit."""
    x, y = np.asarray(distances, float), np.log(np.asarray(rates, float))
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    r2 = 1 - resid.var() / y.var()
    return float(np.exp(intercept)), float(-slope), float(r2)
