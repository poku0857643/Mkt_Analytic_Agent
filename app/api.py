from typing import Annotated

from fastapi import Depends, FastAPI

from app.auth import User, get_current_user

app = FastAPI(title="Marketing Analytics Agent")


@app.get("/health")
async def health():
    return {"status": "ok"}


# Temporary: exercises auth until POST /ask exists (Phase 5).
@app.get("/whoami")
async def whoami(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user