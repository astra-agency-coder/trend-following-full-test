"""Model aggregation point — importing this package registers all tables."""
from app.persistence.models.base import (  # noqa: F401
    Base,
    SystemRun,
    ConfigVersionRow,
    SymbolSpec,
    MarketTickRow,
    MarketBarRow,
    DataImportJobRow,
    create_engine,
    sessionmaker,
)
from app.persistence.models.trading import (  # noqa: F401
    MarketSnapshotRow,
    SessionInstanceRow,
    StrategyEvaluationRow,
    RouterDecisionRow,
    RouterCandidateRankingRow,
)
from app.persistence.models.operations import (  # noqa: F401
    PaperPortfolioRow,
    PaperOrderRow,
    PaperTradeRow,
    TradeEventRow,
    RiskDecisionRow,
    RejectedOpportunityRow,
    ShadowOutcomeRow,
    TelegramOutboxRow,
    AuditEventRow,
    SystemHealthEventRow,
    ServiceComponentRow,
    ReportRunRow,
    EconomicEventRow,
)
