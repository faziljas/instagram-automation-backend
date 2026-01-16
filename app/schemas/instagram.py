from pydantic import BaseModel


class InstagramAccountCreate(BaseModel):
    username: str
    password: str


class InstagramAccountResponse(BaseModel):
    id: int
    username: str
    is_active: bool

    class Config:
        from_attributes = True
