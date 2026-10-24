from fastapi import FastAPI

app = FastAPI(title="Church Roster Bot", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
