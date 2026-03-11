"""A small FastAPI app with 3 intentional bugs for demos."""

from fastapi import FastAPI

app = FastAPI()

# In-memory "database"
users: list[dict] = []


# Bug 1: Code injection via eval()
@app.get("/users/search")
def search_users(name: str):
    # VULNERABLE: uses eval() to build filter — allows code injection
    filter_fn = eval(f"lambda u: u['name'] == '{name}'")
    return [u for u in users if filter_fn(u)]


# Bug 2: Missing input validation
@app.post("/users", status_code=201)
def create_user(data: dict):
    user = {
        "id": len(users) + 1,
        "name": data.get("name"),
        "email": data.get("email"),
        "role": data.get("role"),
    }
    users.append(user)
    return user


# Bug 3: Unhandled error in async route
@app.get("/users/{user_id}/profile")
async def get_profile(user_id: int):
    user = next((u for u in users if u["id"] == user_id), None)
    # VULNERABLE: no null check, no try/catch, crashes on missing user
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://api.example.com/profiles/{user['id']}")
        profile = resp.json()
    return {**user, "profile": profile}
