"""Public treasury aggregates and durable weekly report payloads."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ReportWindow:
    week_start: date
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetArticleReport:
    code: str
    cap: int
    spent: int
    reserved: int


@dataclass(frozen=True, slots=True)
class TreasuryReport:
    window: ReportWindow
    as_of: datetime
    opening_balance: int
    closing_balance: int
    company_fees: int
    sales_tax: int
    asset_sales: int
    treasury_issuance: int
    other_income: int
    npc_payouts: int
    contract_payouts: int
    other_expenses: int
    total_issued: int
    starter_grants: int
    transaction_count: int
    settled_orders: int
    asset_purchases: int
    open_escrow: int
    cash_reserved: int
    budget_limit: int | None
    budget_articles: tuple[BudgetArticleReport, ...]

    @property
    def available_cash(self) -> int:
        return self.closing_balance - self.cash_reserved

    @property
    def total_income(self) -> int:
        return self.company_fees + self.sales_tax + self.asset_sales + self.treasury_issuance + self.other_income

    @property
    def total_expenses(self) -> int:
        return self.npc_payouts + self.contract_payouts + self.other_expenses

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["version"] = 1
        payload["window"] = {
            "week_start": self.window.week_start.isoformat(),
            "starts_at": self.window.starts_at.isoformat(),
            "ends_at": self.window.ends_at.isoformat(),
        }
        payload["as_of"] = self.as_of.isoformat()
        payload["budget_articles"] = list(payload["budget_articles"])
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TreasuryReport":
        fields = dict(payload)
        if fields.pop("version", None) != 1:
            raise ValueError("Unsupported treasury report payload version")
        window = fields.pop("window")
        return cls(
            window=ReportWindow(
                week_start=date.fromisoformat(window["week_start"]),
                starts_at=datetime.fromisoformat(window["starts_at"]),
                ends_at=datetime.fromisoformat(window["ends_at"]),
            ),
            as_of=datetime.fromisoformat(fields.pop("as_of")),
            budget_articles=tuple(BudgetArticleReport(**item) for item in fields.pop("budget_articles")),
            **fields,
        )


@dataclass(frozen=True, slots=True)
class ReportDelivery:
    guild_id: int
    channel_id: int
    report: TreasuryReport
    attempts: int
    lease_token: str
