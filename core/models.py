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
    condition: AlertCondition
    target_group_ids: Optional[List[str]] = []
    notification_channels: Optional[List[str]] = ["in_app"]


class UpdateAlertRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    condition: Optional[AlertCondition] = None
    target_group_ids: Optional[List[str]] = None
    notification_channels: Optional[List[str]] = None
    status: Optional[str] = None


class SnoozeAlertRequest(BaseModel):
    hours: int = 24
