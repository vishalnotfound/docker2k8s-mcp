from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def home():
    print("Hello from FastAPI!")
    return {"message": "Hello from FastAPI!"}


@app.get("/print/{message}")
def print_message(message: str):
    print(message)
    return {"printed": message}
