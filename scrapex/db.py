from __future__ import annotations
import json, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from .models import BatchCreate
from .storage_policy import rewrite_nested_adas_map_paths


ADAS_MAP_COMPLETE_STATES = {"adas_map_complete"}
ADAS_MAP_CONTRACT_VERSION = 2
ADAS_MAP_ATTENTION_STATES = {
    "ro_not_found",
    "ambiguous_ro",
    "view_not_found",
    "view_did_not_navigate",
    "vin_missing",
    "requirements_unparsed",
    "login_required",
    "needs_operator",
    "failed",
}
ADAS_MAP_IN_PROGRESS_STATES = {
    "searching_adas_map",
    "ro_found",
    "vin_verified",
    "opening_inspection",
    "requirements_captured",
}

def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()

class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def conn(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _ensure_item_columns(self, db):
        existing = {
            row["name"]
            for row in db.execute("PRAGMA table_info(items)").fetchall()
        }
        additions = {
            "shop": "TEXT",
            "configuration_json": "TEXT NOT NULL DEFAULT '{}'",
            "ciq_requirements_json": "TEXT NOT NULL DEFAULT '[]'",
            "ciq_calibrations_json": "TEXT NOT NULL DEFAULT '[]'",
            "adas_map_state": "TEXT",
            "adas_map_contract_version": "INTEGER NOT NULL DEFAULT 0",
            "adas_map_attempts": "INTEGER NOT NULL DEFAULT 0",
            "adas_map_url": "TEXT",
            "adas_map_source_url": "TEXT",
            "adas_map_inspection_id": "TEXT",
            "adas_map_vin": "TEXT",
            "adas_map_vehicle_label": "TEXT",
            "adas_map_calibrations_json": "TEXT",
            "adas_map_requirements_json": "TEXT",
            "adas_map_alldata_links_json": "TEXT",
            "adas_map_report_links_json": "TEXT",
            "adas_map_raw_result_json": "TEXT",
            "adas_map_requirements_proven": "INTEGER NOT NULL DEFAULT 0",
            "adas_map_last_error": "TEXT",
            "adas_map_checked_at": "TEXT",
            "ciq_reconciliation_state": "TEXT NOT NULL DEFAULT 'pending'",
            "ciq_reconciliation_json": "TEXT",
            "ciq_reconciliation_error": "TEXT",
            "ciq_reconciled_at": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE items ADD COLUMN {name} {sql_type}")

    def _init(self):
        with self.conn() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS batches(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS items(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                ro_id TEXT, ro_number TEXT, vin TEXT, year INTEGER NOT NULL,
                make TEXT NOT NULL, model TEXT NOT NULL, trim TEXT, engine TEXT,
                requirements_json TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, selected_vehicle_label TEXT,
                quick_reference_url TEXT, procedure_count INTEGER NOT NULL DEFAULT 0,
                captured_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS documents(
                id TEXT PRIMARY KEY, item_id TEXT NOT NULL, title TEXT NOT NULL,
                source_url TEXT NOT NULL, canonical_url TEXT NOT NULL, article_id TEXT,
                sha256 TEXT, relative_path TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(item_id, canonical_url)
            );
            CREATE INDEX IF NOT EXISTS ix_items_batch_state ON items(batch_id,state);
            CREATE INDEX IF NOT EXISTS ix_docs_url ON documents(canonical_url);
            CREATE INDEX IF NOT EXISTS ix_docs_sha ON documents(sha256);
            CREATE TABLE IF NOT EXISTS navigator_tasks(
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, target_json TEXT NOT NULL,
                topic TEXT NOT NULL, state TEXT NOT NULL,
                step_count INTEGER NOT NULL DEFAULT 0, action_budget INTEGER NOT NULL,
                graph_json TEXT NOT NULL DEFAULT '{}',
                last_observation_json TEXT,
                verification_json TEXT, verified INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS navigator_steps(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                action_json TEXT NOT NULL, observation_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS ix_navigator_steps_task ON navigator_steps(task_id,ordinal);
            """)
            self._ensure_item_columns(db)

    def recover_after_restart(self):
        with self.conn() as db:
            db.execute(
                """
                UPDATE batches
                SET state='paused',updated_at=?,
                    last_error=COALESCE(last_error,'Service restarted; resume when ready.')
                WHERE state IN ('running','running_adas_map','running_alldata','pausing')
                """,
                (now(),)
            )
            states = ("selecting_vehicle","vehicle_verified","opening_quick_reference",
                      "discovering_procedures","capturing","indexing")
            marks = ",".join("?" for _ in states)
            db.execute(
                f"UPDATE items SET state='paused',updated_at=?,last_error=COALESCE(last_error,'Interrupted by service restart.') WHERE state IN ({marks})",
                (now(), *states)
            )
            # ADAS Map checkpoints are intentionally left at their last
            # explicit stage. The ADAS runner treats every non-terminal stage
            # as resumable work and safely starts the RO again; retaining the
            # checkpoint tells the operator how far the interrupted attempt got.
            db.execute(
                """
                UPDATE navigator_tasks
                SET state='paused',updated_at=?,
                    last_error=COALESCE(last_error,'Service restarted; resume when ready.')
                WHERE state='active'
                """,
                (now(),),
            )

    def create_batch(self, request: BatchCreate) -> str:
        bid, ts = uuid.uuid4().hex, now()
        with self.conn() as db:
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)", (bid,request.name,"pending",ts,ts))
            for n, v in enumerate(request.vehicles, 1):
                db.execute("""
                INSERT INTO items(
                    id,batch_id,ordinal,ro_id,ro_number,vin,shop,year,make,model,trim,engine,
                    configuration_json,requirements_json,ciq_requirements_json,
                    ciq_calibrations_json,state,adas_map_state,
                    ciq_reconciliation_state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    uuid.uuid4().hex,bid,n,v.ro_id,v.ro_number,v.vin,v.shop,
                    v.year or 0,v.make or "",v.model or "",v.trim,v.engine,
                    json.dumps(v.configuration, sort_keys=True),json.dumps(v.requirements),
                    json.dumps(v.requirements),
                    json.dumps([c.model_dump() for c in v.existing_calibrations]),
                    "pending","pending","pending",ts,ts
                ))
        return bid

    def _item(self, row):
        if row is None: return None
        d = dict(row)
        d["requirements"] = self._json_value(d.get("requirements_json"), [])
        d["ciq_requirements"] = self._json_value(d.get("ciq_requirements_json"), [])
        d["ciq_calibrations"] = self._json_value(d.get("ciq_calibrations_json"), [])
        d["configuration"] = self._json_value(d.get("configuration_json"), {})
        d["adas_map_requirements"] = self._json_value(
            d.get("adas_map_requirements_json"), []
        )
        d["ciq_reconciliation"] = self._json_value(
            d.get("ciq_reconciliation_json"), {}
        )
        return d

    @staticmethod
    def _json_value(value: Any, fallback: Any) -> Any:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return fallback
        return fallback if parsed is None else parsed

    def list_batches(self):
        with self.conn() as db:
            rows = db.execute(
                """
                SELECT b.*,COUNT(i.id) total,
                  SUM(CASE WHEN i.adas_map_contract_version=?
                            AND i.adas_map_state='adas_map_complete'
                            AND i.adas_map_requirements_proven=1
                            AND i.ciq_reconciliation_state='complete'
                           THEN 1 ELSE 0 END) complete_count,
                  SUM(CASE WHEN i.adas_map_contract_version=?
                            AND i.adas_map_state IN
                    ('ro_not_found','ambiguous_ro','view_not_found','view_did_not_navigate',
                     'vin_missing','requirements_unparsed','login_required','needs_operator','failed')
                    THEN 1 ELSE 0 END) needs_operator_count,
                  SUM(CASE WHEN i.adas_map_contract_version=?
                            AND i.adas_map_state IN ('retryable_error','failed')
                           THEN 1 ELSE 0 END) error_count
                FROM batches b LEFT JOIN items i ON i.batch_id=b.id
                GROUP BY b.id ORDER BY b.created_at DESC
                """,
                (
                    ADAS_MAP_CONTRACT_VERSION,
                    ADAS_MAP_CONTRACT_VERSION,
                    ADAS_MAP_CONTRACT_VERSION,
                ),
            ).fetchall()
        return [dict(r) for r in rows]

    def batch(self, bid: str):
        with self.conn() as db:
            b = db.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
            if not b: return None
            items = db.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal",(bid,)).fetchall()
        out = dict(b)
        out["items"] = [self._item(i) for i in items]
        out["summary"] = {
            "total": len(items),
            "complete": sum(
                i.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
                and i.get("adas_map_state") == "adas_map_complete"
                and bool(i.get("adas_map_requirements_proven"))
                and i.get("ciq_reconciliation_state") == "complete"
                for i in out["items"]
            ),
            "needs_operator": sum(
                i.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
                and i.get("adas_map_state") in ADAS_MAP_ATTENTION_STATES
                for i in out["items"]
            ),
            "errors": sum(
                i.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
                and i.get("adas_map_state") in ("retryable_error", "failed")
                for i in out["items"]
            ),
        }
        return out

    def next_item(self, bid: str):
        """Legacy selector retained for compatibility; the runner is frozen."""
        with self.conn() as db:
            row = db.execute("""
            SELECT * FROM items WHERE batch_id=? AND state IN ('pending','paused','retryable_error')
            ORDER BY ordinal LIMIT 1
            """,(bid,)).fetchone()
        return self._item(row)

    def next_adas_map_item(self, bid: str, max_attempts: int = 3):
        terminal = ADAS_MAP_COMPLETE_STATES | ADAS_MAP_ATTENTION_STATES
        marks = ",".join("?" for _ in terminal)
        with self.conn() as db:
            row = db.execute(
                f"""
                SELECT * FROM items
                WHERE batch_id=?
                  AND (
                    COALESCE(adas_map_contract_version,0) < ?
                    OR COALESCE(adas_map_state,'pending') NOT IN ({marks})
                  )
                  AND (
                    COALESCE(adas_map_contract_version,0) < ?
                    OR adas_map_attempts < ?
                  )
                ORDER BY CASE WHEN adas_map_attempts=0 THEN 0 ELSE 1 END,
                         ordinal
                LIMIT 1
                """,
                (
                    bid,
                    ADAS_MAP_CONTRACT_VERSION,
                    *sorted(terminal),
                    ADAS_MAP_CONTRACT_VERSION,
                    max_attempts,
                ),
            ).fetchone()
        return self._item(row)

    def set_batch_state(self,bid,state,error=None):
        with self.conn() as db:
            db.execute("UPDATE batches SET state=?,last_error=?,updated_at=? WHERE id=?",(state,error,now(),bid))

    def set_item(self,item_id,state,**fields):
        allowed={
            "attempts","selected_vehicle_label",
            "quick_reference_url","procedure_count","captured_count",
            "duplicate_count","last_error","vin","shop","year","make",
            "model","trim","engine","configuration_json","requirements_json",
            "adas_map_state","adas_map_contract_version","adas_map_attempts","adas_map_url",
            "adas_map_source_url","adas_map_inspection_id","adas_map_vin",
            "adas_map_vehicle_label","adas_map_calibrations_json",
            "adas_map_requirements_json","adas_map_alldata_links_json",
            "adas_map_report_links_json","adas_map_raw_result_json",
            "adas_map_requirements_proven","adas_map_last_error",
            "adas_map_checked_at","ciq_reconciliation_state",
            "ciq_reconciliation_json","ciq_reconciliation_error",
            "ciq_reconciled_at",
        }
        fields={k:v for k,v in fields.items() if k in allowed}
        sql=["state=?","updated_at=?"]+[f"{k}=?" for k in fields]
        vals=[state,now(),*fields.values(),item_id]
        with self.conn() as db:
            db.execute(f"UPDATE items SET {','.join(sql)} WHERE id=?",vals)

    def checkpoint_adas_map(self, item_id: str, stage: str, **fields: Any) -> None:
        """Persist an explicit ADAS Map checkpoint without touching ALLDATA."""
        item_state = fields.pop("item_state", self.item_state(item_id))
        self.set_item(
            item_id,
            item_state,
            adas_map_state=stage,
            adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
            **fields,
        )

    def item_state(self, item_id: str) -> str:
        with self.conn() as db:
            row = db.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
        return str(row["state"]) if row else "pending"

    def save_reconciliation(
        self, item_id: str, state: str, result: dict[str, Any] | None, error: str | None = None
    ) -> None:
        self.set_item(
            item_id,
            self.item_state(item_id),
            ciq_reconciliation_state=state,
            ciq_reconciliation_json=json.dumps(result or {}, sort_keys=True),
            ciq_reconciliation_error=error,
            ciq_reconciled_at=now(),
        )

    def record_document(self,item_id,title,source_url,canonical_url,article_id,sha256,relative_path,status):
        with self.conn() as db:
            old=db.execute("SELECT id FROM documents WHERE item_id=? AND canonical_url=?",(item_id,canonical_url)).fetchone()
            doc_id=old["id"] if old else uuid.uuid4().hex
            db.execute("""
            INSERT OR REPLACE INTO documents(id,item_id,title,source_url,canonical_url,article_id,sha256,relative_path,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,(doc_id,item_id,title,source_url,canonical_url,article_id,sha256,relative_path,status,now()))

    def adas_map_summary(self, bid: str) -> dict:
        with self.conn() as db:
            rows = db.execute(
                """
                SELECT
                    state,
                    vin,
                    adas_map_state,
                    adas_map_contract_version,
                    adas_map_vin,
                    adas_map_requirements_proven,
                    ciq_reconciliation_state,
                    adas_map_last_error
                FROM items
                WHERE batch_id=?
                ORDER BY ordinal
                """,
                (bid,),
            ).fetchall()

        total = len(rows)
        complete = sum(
            1 for row in rows
            if row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and row["adas_map_state"] == "adas_map_complete"
            and bool(row["adas_map_requirements_proven"])
        )
        vin_ready = sum(
            1 for row in rows
            if row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and row["adas_map_vin"]
        )
        needs_attention = sum(
            1 for row in rows
            if row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and row["adas_map_state"] in ADAS_MAP_ATTENTION_STATES
        )
        requirements_ready = sum(
            row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and bool(row["adas_map_requirements_proven"])
            for row in rows
        )
        ciq_reconciled = sum(
            row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and row["ciq_reconciliation_state"] == "complete"
            for row in rows
        )
        ready = sum(
            row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
            and row["adas_map_state"] == "adas_map_complete"
            and bool(row["adas_map_requirements_proven"])
            and row["ciq_reconciliation_state"] == "complete"
            for row in rows
        )

        return {
            "total": total,
            "adas_map_complete": complete,
            "vin_ready": vin_ready,
            "vin_missing": total - vin_ready,
            "needs_attention": needs_attention,
            "requirements_ready": requirements_ready,
            "ciq_reconciled": ciq_reconciled,
            "ready": ready,
            "manual_future": ready,
        }

    def pipeline_summary(self, bid: str) -> dict[str, int]:
        with self.conn() as db:
            rows = db.execute(
                """
                SELECT adas_map_state,adas_map_contract_version,
                       adas_map_requirements_proven,
                       ciq_reconciliation_state
                FROM items WHERE batch_id=? ORDER BY ordinal
                """,
                (bid,),
            ).fetchall()
        return {
            "total": len(rows),
            "adas_map_complete": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["adas_map_state"] == "adas_map_complete"
                and bool(row["adas_map_requirements_proven"])
                for row in rows
            ),
            "adas_map_attention": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["adas_map_state"] in ADAS_MAP_ATTENTION_STATES
                for row in rows
            ),
            "ciq_reconciled": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["ciq_reconciliation_state"] == "complete"
                for row in rows
            ),
            "ready": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["adas_map_state"] == "adas_map_complete"
                and bool(row["adas_map_requirements_proven"])
                and row["ciq_reconciliation_state"] == "complete"
                for row in rows
            ),
            "manual_future": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["adas_map_state"] == "adas_map_complete"
                and bool(row["adas_map_requirements_proven"])
                and row["ciq_reconciliation_state"] == "complete"
                for row in rows
            ),
            "needs_operator": sum(
                row["adas_map_contract_version"] == ADAS_MAP_CONTRACT_VERSION
                and row["adas_map_state"] in ADAS_MAP_ATTENTION_STATES
                for row in rows
            ),
        }

    def delete_batch(self, bid: str) -> dict:
        """Delete a ScrapeX-only batch only when processing has not begun."""
        with self.conn() as db:
            batch = db.execute(
                "SELECT * FROM batches WHERE id=?",
                (bid,),
            ).fetchone()
            if not batch:
                return {"deleted": False, "reason": "not_found"}

            rows = db.execute(
                """
                SELECT
                    state,
                    captured_count,
                    duplicate_count,
                    adas_map_state
                FROM items
                WHERE batch_id=?
                """,
                (bid,),
            ).fetchall()

            unsafe = [
                dict(row)
                for row in rows
                if int(row["captured_count"] or 0) > 0
                or row["state"] not in ("pending", "paused", "retryable_error")
                or str(row["adas_map_state"] or "") not in ("", "pending")
            ]
            if unsafe:
                return {
                    "deleted": False,
                    "reason": "batch_has_started",
                    "message": (
                        "Refusing to delete a batch that has started ADAS Map "
                        "or ALLDATA processing."
                    ),
                }

            db.execute(
                """
                DELETE FROM documents
                WHERE item_id IN (
                    SELECT id FROM items WHERE batch_id=?
                )
                """,
                (bid,),
            )
            db.execute("DELETE FROM items WHERE batch_id=?", (bid,))
            db.execute("DELETE FROM batches WHERE id=?", (bid,))
            return {"deleted": True, "batch_id": bid}

    def exceptions(self, bid):
        with self.conn() as db:
            rows = db.execute(
                """
                SELECT *
                FROM items
                WHERE batch_id=?
                  AND adas_map_contract_version=?
                  AND adas_map_state IN (
                      'ro_not_found','ambiguous_ro','view_not_found',
                      'view_did_not_navigate','vin_missing','requirements_unparsed',
                      'login_required','needs_operator','retryable_error','failed'
                  )
                ORDER BY ordinal
                """,
                (bid, ADAS_MAP_CONTRACT_VERSION),
            ).fetchall()
        return [self._item(row) for row in rows]

    # -- Navigator tasks ---------------------------------------------------

    def create_navigator_task(
        self, provider: str, target: dict[str, Any], topic: str, action_budget: int
    ) -> str:
        task_id, ts = uuid.uuid4().hex, now()
        with self.conn() as db:
            db.execute(
                """
                INSERT INTO navigator_tasks(
                    id,provider,target_json,topic,state,step_count,action_budget,
                    graph_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id, provider, json.dumps(target, sort_keys=True), topic,
                    "pending", 0, int(action_budget), "{}", ts, ts,
                ),
            )
        return task_id

    def _navigator_task(self, row):
        if row is None:
            return None
        d = dict(row)
        d["target"] = self._json_value(d.get("target_json"), {})
        d["graph"] = self._json_value(d.get("graph_json"), {})
        d["last_observation"] = self._json_value(d.get("last_observation_json"), None)
        d["verification"] = self._json_value(d.get("verification_json"), None)
        d["verified"] = bool(d.get("verified"))
        return d

    def navigator_task(self, task_id: str) -> dict[str, Any] | None:
        with self.conn() as db:
            row = db.execute(
                "SELECT * FROM navigator_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._navigator_task(row)

    def navigator_task_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self.conn() as db:
            rows = db.execute(
                "SELECT * FROM navigator_steps WHERE task_id=? ORDER BY ordinal",
                (task_id,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["action"] = self._json_value(d.get("action_json"), {})
            d["observation"] = self._json_value(d.get("observation_json"), {})
            out.append(d)
        return out

    def append_navigator_step(
        self, task_id: str, action: dict[str, Any], observation: dict[str, Any]
    ) -> int:
        """Persist one step, advance step_count, and cache the latest observation.

        Returns the new step_count so the caller can enforce ``action_budget``
        without a second round trip.
        """
        with self.conn() as db:
            row = db.execute(
                "SELECT step_count FROM navigator_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"navigator task not found: {task_id}")
            ordinal = int(row["step_count"]) + 1
            db.execute(
                """
                INSERT INTO navigator_steps(id,task_id,ordinal,action_json,observation_json,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex, task_id, ordinal,
                    json.dumps(action, sort_keys=True, default=str),
                    json.dumps(observation, sort_keys=True, default=str),
                    now(),
                ),
            )
            db.execute(
                """
                UPDATE navigator_tasks
                SET step_count=?,last_observation_json=?,updated_at=?
                WHERE id=?
                """,
                (ordinal, json.dumps(observation, sort_keys=True, default=str), now(), task_id),
            )
        return ordinal

    def cache_navigator_observation(
        self, task_id: str, observation: dict[str, Any]
    ) -> None:
        """Cache the latest observation without advancing ``step_count``.

        A plain ``observe`` is a read, not a browser action -- it must not
        consume the caller's ``action_budget``. Only ``append_navigator_step``
        (called for actual executed actions) advances the ordinal/budget
        counter.
        """
        with self.conn() as db:
            db.execute(
                "UPDATE navigator_tasks SET last_observation_json=?,updated_at=? WHERE id=?",
                (json.dumps(observation, sort_keys=True, default=str), now(), task_id),
            )

    def set_navigator_task_state(
        self,
        task_id: str,
        state: str,
        *,
        graph: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> None:
        fields = ["state=?", "updated_at=?"]
        values: list[Any] = [state, now()]
        if graph is not None:
            fields.append("graph_json=?")
            values.append(json.dumps(graph, sort_keys=True, default=str))
        if last_error is not None:
            fields.append("last_error=?")
            values.append(last_error)
        values.append(task_id)
        with self.conn() as db:
            db.execute(
                f"UPDATE navigator_tasks SET {','.join(fields)} WHERE id=?", values
            )

    def save_navigator_verification(
        self, task_id: str, verification: dict[str, Any]
    ) -> None:
        verified = bool(verification.get("verified"))
        with self.conn() as db:
            db.execute(
                """
                UPDATE navigator_tasks
                SET verification_json=?,verified=?,
                    state=CASE WHEN ? THEN 'verified' ELSE state END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    json.dumps(verification, sort_keys=True, default=str),
                    int(verified), int(verified), now(), task_id,
                ),
            )


    def normalize_adas_map_storage_paths(self, adas_si_root: Path) -> int:
        """Rewrite persisted local ADAS Map paths to ADAS Map/<RO>/.

        This runs independently of the filesystem migration so a prior X Omni
        startup may already have moved the physical file before ScrapeX starts.
        Source HTTP URLs are untouched.
        """
        root = Path(adas_si_root).resolve()
        changed = 0
        with self.conn() as db:
            rows = db.execute(
                "SELECT id,adas_map_report_links_json,adas_map_raw_result_json FROM items"
            ).fetchall()
            for row in rows:
                updates: dict[str, str] = {}
                for column in ("adas_map_report_links_json", "adas_map_raw_result_json"):
                    raw = row[column]
                    if raw is None:
                        continue
                    try:
                        parsed = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    normalized = rewrite_nested_adas_map_paths(parsed, root)
                    if normalized != parsed:
                        updates[column] = json.dumps(
                            normalized, sort_keys=True, default=str
                        )
                if updates:
                    assignments = ",".join(f"{key}=?" for key in updates)
                    db.execute(
                        f"UPDATE items SET {assignments},updated_at=? WHERE id=?",
                        [*updates.values(), now(), row["id"]],
                    )
                    changed += 1

            docs = db.execute(
                "SELECT id,relative_path FROM documents WHERE relative_path IS NOT NULL"
            ).fetchall()
            for row in docs:
                old = str(row["relative_path"] or "")
                normalized = rewrite_nested_adas_map_paths(old, root)
                if normalized == old:
                    continue
                try:
                    rel = str(
                        Path(normalized).resolve().relative_to(root)
                    ).replace("\\", "/")
                except (ValueError, OSError):
                    rel = normalized
                db.execute(
                    "UPDATE documents SET relative_path=? WHERE id=?",
                    (rel, row["id"]),
                )
                changed += 1
        return changed
