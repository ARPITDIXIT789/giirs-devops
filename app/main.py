from fastapi import FastAPI
import random
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()

@app.get('/')
def root():
    return {'message': 'GIIRS App Running!', 'status': 'healthy'}

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/metrics-test')
def metrics_test():
    x = [random.random() ** 2 for _ in range(10000)]
    return {'status': 'load generated', 'items': len(x)}

Instrumentator().instrument(app).expose(app)
