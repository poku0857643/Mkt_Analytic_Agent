from fastapi import FastAPI

app = FastAPI(title="Marketing Analytics Agent")


@app.get("/health")
async def health():
    return {"status": "ok"}
