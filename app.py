import os
import re
from datetime import date, datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

EXA_API_KEY = os.getenv("EXA_API_KEY")
CONVEX_URL = os.getenv("CONVEX_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = FastAPI(title="ShelfSense")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "https://shelf-sense-sand.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


class Product(BaseModel):
    item_name: str
    expiry_date: str  # YYYY-MM-DD
    quantity: int
    barcode: Optional[str] = None


async def save_to_convex(data: dict) -> dict:
    """Send a POST request to the Convex backend to save a product."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CONVEX_URL}/api/mutation",
            json={"path": "products:add", "args": data},
        )
        response.raise_for_status()
        return response.json()


async def get_all_products() -> list[dict]:
    """Fetch all products from the Convex backend."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{CONVEX_URL}/api/query",
            json={"path": "products:getAll", "args": {}},
        )
        response.raise_for_status()
        return response.json()["value"]


def get_status(expiry_date_str: str) -> tuple[int, str]:
    """Return (days_left, color) for a given expiry date string (YYYY-MM-DD).

    Colors: "expired", "red" (<3 days), "yellow" (<7 days), "green" (>=7 days).
    """
    today = date.today()
    try:
        expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        expiry = today
    days_left = (expiry - today).days
    if days_left < 0:
        return days_left, "expired"
    if days_left < 3:
        return days_left, "red"
    if days_left < 7:
        return days_left, "yellow"
    return days_left, "green"


def suggest_discount(days_left: int) -> str:
    if days_left <= 0:
        return "Do not sell - Expired"
    if days_left <= 2:
        return "50% off - urgent"
    if days_left <= 4:
        return "30% off"
    if days_left <= 7:
        return "10% off"
    return "No discount"


def enrich_products(products: list[dict]) -> list[dict]:
    """Add days_left, status, and discount to each product using get_status."""
    enriched = []
    for p in products:
        days_left, status = get_status(p.get("expiry_date", ""))
        discount = suggest_discount(days_left)
        enriched.append({**p, "days_left": days_left, "status": status, "discount": discount})
    return enriched


def compute_stats(products: list[dict]) -> dict:
    total = len(products)
    expired = sum(1 for p in products if p["status"] == "expired")
    expiring_soon = sum(1 for p in products if p["status"] in ("red", "yellow"))
    return {"total": total, "expiring_soon": expiring_soon, "expired": expired}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        raw_products = await get_all_products()
    except Exception:
        raw_products = []
    products = enrich_products(raw_products)
    stats = compute_stats(products)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "products": products,
            "stats": stats,
            "today": date.today().strftime("%A, %B %d, %Y"),
        },
    )


@app.post("/add-product")
async def add_product(
    item_name: str = Form(...),
    expiry_date: str = Form(...),
    quantity: int = Form(...),
    barcode: str = Form(""),
):
    product_data = {
        "item_name": item_name,
        "expiry_date": expiry_date,
        "quantity": quantity,
    }
    if barcode.strip():
        product_data["barcode"] = barcode.strip()
    await save_to_convex(product_data)
    return RedirectResponse(url="/", status_code=303)


@app.get("/lookup-barcode")
async def lookup_barcode(code: str):
    """Look up a product name by barcode."""
    name = ""

    # 1) Open Food Facts public API
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://world.openfoodfacts.org/api/v2/product/{code}.json"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 1:
                    product = data.get("product", {})
                    name = product.get("product_name") or product.get("generic_name") or ""
    except Exception:
        pass

    if not name and gemini_client:
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=(
                    f"What is the product with barcode/EAN {code}? "
                    "Return ONLY the product name (brand + product + size if known). "
                    "If you don't know, return exactly 'unknown'. "
                    "Return nothing else."
                ),
            )
            raw = response.text.strip().strip('"').strip("'")
            if raw.lower() != "unknown" and len(raw) < 200:
                name = raw
        except Exception:
            pass

    return JSONResponse({"name": name})


def _guess_mime(filename: str, declared: str | None) -> str:
    if declared and declared.startswith("image/"):
        return declared
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp",
    }.get(ext, "image/jpeg")


@app.post("/scan-date")
async def scan_date(file: UploadFile = File(...)):
    if not gemini_client:
        return JSONResponse({"date": "not found", "error": "GEMINI_API_KEY not set in .env"})

    image_bytes = await file.read()
    mime = _guess_mime(file.filename, file.content_type)
    try:
        import base64
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {"inline_data": {"mime_type": mime, "data": b64}},
                (
                    "Look at this image and find the expiry date or best before date. "
                    "Return only the date in YYYY-MM-DD format. "
                    "If you cannot find a date, return the word 'not found'. "
                    "Return nothing else, just the date or 'not found'."
                ),
            ],
        )
        raw = response.text.strip()
        print(f"[scan-date] Gemini raw response: {raw!r}")
        match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
        result = match.group(0) if match else "not found"
    except Exception as e:
        print(f"[scan-date] ERROR: {e}")
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            err_msg = "API rate limit reached. Please wait a minute and try again."
        return JSONResponse({"date": "not found", "error": err_msg})

    return JSONResponse({"date": result})


@app.post("/scan-barcode")
async def scan_barcode(file: UploadFile = File(...)):
    if not gemini_client:
        return JSONResponse({"barcode": "not found", "error": "GEMINI_API_KEY not set in .env"})

    image_bytes = await file.read()
    mime = _guess_mime(file.filename, file.content_type)
    try:
        import base64
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                {"inline_data": {"mime_type": mime, "data": b64}},
                (
                    "Look at this image and find the barcode number (EAN, UPC, or any product barcode). "
                    "Return ONLY the numeric barcode digits. "
                    "If you cannot find a barcode, return the word 'not found'. "
                    "Return nothing else, just the barcode number or 'not found'."
                ),
            ],
        )
        raw = response.text.strip()
        print(f"[scan-barcode] Gemini raw response: {raw!r}")
        match = re.search(r"\d{8,14}", raw)
        result = match.group(0) if match else "not found"
    except Exception as e:
        print(f"[scan-barcode] ERROR: {e}")
        err_msg = str(e)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
            err_msg = "API rate limit reached. Please wait a minute and try again."
        return JSONResponse({"barcode": "not found", "error": err_msg})

    return JSONResponse({"barcode": result})


@app.get("/find-ngos")
async def find_ngos(city: str):
    """Search for food banks / NGOs accepting food donations in a city using Exa."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": EXA_API_KEY, "Content-Type": "application/json"},
            json={
                "query": f"food banks or NGOs accepting food donations in {city} India",
                "numResults": 4,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = [
            {"name": r.get("title", "Unnamed NGO"), "url": r.get("url", "#")}
            for r in data.get("results", [])
        ]
        return JSONResponse({"ngos": results})
