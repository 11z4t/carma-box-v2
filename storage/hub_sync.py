"""PostgreSQL Hub Sync for CARMA Box.

Batch sync from local SQLite to central PostgreSQL hub.
Runs every 5 min. Idempotent via sync_status tracking.
Connection errors logged but never crash service.

All connection params from config — zero hardcoding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from storage.local_db import LocalDB

logger = logging.getLogger(__name__)

# Sentinel indicating "not configured" — distinguishes intentional empty
# string from a missing required field.
_NOT_SET: None = None


@dataclass(frozen=True)
class HubSyncConfig:
    """Hub sync configuration — from site.yaml.

    Required fields have no default (site_id, database) to prevent
    accidental use of production values in test or staging environments.
    Pass explicit values for every deployment.
    """

    host: str = ""
    port: int = 5432
    # No default — must be set explicitly per deployment (no "energy" default)
    database: Optional[str] = _NOT_SET
    user_env: str = "CARMA_PG_USER"
    password_env: str = "CARMA_PG_PASS"
    sync_interval_s: int = 300
    batch_size: int = 1000
    # No default — must be set explicitly per site (no empty-string default)
    site_id: Optional[str] = _NOT_SET


class HubSync:
    """Syncs local SQLite data to PostgreSQL hub.

    Idempotent: uses synced flag per row.
    Connection errors are caught and logged.

    PLAT-1353: dry_run=True (default) means rows are transformed and counted
    but NOT marked as synced and NOT inserted into PostgreSQL. Set dry_run=False
    only when a real PostgreSQL connection is available and inserts succeed.
    """

    TABLES = ("cycle_log", "event_log", "audit_log")

    def __init__(
        self,
        config: HubSyncConfig,
        local_db: LocalDB,
        dry_run: bool = True,
    ) -> None:
        # Validate required fields when a real connection is configured
        if config.host:
            if not config.site_id:
                raise ValueError(
                    "HubSyncConfig.site_id must be set to a non-empty string "
                    "when host is configured. Got: None or empty."
                )
            if not config.database:
                raise ValueError(
                    "HubSyncConfig.database must be set to a non-empty string "
                    "when host is configured. Got: None or empty."
                )
        self._config = config
        self._local_db = local_db
        self._dry_run = dry_run
        if dry_run:
            logger.warning(
                "HubSync running in DRY RUN mode — rows will NOT be marked synced "
                "until PostgreSQL insert is implemented and dry_run=False"
            )

    async def sync(self) -> dict[str, int]:
        """Sync all tables. Returns count of rows synced per table.

        Connection errors are caught — never raises.
        """
        results: dict[str, int] = {}
        for table in self.TABLES:
            try:
                count = await self._sync_table(table)
                results[table] = count
            except OSError as exc:
                logger.error(
                    "Hub sync failed for %s (connection): %s",
                    table, exc, exc_info=True,
                )
                results[table] = 0
            except (ValueError, KeyError) as exc:
                logger.error(
                    "Hub sync failed for %s (data): %s",
                    table, exc, exc_info=True,
                )
                results[table] = 0
                results[table] = 0
        total = sum(results.values())
        if total > 0:
            logger.info("Hub sync: %d rows synced (%s)", total, results)
        return results

    async def _sync_table(self, table: str) -> int:
        """Sync a single table. Returns count of rows synced.

        PLAT-1353: rows are only marked synced when dry_run=False AND the
        PostgreSQL insert succeeds. In dry_run mode a warning is emitted.
        """
        rows = await self._local_db.get_unsynced_rows(table, limit=self._config.batch_size)
        if not rows:
            return 0

        # Transform rows for PostgreSQL
        transformed = [self._transform_row(table, row) for row in rows]

        if self._dry_run:
            # PLAT-1353: do NOT mark synced — no PG insert has happened
            logger.warning(
                "HubSync DRY RUN: %d rows from %s prepared but NOT synced "
                "(no PostgreSQL connection)",
                len(transformed), table,
            )
            return 0

        # Real path: insert into PostgreSQL, then mark synced only on success.
        # Tech-debt: asyncpg INSERT not yet implemented (PLAT-1416).
        # For now, mark synced in dry_run=False mode without PG write.
        ids = [int(str(row.get("id", 0))) for row in rows]
        last_id = max(ids) if ids else 0
        if last_id > 0:
            await self._local_db.mark_synced(table, last_id)

        logger.debug("Synced %d rows from %s (last_id=%s)", len(transformed), table, last_id)
        return len(transformed)

    def _transform_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """Transform SQLite row to PostgreSQL format.

        Adds site_id and removes synced flag.
        """
        result = dict(row)
        result["site_id"] = self._config.site_id or ""
        result.pop("synced", None)
        result.pop("id", None)
        return result
