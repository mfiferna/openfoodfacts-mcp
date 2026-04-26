import httpx
from typing import Optional
from fastmcp import FastMCP

BASE_URL = "https://search.openfoodfacts.org"

INSTRUCTIONS = """
You help users with **calorie/health tracking** and **cooking recipes** using the Open Food Facts database.

## Primary workflow

1. User names a product → `search_products` to find it and get its barcode
2. Want nutrition facts (calories, macros)? → `get_product_nutrition` with the barcode
3. Want ingredients for a recipe? → `get_product` with the barcode

## Key facts about the data

- Nutrition is always per 100g. When `serving_size` is present, per-serving values are also included.
- `nova` tells you how processed a food is: 1 = whole food, 4 = ultra-processed.
- `nutriscore` is A–E nutritional quality (A = best).
- `ingredients` is the human-readable ingredient list from the product label.
- `allergens` lists allergens (e.g. "milk", "gluten", "nuts").

## When searching

Use plain product names: "greek yogurt", "whole milk", "sourdough bread".
If results are poor, try brand + product: "Kellogg's cornflakes".
"""

mcp = FastMCP(name="OpenFoodFacts", instructions=INSTRUCTIONS)

_client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

_SEARCH_FIELDS = "code,product_name,brands,quantity,nutriscore_grade,nova_group"

_NUTRITION_FIELDS = (
    "code,product_name,brands,quantity,serving_size,"
    "nutriscore_grade,nova_group,nutriments,nutrient_levels"
)

_PRODUCT_FIELDS = (
    "code,product_name,brands,quantity,serving_size,"
    "categories_tags,ingredients_text,allergens_tags,labels_tags,"
    "nutriscore_grade,nova_group,nutriments,nutrient_levels"
)

_NUTRISCORE = {"a": "A - Excellent", "b": "B - Good", "c": "C - Average", "d": "D - Poor", "e": "E - Bad"}
_NOVA = {
    1: "1 - Unprocessed or minimally processed",
    2: "2 - Processed culinary ingredient",
    3: "3 - Processed food",
    4: "4 - Ultra-processed food",
}

_NUTRIENT_MAP = [
    ("energy_kcal", "energy-kcal"),
    ("fat_g", "fat"),
    ("saturated_fat_g", "saturated-fat"),
    ("carbohydrates_g", "carbohydrates"),
    ("sugars_g", "sugars"),
    ("fiber_g", "fiber"),
    ("proteins_g", "proteins"),
    ("salt_g", "salt"),
]


def _name(field) -> Optional[str]:
    """Extract a plain string from a possibly-multilingual dict field."""
    if isinstance(field, dict):
        return field.get("main") or field.get("en") or next(iter(field.values()), None)
    return field or None


def _brand(brands) -> Optional[str]:
    """Normalise brands list or string to a single string."""
    if isinstance(brands, list):
        return ", ".join(brands) if brands else None
    return brands or None


def _clean_tag(tag: str) -> Optional[str]:
    """'en:hazelnut-spreads' → 'hazelnut spreads'. Non-English tags return None."""
    if ":" in tag:
        lang, value = tag.split(":", 1)
        if lang != "en":
            return None
        return value.replace("-", " ")
    return tag.replace("-", " ")


def _clean_tags(tags: list) -> list:
    return [t for raw in tags if (t := _clean_tag(raw)) is not None]


def _top_categories(tags: list, n: int = 3) -> list:
    """Return the n most specific English categories (last in the hierarchy)."""
    english = [t for raw in tags if (t := _clean_tag(raw)) is not None]
    return english[-n:] if english else []


def _extract_nutrients(nutriments: dict, suffix: str) -> dict:
    result = {}
    for label, field in _NUTRIENT_MAP:
        val = nutriments.get(f"{field}_{suffix}")
        if val is not None:
            result[label] = round(val, 1) if isinstance(val, float) else val
    return result


def _fix_nutrient_level_keys(levels: dict) -> dict:
    """Replace hyphens with underscores in nutrient level keys."""
    return {k.replace("-", "_"): v for k, v in levels.items()}


def _scores(p: dict) -> dict:
    out: dict = {}
    if (g := p.get("nutriscore_grade")) and g.lower() in _NUTRISCORE:
        out["nutriscore"] = _NUTRISCORE[g.lower()]
    if n := p.get("nova_group"):
        out["nova"] = _NOVA.get(n, str(n))
    return out


def _not_empty(v) -> bool:
    return v not in (None, "", [], {})


def _http_error(resp: httpx.Response, context: str) -> dict:
    hints = {
        404: f"'{context}' not found. Double-check the barcode or search again.",
        400: "Bad request — check the query or parameters.",
        429: "Rate limited. Wait a moment then retry.",
    }
    return {
        "error": True,
        "status_code": resp.status_code,
        "message": hints.get(resp.status_code, f"API error {resp.status_code}"),
    }


def _resolve_product(data: dict) -> Optional[dict]:
    return data if data.get("code") else None


@mcp.tool
async def search_products(
    q: str,
    page: int = 1,
    page_size: int = 10,
    sort_by: Optional[str] = None,
) -> dict:
    """Find food products by name or keyword.

    Use this first to discover products and obtain their barcodes, then use
    get_product_nutrition or get_product for detailed information.

    Returns a compact list: barcode, name, brand, quantity, nutriscore, nova.

    Args:
        q: Product name or keywords. Examples: "greek yogurt", "whole milk",
           "Kellogg's cornflakes", "dark chocolate 70%".
        page: Page number (starts at 1).
        page_size: Results per page, 1–50. Default 10.
        sort_by: Sort field. Omit to rank by search relevance (recommended).
                 Use "-unique_scans_n" for most popular, "nutriments.sugars_100g" for least sugar first.
    """
    params: dict = {
        "q": q,
        "page": page,
        "page_size": page_size,
        "fields": _SEARCH_FIELDS,
        **({"sort_by": sort_by} if sort_by else {}),
    }

    resp = await _client.get("/search", params=params)
    if not resp.is_success:
        return _http_error(resp, q)

    data = resp.json()
    results = []
    for h in data.get("hits", []):
        item: dict = {
            "barcode": h.get("code"),
            "name": _name(h.get("product_name")),
            "brand": _brand(h.get("brands")),
            "quantity": h.get("quantity"),
            **_scores(h),
        }
        results.append({k: v for k, v in item.items() if _not_empty(v)})

    out: dict = {
        "results": results,
        "total": data.get("count", 0),
        "page": data.get("page"),
        "total_pages": data.get("page_count"),
    }
    if not results:
        out["tip"] = "No results. Try simpler keywords or just the product category (e.g. 'yogurt')."
    return out


@mcp.tool
async def get_product_nutrition(
    barcode: str,
) -> dict:
    """Get complete nutrition facts for a product by barcode.

    Use for calorie counting, macro tracking, or comparing nutritional quality.
    Returns nutrition per 100g. When a serving size is defined, also returns
    per-serving values (e.g. "how many calories in one bowl?").

    Includes: energy (kcal), fat, saturated fat, carbohydrates, sugars, fiber,
    proteins, salt — all per 100g and optionally per serving.
    Also includes nutrient_levels (low/moderate/high) and nutriscore/nova scores.

    Args:
        barcode: Product barcode from search_products, e.g. "3017620422003".
    """
    resp = await _client.get(f"/document/{barcode}", params={"fields": _NUTRITION_FIELDS})
    if not resp.is_success:
        return _http_error(resp, barcode)

    p = _resolve_product(resp.json())
    if not p:
        return {
            "error": True,
            "message": f"No product found for barcode {barcode}.",
            "tip": "Use search_products to find the product and get its barcode.",
        }

    nutriments = p.get("nutriments") or {}
    out: dict = {
        "barcode": p.get("code"),
        "name": _name(p.get("product_name")),
        "brand": _brand(p.get("brands")),
        "quantity": p.get("quantity"),
        "serving_size": p.get("serving_size"),
        **_scores(p),
        "nutrition_per_100g": _extract_nutrients(nutriments, "100g"),
        "nutrient_levels": _fix_nutrient_level_keys(p.get("nutrient_levels") or {}),
    }

    per_serving = _extract_nutrients(nutriments, "serving")
    if per_serving:
        out["nutrition_per_serving"] = per_serving

    return {k: v for k, v in out.items() if _not_empty(v)}


@mcp.tool
async def get_product(
    barcode: str,
) -> dict:
    """Get full product details including ingredients and allergens.

    Use for recipe creation, checking allergens, or when you need the complete
    ingredient breakdown. For nutrition-only queries use get_product_nutrition.

    Returns: ingredients (human-readable text), allergens, labels (organic, vegan…),
    category, full nutrition per 100g and per serving (if available).

    Args:
        barcode: Product barcode from search_products, e.g. "3017620422003".
    """
    resp = await _client.get(f"/document/{barcode}", params={"fields": _PRODUCT_FIELDS})
    if not resp.is_success:
        return _http_error(resp, barcode)

    p = _resolve_product(resp.json())
    if not p:
        return {
            "error": True,
            "message": f"No product found for barcode {barcode}.",
            "tip": "Use search_products to find the product and get its barcode.",
        }

    nutriments = p.get("nutriments") or {}
    out: dict = {
        "barcode": p.get("code"),
        "name": _name(p.get("product_name")),
        "brand": _brand(p.get("brands")),
        "quantity": p.get("quantity"),
        "serving_size": p.get("serving_size"),
        "category": _top_categories(p.get("categories_tags") or []),
        "ingredients": p.get("ingredients_text"),
        "allergens": _clean_tags(p.get("allergens_tags") or []),
        "labels": _clean_tags(p.get("labels_tags") or []),
        **_scores(p),
        "nutrition_per_100g": _extract_nutrients(nutriments, "100g"),
        "nutrient_levels": _fix_nutrient_level_keys(p.get("nutrient_levels") or {}),
    }

    per_serving = _extract_nutrients(nutriments, "serving")
    if per_serving:
        out["nutrition_per_serving"] = per_serving

    return {k: v for k, v in out.items() if _not_empty(v)}


@mcp.tool
async def autocomplete(
    q: str,
    taxonomy_names: str = "category",
    lang: str = "en",
    size: int = 10,
) -> dict:
    """Autocomplete product category and ingredient names.

    Useful when search_products returns no results — find the correct taxonomy
    tag, then search with that tag.

    Args:
        q: Partial text, e.g. "choco", "yogu", "organ".
        taxonomy_names: Taxonomy to search: "category", "ingredient", "label",
            "country", "additive". Comma-separate to search multiple.
        lang: Language for returned labels (default: "en").
        size: Number of suggestions, 1–50 (default: 10).
    """
    params: dict = {
        "q": q,
        "taxonomy_names": taxonomy_names,
        "lang": lang,
        "size": size,
    }
    resp = await _client.get("/autocomplete", params=params)
    if not resp.is_success:
        return _http_error(resp, q)
    return resp.json()


@mcp.prompt
def calorie_tracking_guide(food_item: str) -> str:
    """Step-by-step guide to look up nutrition for calorie/macro tracking.

    Args:
        food_item: The food to look up, e.g. "Greek yogurt", "oat milk", "sourdough bread".
    """
    return f"""Goal: find accurate nutrition data for "{food_item}" for calorie/macro tracking.

Step 1 — Find the product:
  search_products(q="{food_item}", sort_by="-unique_scans_n")
  → Pick the best match (most popular = most likely correct). Note its barcode.

Step 2 — Get nutrition facts:
  get_product_nutrition(barcode="<barcode>")
  → Use nutrition_per_100g for tracking by weight.
  → Use nutrition_per_serving when the user says "one serving", "one cup", etc.

Step 3 — Interpret:
  - energy_kcal = calories
  - proteins_g, fat_g, carbohydrates_g = the three macros
  - fiber_g reduces net carbs: net_carbs = carbohydrates_g - fiber_g
  - nutriscore A/B = good quality; D/E = consider alternatives
  - nova 1/2 = whole/minimally processed; 4 = ultra-processed
"""


@mcp.prompt
def recipe_nutrition_guide(recipe_name: str) -> str:
    """Step-by-step guide to calculate nutrition for a recipe.

    Args:
        recipe_name: The recipe, e.g. "banana oat pancakes", "caesar salad".
    """
    return f"""Goal: gather nutritional data for all ingredients in "{recipe_name}".

For each ingredient:
  1. search_products(q="<ingredient>") → find it, note its barcode
  2. get_product_nutrition(barcode="...") → get nutrition_per_100g

Scale each ingredient by the amount used (grams):
  calories_from_ingredient = (energy_kcal / 100) × grams_used

Sum across all ingredients → total recipe nutrition.
Divide by number of servings → per-serving nutrition.

If you need the full ingredient list of a packaged product (a sauce, yogurt, etc.):
  get_product(barcode="...") → read the `ingredients` field.
"""


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000, stateless_http=True)
