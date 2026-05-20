from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SystemLogOut(BaseModel):
    id:         int
    created_at: datetime
    level:      str
    category:   str
    actor:      Optional[str]
    action:     str
    subject:    Optional[str]
    detail:     Optional[str]

    model_config = {"from_attributes": True}
