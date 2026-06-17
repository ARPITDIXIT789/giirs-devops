from fastapi import FastAPI
import random

app = FastAPI()

@app.get("/")
def root():
    return {"message": "GIIRS App Running!", "status": "healthy"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/metrics-test")
def metrics_test():
    # Random CPU simulation for Prometheus testing
    x = [random.random() ** 2 for _ in range(10000)]
    return {"status": "load generated", "items": len(x)}
