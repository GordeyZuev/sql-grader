import uvicorn

uvicorn.run("cabinet.api:app", host="127.0.0.1", port=8000, reload=True)
