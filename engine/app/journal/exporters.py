"""CSV/JSON exporters for the research journal (spec 12.3 / 40.8)."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.persistence.database import Database, dumps, loads
from app.persistence.models import (
    AuditEventRow,
    MarketSnapshotRow,
    PaperTradeRow,
    RejectedOpportunityRow,
    RouterDecisionRow,
    ShadowOutcomeRow,
    StrategyEvaluationRow,
    TradeEventRow,
)
from sqlalchemy import select

EXPORTABLE = {
    "audit_events": AuditEventRow,
    "market_snapshots": MarketSnapshotRow,
    "strategy_evaluations": StrategyEvaluationRow,
    "router_decisions": RouterDecisionRow,
    "paper_trades": PaperTradeRow,
    "trade_events": TradeEventRow,
    "rejected_opportunities": RejectedOpportunityRow,
    "shadow_outcomes": ShadowOutcomeRow,
}

JSON_TABLES = {"market_snapshots", "strategy_evaluations", "router_decisions"}


class JournalExporter:
    def __init__(self, database: Database):
        self.db = database

    def _rows(self, table: str, start_utc: datetime | None, end_utc: datetime | None) -> list[Any]:
        model = EXPORTABLE[table]
        time_col = {
            AuditEventRow: AuditEventRow.timestamp_utc,
            MarketSnapshotRow: MarketSnapshotRow.timestamp_utc,
            StrategyEvaluationRow: StrategyEvaluationRow.timestamp_utc,
            RouterDecisionRow: RouterDecisionRow.timestamp_utc,
            PaperTradeRow: PaperTradeRow.created_at_utc,
            TradeEventRow: TradeEventRow.timestamp_utc,
            RejectedOpportunityRow: RejectedOpportunityRow.rejected_at_utc,
            ShadowOutcomeRow: ShadowOutcomeRow.tracked_at_utc,
        }[model]
        stmt = select(model)
        if start_utc:
            stmt = stmt.where(time_col >= start_utc)
        if end_utc:
            stmt = stmt.where(time_col < end_utc)
        with self.db.session() as session:
            return list(session.execute(stmt).scalars().all())

    @staticmethod
    def _row_to_dict(row: Any) -> dict:
        out: dict[str, Any] = {}
        for column in row.__table__.columns:
            value = getattr(row, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[column.name] = value
        return out

    def export(
        self,
        table: str,
        *,
        fmt: str = "csv",
        output_dir: Path | str,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> Path:
        if table not in EXPORTABLE:
            raise ValueError(f"unknown table {table!r}; valid: {sorted(EXPORTABLE)}")
        rows = self._rows(table, start_utc, end_utc)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"{table}_{stamp}.{fmt}"

        dicts = [self._row_to_dict(r) for r in rows]
        if fmt == "json":
            path.write_text(
                json.dumps(dicts, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
        else:
            if not dicts:
                path.write_text("", encoding="utf-8")
            else:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(dicts[0].keys()))
                    writer.writeheader()
                    writer.writerows(dicts)
        return path

    def export_all(self, *, output_dir: Path | str, start_utc: datetime | None = None, end_utc: datetime | None = None) -> list[Path]:
        paths = []
        for table in EXPORTABLE:
            fmt = "json" if table in JSON_TABLES else "csv"
            paths.append(self.export(table, fmt=fmt, output_dir=output_dir, start_utc=start_utc, end_utc=end_utc))
        return paths

    def manifest(self, paths: Iterable[Path], output_dir: Path | str, *, scope: str) -> Path:
        out_dir = Path(output_dir)
        manifest = {
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "scope": scope,
            "files": [{"name": p.name, "bytes": p.stat().st_size} for p in paths],
        }
        path = out_dir / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
