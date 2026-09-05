from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class CalibrationSnapshot(BaseModel):
    """The CIQ identity/version needed for safe reconciliation.

    ScrapeX deliberately keeps inactive rows too: ADAS Map may prove that a
    formerly-not-required calibration is required now, in which case the
    existing CIQ row must be reactivated instead of duplicated.
    """

    id: str
    calibration_type: str
    determination: str
    method: str | None = None
    version: int = 1


class VehicleSpec(BaseModel):
    ro_id: str | None = None
    ro_number: str | None = None
    vin: str | None = Field(default=None, min_length=4, max_length=32)
    shop: str | None = Field(default=None, max_length=180)
    year: int | None = None
    make: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=160)
    trim: str | None = Field(default=None, max_length=160)
    engine: str | None = Field(default=None, max_length=160)
    configuration: dict[str, Any] | str | None = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)
    existing_calibrations: list[CalibrationSnapshot] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return " ".join(
            str(v).strip()
            for v in (self.year, self.make, self.model, self.trim)
            if v not in (None, "")
        )

class BatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    vehicles: list[VehicleSpec] = Field(min_length=1, max_length=500)


class CIQBatchCreate(BaseModel):
    name: str = Field(default="Calibration IQ weekly queue", min_length=1, max_length=180)
    phases: list[str] = Field(min_length=1, max_length=10)
    shop: str | None = None
    source_scope: Literal["active", "all", "terminal"] = "active"

class CIQPreviewRequest(BaseModel):
    phases: list[str] = Field(min_length=1, max_length=10)
    shop: str | None = None
    source_scope: Literal["active", "all", "terminal"] = "active"


class AdasMapLookupResult(BaseModel):
    success: bool
    ro_number: str
    shop: str | None = None
    vin: str | None = None
    vehicle_label: str | None = None
    inspection_id: str | None = None
    source_url: str | None = None
    calibrations: list[str] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    requirements_proven: bool = False
    explicit_no_calibration: bool = False
    details_url: str | None = None
    alldata_links: list[str] = Field(default_factory=list)
    report_links: list[str] = Field(default_factory=list)
    reason: str | None = None
