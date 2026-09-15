"""ScrapeX package metadata."""

__version__ = "0.6.0"

# Install the mechanical ALLDATA vehicle-identity hardening at package import
# time so every production/test provider instance uses the same fail-closed
# VIN/YMM selection and proof contract.
from . import navigator_vehicle_identity as _navigator_vehicle_identity  # noqa: E402,F401
# Preserve the historical target-signal fallback only for URL-less legacy/test
# adapters. Real Playwright pages retain the strict route-aware identity proof.
from . import navigator_vehicle_signal_compat as _navigator_vehicle_signal_compat  # noqa: E402,F401
# Older contract-v3 ADAS Map rows can carry fully proven report/calibration
# evidence while CIQ still lacks the VIN. Keep normal acquisition strict, but
# let the explicit repair endpoint recover that already-proven identity.
from . import adas_map_vin_backfill as _adas_map_vin_backfill  # noqa: E402,F401
