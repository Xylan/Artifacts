import json
import time
from typing import List, Optional
from .base_store import BaseStore
from models import PlanTask, TaskType, TaskStatus


class TaskStore(BaseStore):
    """Persists in-progress GearPlan tasks (see planning.py) across process
    restarts. Unlike the other *_store.py caches this isn't TTL-based --
    rows live here until a task/plan completes and is explicitly pruned
    via delete_plan(), or PlanRunner prunes it automatically once every
    task in a plan is TaskStatus.DONE.
    """

    def __init__(self, db_path: str = "artifacts_game.db", api=None):
        super().__init__(db_path=db_path, api=api, ttl_seconds=0, table_name="tasks")
        self._init_db()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    plan_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    code TEXT NOT NULL,
                    node_code TEXT DEFAULT '',
                    target_quantity INTEGER NOT NULL,
                    skill TEXT DEFAULT '',
                    skill_level INTEGER DEFAULT 1,
                    produces_per_action INTEGER DEFAULT 1,
                    depends_on TEXT DEFAULT '[]',
                    assigned_to TEXT,
                    status TEXT DEFAULT 'pending',
                    updated_at REAL,
                    PRIMARY KEY (plan_id, task_id)
                )
            """)
            conn.commit()

    def save_plan(self, plan_id: str, tasks: List[PlanTask]) -> None:
        """Upserts every task in the plan. Safe to call repeatedly (e.g. once
        at the start of PlanRunner.run()) -- existing rows are updated in
        place rather than duplicated."""
        with self._get_connection() as conn:
            for t in tasks:
                conn.execute("""
                    INSERT INTO tasks (plan_id, task_id, type, code, node_code, target_quantity,
                                        skill, skill_level, produces_per_action, depends_on,
                                        assigned_to, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id, task_id) DO UPDATE SET
                        target_quantity=excluded.target_quantity,
                        assigned_to=excluded.assigned_to,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                """, (
                    plan_id, t.id, t.type.value, t.code, t.node_code, t.target_quantity,
                    t.skill, t.skill_level, t.produces_per_action, json.dumps(t.depends_on),
                    t.assigned_to, t.status.value, time.time(),
                ))
            conn.commit()

    def load_plan(self, plan_id: str) -> Optional[List[PlanTask]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE plan_id = ? ORDER BY task_id", (plan_id,)
            ).fetchall()
            return [self._row_to_task(r) for r in rows] if rows else None

    def _row_to_task(self, row) -> PlanTask:
        return PlanTask(
            id=row["task_id"], type=TaskType(row["type"]), code=row["code"],
            node_code=row["node_code"], target_quantity=row["target_quantity"],
            skill=row["skill"], skill_level=row["skill_level"],
            produces_per_action=row["produces_per_action"],
            depends_on=json.loads(row["depends_on"]), assigned_to=row["assigned_to"],
            status=TaskStatus(row["status"]),
        )

    def list_open_plans(self) -> List[str]:
        """Distinct plan_ids that still have at least one non-DONE task --
        used by planning.load_open_plans() to rehydrate work-in-progress
        plans after a restart."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT DISTINCT plan_id FROM tasks WHERE status != 'done'")
            return [row[0] for row in cursor.fetchall()]

    def update_status(self, plan_id: str, task_id: int, status: TaskStatus) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE plan_id = ? AND task_id = ?",
                (status.value, time.time(), plan_id, task_id),
            )
            conn.commit()

    def update_assignment(self, plan_id: str, task_id: int, character_name: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET assigned_to = ? WHERE plan_id = ? AND task_id = ?",
                (character_name, plan_id, task_id),
            )
            conn.commit()

    def delete_plan(self, plan_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM tasks WHERE plan_id = ?", (plan_id,))
            conn.commit()

    def is_plan_complete(self, plan_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE plan_id = ? AND status != 'done'", (plan_id,)
            ).fetchone()
            return row[0] == 0
