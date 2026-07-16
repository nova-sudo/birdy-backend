from pydantic import BaseModel
from typing import Optional, List


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    default_currency: str


class LoginRequest(BaseModel):
    email: str
    password: str
    rememberMe: bool = False


class LinkClientRequest(BaseModel):
    client_name: str
    ghl_location_id: str
    meta_ad_account_id: str
    hotprospector_group_id: str


class ClientGroupRequest(BaseModel):
    name: str
    ghl_location_id: str | None
    meta_ad_account_id: str | None
    hotprospector_group_id: str | None
    ad_account_currency: str | None
    notes: str | None = ""
    client_status: str | None = "Active"
    # Which provider feeds the Sales-Hub call log view for this client.
    # "ghl" reads from the call_logs collection (populated by the GHL workflow
    # webhook). "hotprospector" reserves the choice; HP data wiring comes once
    # their endpoints are ready. Default mirrors the working path today.
    call_log_provider: str | None = "ghl"


class SaveViewRequest(BaseModel):
    page: str
    visible_columns: list


class AlertCondition(BaseModel):
    metric: str
    operator: str
    value: float
    period: Optional[str] = "week"


class CreateAlertRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    type: Optional[str] = "warning"        # "win" | "warning"
    condition: AlertCondition
    target_group_ids: Optional[List[str]] = []
    notification_channels: Optional[List[str]] = ["in_app"]
    frequency: Optional[str] = "daily"     # "realtime" | "hourly" | "daily" | "weekly"
    # "total"      → aggregate all selected clients into one value, evaluate once
    # "per_client" → evaluate the condition independently for each client group;
    #                the alert triggers (and fires a notification) for every client
    #                that crosses the threshold individually
    tracking_mode: Optional[str] = "total"


class UpdateAlertRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    condition: Optional[AlertCondition] = None
    target_group_ids: Optional[List[str]] = None
    notification_channels: Optional[List[str]] = None
    frequency: Optional[str] = None
    status: Optional[str] = None
    tracking_mode: Optional[str] = None


class SnoozeAlertRequest(BaseModel):
    hours: int = 24


class SnoozeClientRequest(BaseModel):
    """Snooze a single client within a per-client alert without pausing the whole alert."""
    client_id: str
    hours: int = 24


# ── Custom Metrics ─────────────────────────────────────────────────────────

class CreateCustomMetricRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    formula_parts: list            # [{type: "metric", value: "spend"}, {type: "operator", value: "/"}, ...]
    formula_display: Optional[str] = ""
    dashboards: List[str] = []     # ["clients", "campaigns", "leads"]
    format_type: Optional[str] = "integer"   # "integer" | "currency" | "percentage" | "decimal"
    aggregation: Optional[str] = "total"     # "total" | "average"


class UpdateCustomMetricRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    formula_parts: Optional[list] = None
    formula_display: Optional[str] = None
    dashboards: Optional[List[str]] = None
    format_type: Optional[str] = None
    aggregation: Optional[str] = None
    enabled: Optional[bool] = None


# ── Birdy AI Chat ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    page: Optional[str] = None
    client_group_id: Optional[str] = None
    client_name: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    tools_used: List[str] = []
    session_id: str = ""


# ── External MCP Access Tokens ────────────────────────────────────────────────

class CreateMcpTokenRequest(BaseModel):
    name: str                          # e.g. "Claude Desktop on my laptop"
    expiry_days: Optional[int] = 365


# ── AI Credentials (BYOK) ───────────────────────────────────────────────────

class SaveAiCredentialRequest(BaseModel):
    provider: str          # "anthropic" | "openai"
    api_key: str
    model: str              # must be one of AVAILABLE_AI_MODELS[provider]


class AiCredentialStatusResponse(BaseModel):
    configured: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    key_preview: Optional[str] = None
    validated: Optional[bool] = None
    validated_at: Optional[str] = None


class AiModelOption(BaseModel):
    id: str
    label: str


class AiModelsResponse(BaseModel):
    anthropic: List[AiModelOption]
    openai: List[AiModelOption]