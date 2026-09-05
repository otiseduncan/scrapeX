import copy

import pytest

from scrapex.ciq import CIQClient, CIQReconciliationError, calibration_key
from scrapex.models import VehicleSpec


class FakeCIQ(CIQClient):
    def __init__(self, snapshot, *, verified=True):
        super().__init__(object())
        self.snapshot = copy.deepcopy(snapshot)
        self.actions = []
        self.verified = verified

    async def operator_capabilities(self):
        return {
            "policy": {"routine": [
                "update_ro", "add_calibration", "update_calibration",
                "update_research", "import_document", "link_document",
            ]},
            "batch": {"authoritative_verification": True},
        }

    async def _snapshot(self, ro_id):
        return copy.deepcopy(self.snapshot)

    async def _post_actions(self, actions):
        self.actions.extend(copy.deepcopy(actions))
        receipts = []
        for number, action in enumerate(actions, 1):
            operation = action["operation"]
            resource_id = action.get("target_id")
            if operation == "update_ro":
                self.snapshot["repair_order"].update(action["arguments"])
                self.snapshot["repair_order"]["version"] += 1
                resource_id = self.snapshot["repair_order"]["id"]
            elif operation == "update_research":
                self.snapshot["research"].update(action["arguments"])
                self.snapshot["research"]["version"] += 1
                resource_id = self.snapshot["research"]["id"]
            elif operation == "update_calibration":
                row = next(
                    value for value in self.snapshot["calibrations"]
                    if value["id"] == action["target_id"]
                )
                row.update(action["arguments"])
                row["version"] += 1
            elif operation == "add_calibration":
                resource_id = f"cal-{len(self.snapshot['calibrations']) + 1}"
                self.snapshot["calibrations"].append(
                    {"id": resource_id, "version": 1, **action["arguments"]}
                )
            receipts.append({
                "mutation_id": f"mutation-{number}",
                "idempotency_key": action["idempotency_key"],
                "operation": operation,
                "status": "completed",
                "success": True,
                "indeterminate": False,
                "resource_id": resource_id,
                "verification": {"verified": self.verified},
            })
        if not self.verified and receipts:
            raise CIQReconciliationError(
                "Calibration IQ did not return a verified completed receipt for every action.",
                result={"receipts": receipts},
            )
        return {"success": True, "receipts": receipts}


def snapshot():
    return {
        "repair_order": {
            "id": "ro-1", "version": 4, "vin": None, "year": 2016,
            "make": "Toyota", "model": "Sienna", "trim": None,
            "vehicle_configuration": {},
        },
        "vehicle": {
            "vin": None, "year": 2016, "make": "Toyota", "model": "Sienna",
            "trim": None, "configuration": {},
        },
        "calibrations": [
            {
                "id": "cal-bsm", "calibration_type": "BSM calibration",
                "determination": "REQUIRED", "method": "UNKNOWN", "version": 2,
            },
            {
                "id": "cal-sas", "calibration_type": "Steering Angle Sensor",
                "determination": "NOT_REQUIRED", "method": "UNKNOWN", "version": 3,
            },
        ],
        "documents": [],
    }


@pytest.mark.asyncio
async def test_reconciliation_keeps_alias_and_reactivates_without_duplicate():
    client = FakeCIQ(snapshot())
    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Blind Spot Sensors", "Steering Angle Sensor Initialization"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
    )

    assert [row["operation"] for row in result["kept"]] == ["keep"]
    assert [row["operation"] for row in result["changed"]] == ["update_calibration"]
    assert client.actions[0]["target_id"] == "cal-sas"
    assert client.actions[0]["expected_version"] == 3
    assert len(client.snapshot["calibrations"]) == 2
    assert client.snapshot["calibrations"][1]["determination"] == "REQUIRED"


@pytest.mark.asyncio
async def test_reconciliation_updates_only_observed_vehicle_fields_with_ro_version():
    client = FakeCIQ(snapshot())
    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Seat Belt"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
        vehicle={
            "vin": "TESTCAR0000000001",
            "year": 2016,
            "make": "Toyota",
            "model": "Sienna L FWD w/7-Passenger Seating",
            "configuration": {"drive": "FWD", "seats": 7},
        },
    )
    update = client.actions[0]
    assert update["operation"] == "update_ro"
    assert update["expected_version"] == 4
    assert update["arguments"] == {
        "vin": "TESTCAR0000000001",
        "model": "Sienna L FWD w/7-Passenger Seating",
        "vehicle_configuration": {"drive": "FWD", "seats": 7},
    }
    assert result["vehicle_changed"]["mutation_id"] == "mutation-1"


@pytest.mark.asyncio
async def test_live_map_configuration_is_merged_without_clobbering_ro_configuration():
    current = snapshot()
    current["repair_order"]["vehicle_configuration"] = {"existing_key": "preserved"}
    current["vehicle"]["configuration"] = {"existing_key": "preserved"}
    client = FakeCIQ(current)

    await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Blind Spot Sensors"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
        vehicle={"configuration": "L FWD w/7-Passenger Seating"},
    )

    assert client.actions == [
        {
            "idempotency_key": client.actions[0]["idempotency_key"],
            "correlation_id": "scrapex-batch-1-item-1",
            "operation": "update_ro",
            "repair_order_id": "ro-1",
            "expected_version": 4,
            "arguments": {
                "vehicle_configuration": {
                    "existing_key": "preserved",
                    "adas_map_configuration": "L FWD w/7-Passenger Seating",
                }
            },
        }
    ]


@pytest.mark.asyncio
async def test_truncated_ciq_model_is_replaced_by_full_observed_portal_field():
    current = snapshot()
    current["repair_order"]["model"] = "Sienna L FWD..."
    current["vehicle"]["model"] = "Sienna L FWD..."
    client = FakeCIQ(current)

    await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Blind Spot Sensors"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
        vehicle={
            "year": 2016,
            "make": "Toyota",
            "model": "Sienna L FWD w/7-Passenger Seating",
            "configuration": None,
            "model_configuration": "Sienna L FWD w/7-Passenger Seating",
        },
    )

    update = client.actions[0]
    assert update["operation"] == "update_ro"
    assert update["arguments"]["model"] == "Sienna L FWD w/7-Passenger Seating"
    assert update["arguments"]["vehicle_configuration"] == {
        "adas_map_model_configuration": "Sienna L FWD w/7-Passenger Seating"
    }


@pytest.mark.asyncio
async def test_reconciliation_rejects_ui_noise_before_any_mutation():
    client = FakeCIQ(snapshot())
    with pytest.raises(CIQReconciliationError, match="not safe"):
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=["Create Calibration"],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
        )
    assert client.actions == []


@pytest.mark.asyncio
async def test_reconciliation_rejects_unverified_receipt():
    client = FakeCIQ(snapshot(), verified=False)
    with pytest.raises(CIQReconciliationError, match="verified completed receipt"):
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=["Steering Angle Sensor Initialization"],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
        )


@pytest.mark.asyncio
async def test_explicit_no_calibration_conflict_requires_manual_review():
    client = FakeCIQ(snapshot())
    with pytest.raises(CIQReconciliationError, match="manual review") as captured:
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=[],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
            explicit_no_calibration=True,
        )
    assert captured.value.result["active_calibration_conflicts"]
    assert client.actions == []


@pytest.mark.asyncio
async def test_explicit_no_calibration_can_verify_when_ciq_has_no_active_rows():
    current = snapshot()
    for row in current["calibrations"]:
        row["determination"] = "NOT_REQUIRED"
    client = FakeCIQ(current)
    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=[],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
        explicit_no_calibration=True,
    )
    assert result["verified"] is True
    assert result["explicit_no_calibration"] is True
    assert client.actions == []


@pytest.mark.asyncio
async def test_completed_research_is_verified_reopened_before_new_requirement():
    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": 7,
    }
    client = FakeCIQ(current)

    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Seat Belt"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
    )

    assert [action["operation"] for action in client.actions] == [
        "update_research",
        "add_calibration",
    ]
    reopen, added = client.actions
    assert reopen["expected_version"] == 7
    assert reopen["arguments"]["state"] == "research_in_progress"
    assert "inspection 9900001" in reopen["arguments"]["reason"]
    assert client.snapshot["research"]["state"] == "research_in_progress"
    assert client.snapshot["research"]["version"] == 8
    assert result["research_reopened"]["from_version"] == 7
    assert result["research_reopened"]["to_version"] == 8
    assert result["receipt_count"] == 2

    legacy_identity = {
        "operation": "add_calibration",
        "repair_order_id": "ro-1",
        "arguments": added["arguments"],
        "inspection_id": "9900001",
    }
    assert added["idempotency_key"] != client._idempotency(legacy_identity)


@pytest.mark.asyncio
async def test_completed_research_is_not_reopened_when_requirements_are_kept():
    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": 7,
    }
    client = FakeCIQ(current)

    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Blind Spot Sensors"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
    )

    assert client.actions == []
    assert result["research_reopened"] is None
    assert result["receipt_count"] == 0
    assert client.snapshot["research"]["state"] == "research_complete"


@pytest.mark.asyncio
async def test_completed_research_is_reopened_before_reactivation():
    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": 7,
    }
    client = FakeCIQ(current)

    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Steering Angle Sensor Initialization"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
    )

    assert [action["operation"] for action in client.actions] == [
        "update_research",
        "update_calibration",
    ]
    assert client.actions[1]["target_id"] == "cal-sas"
    assert client.actions[1]["expected_version"] == 3
    assert result["research_reopened"]["to_state"] == "research_in_progress"
    assert result["changed"][0]["operation"] == "update_calibration"


@pytest.mark.asyncio
async def test_reopen_response_loss_resumes_from_authoritative_research_state():
    class LoseFirstReopenResponse(FakeCIQ):
        lost = False

        async def _post_actions(self, actions):
            result = await super()._post_actions(actions)
            if actions and actions[0]["operation"] == "update_research" and not self.lost:
                self.lost = True
                raise TimeoutError("response lost after committed reopen")
            return result

    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": 7,
    }
    client = LoseFirstReopenResponse(current)

    with pytest.raises(TimeoutError, match="response lost"):
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=["Seat Belt"],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
        )

    assert [action["operation"] for action in client.actions] == ["update_research"]
    assert client.snapshot["research"]["state"] == "research_in_progress"
    assert client.snapshot["research"]["version"] == 8

    result = await client.reconcile_requirements(
        repair_order_id="ro-1",
        requirements=["Seat Belt"],
        batch_id="batch-1",
        item_id="item-1",
        inspection_id="9900001",
    )

    assert [action["operation"] for action in client.actions] == [
        "update_research",
        "add_calibration",
    ]
    assert result["research_reopened"] is None
    assert result["receipt_count"] == 1


@pytest.mark.asyncio
async def test_reopen_requires_authoritative_in_progress_reread_before_add():
    class StaleResearchReread(FakeCIQ):
        async def _snapshot(self, ro_id):
            value = await super()._snapshot(ro_id)
            if self.actions and self.actions[-1]["operation"] == "update_research":
                value["research"]["state"] = "research_complete"
                value["research"]["version"] = 7
            return value

    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": 7,
    }
    client = StaleResearchReread(current)

    with pytest.raises(CIQReconciliationError, match="did not verify reopened research"):
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=["Seat Belt"],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
        )

    assert [action["operation"] for action in client.actions] == ["update_research"]


@pytest.mark.asyncio
async def test_calibration_mutation_fails_closed_on_malformed_research_snapshot():
    current = snapshot()
    current["research"] = {
        "id": "research-1",
        "state": "research_complete",
        "version": None,
    }
    client = FakeCIQ(current)

    with pytest.raises(CIQReconciliationError, match="not authoritative enough"):
        await client.reconcile_requirements(
            repair_order_id="ro-1",
            requirements=["Seat Belt"],
            batch_id="batch-1",
            item_id="item-1",
            inspection_id="9900001",
        )
    assert client.actions == []


def test_alias_equivalence_is_narrow_and_does_not_merge_front_and_rear_camera():
    assert calibration_key("BSM calibration") == calibration_key("Blind Spot Sensors")
    assert calibration_key("Front View Camera") != calibration_key("Rearview Camera")


class QueueCIQ(CIQClient):
    def __init__(self, rows, snapshots):
        super().__init__(object())
        self.rows = rows
        self.snapshots = snapshots
        self.queries = []

    async def _all_rows(self, phase, shop, source_scope="active", query=None):
        self.queries.append((phase, shop, source_scope, query))
        return list(self.rows)

    async def _snapshot(self, ro_id):
        return copy.deepcopy(self.snapshots[ro_id])


@pytest.mark.asyncio
async def test_exact_ro_queue_uses_query_and_authoritative_top_level_shop_snapshot():
    client = QueueCIQ(
        rows=[
            {
                "id": "ro-1",
                "ro_number": "9000000001",
                "shop": "Wrong collection shop",
            }
        ],
        snapshots={
            "ro-1": {
                "shop": {"name": "Gerber Collision & Glass - Macon"},
                "repair_order": {
                    "id": "ro-1", "ro_number": "9000000001", "version": 4,
                },
                "vehicle": {
                    "vin": None,
                    "year": 2016,
                    "make": "Toyota",
                    "model": "Sienna",
                    "configuration": "L FWD w/7-Passenger Seating",
                },
                "calibrations": [
                    {
                        "id": "cal-1", "calibration_type": "Seat Belt",
                        "determination": "REQUIRED", "version": 2,
                    },
                    {
                        "id": "cal-2", "calibration_type": "Front View Camera",
                        "determination": "NOT_REQUIRED", "version": 3,
                    },
                ],
            }
        },
    )

    vehicles = await client.vehicles_for_ro_numbers(["9000000001"])

    assert client.queries == [(None, None, "all", "9000000001")]
    assert len(vehicles) == 1
    assert vehicles[0].shop == "Gerber Collision & Glass - Macon"
    assert vehicles[0].configuration == "L FWD w/7-Passenger Seating"
    assert vehicles[0].requirements == ["Seat Belt"]
    assert {row.id for row in vehicles[0].existing_calibrations} == {"cal-1", "cal-2"}


@pytest.mark.asyncio
async def test_exact_ro_queue_fails_closed_on_ambiguous_exact_match():
    rows = [
        {"id": "ro-1", "ro_number": "9000000001"},
        {"id": "ro-2", "ro_number": "9000000001"},
    ]
    snapshots = {
        row["id"]: {
            "repair_order": {"id": row["id"], "ro_number": row["ro_number"]},
            "vehicle": {"year": 2016, "make": "Toyota", "model": "Sienna"},
            "calibrations": [],
        }
        for row in rows
    }
    client = QueueCIQ(rows, snapshots)

    with pytest.raises(RuntimeError, match="multiple exact"):
        await client.vehicles_for_ro_numbers(["9000000001"])


@pytest.mark.asyncio
async def test_queue_rejects_collection_row_when_snapshot_is_missing():
    client = QueueCIQ(
        rows=[{"id": "ro-1", "ro_number": "9000000001", "shop": "Macon"}],
        snapshots={"ro-1": {}},
    )
    with pytest.raises(RuntimeError, match="snapshot"):
        await client.vehicles_for_ro_numbers(["9000000001"])


@pytest.mark.asyncio
async def test_exact_queue_rejects_reused_internal_ro_identity():
    class ReusedIdentityCIQ(CIQClient):
        def __init__(self):
            super().__init__(object())

        async def vehicles(self, phase=None, shop=None, source_scope="active", query=None):
            return [
                VehicleSpec(
                    ro_id="same-ro-id",
                    ro_number=query,
                    shop="Macon",
                    year=2020,
                    make="Toyota",
                    model="Camry",
                )
            ]

    client = ReusedIdentityCIQ()
    with pytest.raises(RuntimeError, match="reused"):
        await client.vehicles_for_ro_numbers(["RO-1", "RO-2"])
