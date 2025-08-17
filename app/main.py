# FastAPI gutter-only EagleView service
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, uuid, httpx

EV_BASE = os.getenv("EV_BASE", "https://api.eagleview.com")
EV_CLIENT_ID = os.getenv("EV_CLIENT_ID")
EV_CLIENT_SECRET = os.getenv("EV_CLIENT_SECRET")

app = FastAPI(title="Gutter API")

# Let anything call us while you test. Later, lock this to your app/domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Gutter API is running"}

@app.get("/health")
def health():
    return {"ok": True}

# --- EagleView auth (client credentials) ---
_token_cache = {"access_token": None}

async def get_token():
    if _token_cache["access_token"]:
        return _token_cache["access_token"]
    if not (EV_CLIENT_ID and EV_CLIENT_SECRET):
        raise HTTPException(500, "EagleView credentials not set")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{EV_BASE}/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": EV_CLIENT_ID,
            "client_secret": EV_CLIENT_SECRET
        })
        if r.status_code >= 400:
            raise HTTPException(r.status_code, f"Auth failed: {r.text}")
        _token_cache["access_token"] = r.json().get("access_token")
        return _token_cache["access_token"]

# --- Data models ---
class GutterOrder(BaseModel):
    address1: str
    city: str
    state: str
    postal_code: str
    country: str = "US"
    options: dict = {}  # e.g. {"size":"6in","material":"aluminum"}

# --- Place a GutterReport order ---
@app.post("/orders")
async def create_order(body: GutterOrder):
    token = await get_token()
    payload = {
        "orderReference": str(uuid.uuid4()),
        "location": {
            "addressLine1": body.address1,
            "city": body.city,
            "state": body.state,
            "postalCode": body.postal_code,
            "country": body.country
        },
        "products": [{"type": "GUTTER"}],
        "metadata": {"options": body.options}
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{EV_BASE}/measurement-orders/v1/orders",
                         json=payload,
                         headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()

# --- Check order status ---
@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    token = await get_token()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{EV_BASE}/measurement-orders/v1/orders/{order_id}",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()

# --- Get results + estimate ---
@app.get("/orders/{order_id}/results")
async def get_results(order_id: str):
    token = await get_token()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{EV_BASE}/measurement-orders/v1/orders/{order_id}/results",
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 404:
            raise HTTPException(404, "Results not ready yet")
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        data = r.json()

    gutter = data.get("gutterReport", {}) or {}
    totals = {
        "eave_linear_ft": gutter.get("totalEaveLengthFt") or 0.0,
        "downspouts": gutter.get("estimatedDownspouts") or 0,
        "miters_inside": (gutter.get("miterCount") or {}).get("inside90") or 0,
        "miters_outside": (gutter.get("miterCount") or {}).get("outside90") or 0,
        "stories_by_direction": gutter.get("storiesByDirection") or {},
        "pdf_url": (gutter.get("assets") or {}).get("pdfUrl")
    }

    # --- super simple estimator (edit prices later or load from DB) ---
    pricing = {
        "price_per_ft": 8.50,       # materials per linear foot
        "labor_per_ft": 5.00,       # labor per linear foot
        "downspout_each": 85.00,
        "miter_each": 12.00,
        "pitch_multiplier": 1.10,
        "story_multiplier": 1.00,
        "overhead_pct": 0.12,
        "profit_pct": 0.10,
        "sales_tax_pct": 0.00
    }

    # bump labor 10% if any side has 2+ stories
    if any(((v or 1) >= 2) for v in totals["stories_by_direction"].values()):
        pricing["story_multiplier"] = 1.10

    eave_ft = float(totals["eave_linear_ft"])
    ds = int(totals["downspouts"])
    miters = int(totals["miters_inside"]) + int(totals["miters_outside"])

    base_mat = eave_ft * pricing["price_per_ft"]
    base_lab = eave_ft * pricing["labor_per_ft"] * pricing["pitch_multiplier"] * pricing["story_multiplier"]
    accessories = ds * pricing["downspout_each"] + miters * pricing["miter_each"]
    subtotal = base_mat + base_lab + accessories
    overhead = subtotal * pricing["overhead_pct"]
    pre_profit = subtotal + overhead
    profit = pre_profit * pricing["profit_pct"]
    sales_tax = (base_mat + accessories) * pricing["sales_tax_pct"]
    total = pre_profit + profit + sales_tax

    return {
        "inputs": totals,
        "line_items": [
            {"label": "Gutter materials", "qty": round(eave_ft,1), "uom": "ft", "unit_price": pricing["price_per_ft"], "amount": round(base_mat,2)},
            {"label": "Labor", "qty": round(eave_ft,1), "uom": "ft", "unit_price": pricing["labor_per_ft"], "amount": round(base_lab,2)},
            {"label": f"Downspouts ({ds})", "qty": ds, "uom": "ea", "unit_price": pricing["downspout_each"], "amount": round(ds*pricing["downspout_each"],2)},
            {"label": f"Miters ({miters})", "qty": miters, "uom": "ea", "unit_price": pricing["miter_each"], "amount": round(miters*pricing["miter_each"],2)},
            {"label": "Overhead", "amount": round(overhead,2)},
            {"label": "Profit", "amount": round(profit,2)},
            {"label": "Sales tax", "amount": round(sales_tax,2)}
        ],
        "totals": {"subtotal": round(subtotal,2), "total": round(total,2)}
    }

# --- Optional: webhook endpoint (register this URL in EagleView portal) ---
@app.post("/webhooks/eagleview")
async def webhook_ev(req: Request):
    payload = await req.json()
    # TODO: verify signature if EagleView provides one; update your DB, etc.
    return {"ok": True}
