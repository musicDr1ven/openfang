"""
Custom Pydantic entity type definitions for guided LLM extraction.

Graphiti uses these schemas to constrain what the LLM extracts from each episode,
significantly improving extraction quality for domain-specific knowledge graphs.

Two agent domains are defined:
  - Trading: strategies, market events, assets, indicators
  - Nursing:  medications, procedures, certifications, protocols
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Trading agent entity types
# ---------------------------------------------------------------------------


class TradingStrategy(BaseModel):
    """A trading or investment strategy extracted from financial literature."""

    asset_class: Optional[str] = None
    """Broad asset class: 'equities', 'crypto', 'futures', 'fixed_income', etc."""

    timeframe: Optional[str] = None
    """Typical holding period: 'intraday', 'swing', 'positional', 'long_term'."""

    indicators: Optional[list[str]] = None
    """Technical indicators used (e.g. ['RSI', 'MACD', 'Bollinger Bands'])."""

    risk_profile: Optional[str] = None
    """Qualitative risk description: 'low', 'medium', 'high', 'speculative'."""

    regime_dependency: Optional[str] = None
    """Market regime the strategy works best in: 'trending', 'ranging', 'volatile'."""


class MarketEvent(BaseModel):
    """A significant market event that may affect prices or strategies."""

    event_type: Optional[str] = None
    """Type: 'earnings', 'fed_meeting', 'halving', 'economic_release', 'geopolitical'."""

    date: Optional[datetime] = None
    """Date or expected date of the event."""

    impact: Optional[str] = None
    """Expected or observed impact: 'bullish', 'bearish', 'neutral', 'uncertain'."""


class Asset(BaseModel):
    """A tradeable asset or security."""

    ticker: Optional[str] = None
    """Ticker symbol (e.g. 'BTC', 'AAPL', 'SPY')."""

    asset_type: Optional[str] = None
    """Asset type: 'stock', 'etf', 'crypto', 'futures', 'options', 'bond'."""

    sector: Optional[str] = None
    """Sector or category (e.g. 'technology', 'DeFi', 'commodities')."""


class Indicator(BaseModel):
    """A technical or fundamental indicator used in trading analysis."""

    full_name: Optional[str] = None
    """Expanded name (e.g. 'Relative Strength Index' for 'RSI')."""

    category: Optional[str] = None
    """Category: 'momentum', 'trend', 'volatility', 'volume', 'fundamental'."""

    typical_period: Optional[str] = None
    """Common period setting (e.g. '14' for RSI, '26/12/9' for MACD)."""


# Map of entity type name → Pydantic class for the trading agent
TRADING_ENTITY_TYPES = {
    "TradingStrategy": TradingStrategy,
    "MarketEvent": MarketEvent,
    "Asset": Asset,
    "Indicator": Indicator,
}


# ---------------------------------------------------------------------------
# Infusion nursing agent entity types
# ---------------------------------------------------------------------------


class Medication(BaseModel):
    """A medication or drug relevant to infusion nursing."""

    drug_class: Optional[str] = None
    """Pharmacological class (e.g. 'antibiotic', 'anticoagulant', 'chemotherapy')."""

    route: Optional[str] = None
    """Administration route: 'IV', 'oral', 'infusion', 'subcutaneous', 'IM'."""

    common_indications: Optional[list[str]] = None
    """Common clinical indications for use."""

    requires_filter: Optional[bool] = None
    """Whether an in-line filter is required for infusion."""


class Procedure(BaseModel):
    """A clinical procedure performed by infusion nurses."""

    procedure_type: Optional[str] = None
    """Procedure category: 'IV_insertion', 'PICC_care', 'port_access', 'blood_draw'."""

    sterility_level: Optional[str] = None
    """Required sterility: 'aseptic', 'sterile', 'clean'."""

    equipment_required: Optional[list[str]] = None
    """List of required equipment items."""


class NursingCertification(BaseModel):
    """A professional certification relevant to infusion nursing."""

    issuing_body: Optional[str] = None
    """Organization issuing the certification (e.g. 'INCC', 'ANCC', 'ONS')."""

    validity_period_years: Optional[int] = None
    """How many years the certification remains valid before renewal."""

    renewal_requirements: Optional[str] = None
    """Summary of renewal requirements (CE credits, re-examination, etc.)."""


class Protocol(BaseModel):
    """A clinical protocol or guideline for infusion nursing care."""

    protocol_type: Optional[str] = None
    """Type: 'infection_control', 'medication_administration', 'emergency', 'documentation'."""

    issuing_body: Optional[str] = None
    """Organization that issued or endorses the protocol."""

    last_updated: Optional[str] = None
    """Date of most recent revision (ISO date string)."""


# Map of entity type name → Pydantic class for the nursing agent
NURSING_ENTITY_TYPES = {
    "Medication": Medication,
    "Procedure": Procedure,
    "NursingCertification": NursingCertification,
    "Protocol": Protocol,
}


# ---------------------------------------------------------------------------
# Agent type → entity type map
# ---------------------------------------------------------------------------

AGENT_ENTITY_TYPES: dict[str, dict] = {
    "trading": TRADING_ENTITY_TYPES,
    "nursing": NURSING_ENTITY_TYPES,
}

DEFAULT_ENTITY_TYPES = ["person", "organization", "concept", "event", "document"]
