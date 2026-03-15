from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import gspread
from google.oauth2.service_account import Credentials
import httpx
from bs4 import BeautifulSoup
import os
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SHEET_ID = "14AgTeaLDsXNA9NB2Si4HvRr2LtJT4pepZljAp8gVYbQ"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDS_JSON non configuré")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

# ── Modèles ────────────────────────────────────────────────

class Ingredient(BaseModel):
    nom: str
    quantite: Optional[str] = ""
    unite: Optional[str] = ""

class RecetteCreate(BaseModel):
    titre: str
    saison: Optional[str] = "toute saison"
    categorie: Optional[str] = ""
    portions: Optional[str] = ""
    source_type: Optional[str] = "manuel"
    source_ref: Optional[str] = "saisie manuelle"
    feuille: Optional[str] = "Manuel"
    ingredients: List[Ingredient] = []

class RecetteMeta(BaseModel):
    saison: Optional[str] = None
    categorie: Optional[str] = None

# ── Routes ─────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "recettes-backend"}

@app.post("/recettes")
def add_recette(recette: RecetteCreate):
    sh = get_sheet()
    ws_r = sh.worksheet("recettes")
    ws_i = sh.worksheet("ingredients")

    # Trouver le prochain ID
    all_ids = ws_r.col_values(1)[1:]
    numeric_ids = [int(x) for x in all_ids if x.isdigit()]
    new_id = max(numeric_ids, default=0) + 1

    # Ajouter la recette
    ws_r.append_row([
        new_id, recette.titre, recette.feuille, recette.saison,
        recette.source_type, recette.source_ref, recette.portions, recette.categorie
    ])

    # Ajouter les ingrédients
    if recette.ingredients:
        ing_rows = [[new_id, i.nom, i.quantite, i.unite] for i in recette.ingredients]
        ws_i.append_rows(ing_rows)

    return {"id": new_id, "titre": recette.titre}

@app.patch("/recettes/{recette_id}")
def update_recette(recette_id: int, meta: RecetteMeta):
    sh = get_sheet()
    ws_r = sh.worksheet("recettes")

    ids = ws_r.col_values(1)
    try:
        row_idx = ids.index(str(recette_id)) + 1
    except ValueError:
        raise HTTPException(status_code=404, detail="Recette non trouvée")

    if meta.saison is not None:
        ws_r.update_cell(row_idx, 4, meta.saison)
    if meta.categorie is not None:
        ws_r.update_cell(row_idx, 8, meta.categorie)

    return {"id": recette_id, "updated": True}

@app.delete("/recettes/{recette_id}")
def delete_recette(recette_id: int):
    sh = get_sheet()
    ws_r = sh.worksheet("recettes")
    ws_i = sh.worksheet("ingredients")

    # Supprimer la ligne recette
    ids = ws_r.col_values(1)
    try:
        row_idx = ids.index(str(recette_id)) + 1
    except ValueError:
        raise HTTPException(status_code=404, detail="Recette non trouvée")
    ws_r.delete_rows(row_idx)

    # Supprimer les ingrédients associés
    ing_ids = ws_i.col_values(1)
    rows_to_delete = [i + 1 for i, v in enumerate(ing_ids) if v == str(recette_id)]
    for row in reversed(rows_to_delete):
        ws_i.delete_rows(row)

    return {"id": recette_id, "deleted": True}

class CourseItem(BaseModel):
    nom: str
    details: List[str]

class NormaliserRequest(BaseModel):
    items: List[CourseItem]

@app.post("/normaliser")
async def normaliser_courses(req: NormaliserRequest):
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY non configuré")

    lignes = "\n".join(f"{item.nom} : {', '.join(item.details)}" for item in req.items)

    prompt = f"""Voici une liste de courses brute extraite de plusieurs recettes de cuisine.
Regroupe les ingrédients qui sont les mêmes mais écrits différemment (variantes orthographiques, singulier/pluriel, accents).
Par exemple : "oeuf" et "oeufs" → "oeufs", "maizéna" et "maïzena" → "maïzena".
Ne regroupe PAS des ingrédients différents (ex: "lait" et "lait sans lactose" restent séparés).
Pour chaque ingrédient regroupé, conserve toutes les quantités et recettes associées.
Réponds UNIQUEMENT avec un JSON valide, sans markdown, sans explication, sous cette forme exacte :
[{{"nom":"nom normalisé","details":["quantité (recette)","quantité (recette)"]}}]

Liste brute :
{lignes}"""

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "temperature": 0, "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
        )
        if not res.is_success:
            raise HTTPException(status_code=502, detail=f"Erreur Groq : {res.text}")

    data = res.json()
    text = data["choices"][0]["message"]["content"].strip()
    # Nettoyer les balises markdown éventuelles
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    print(f"DEBUG Groq response: {repr(text[:500])}")
    try:
        normalized = json.loads(text)
    except json.JSONDecodeError as je:
        raise HTTPException(status_code=502, detail=f"Réponse Groq invalide : {repr(text[:300])}")

    return {"items": normalized}


async def scrape_url(url: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Impossible de charger l'URL : {e}")

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Détection du site ──────────────────────────────────
    if "cookomix.com" in url:
        return _parse_cookomix(soup, url)
    elif "yummix.fr" in url or "yummly.com" in url:
        return _parse_yummix(soup, url)
    else:
        return _parse_generic(soup, url)

def _parse_cookomix(soup, url):
    titre = ""
    h1 = soup.find("h1")
    if h1: titre = h1.get_text(strip=True)

    ingredients = []
    # Cookomix structure : liste d'ingrédients dans des éléments spécifiques
    for item in soup.select(".ingredient, .wprm-recipe-ingredient, li.ingredient"):
        qty_el = item.select_one(".wprm-recipe-ingredient-amount, .qty, .ingredient-amount")
        unit_el = item.select_one(".wprm-recipe-ingredient-unit, .unit, .ingredient-unit")
        name_el = item.select_one(".wprm-recipe-ingredient-name, .ingredient-name") or item
        nom = name_el.get_text(strip=True) if name_el else ""
        qty = qty_el.get_text(strip=True) if qty_el else ""
        unit = unit_el.get_text(strip=True) if unit_el else ""
        if nom:
            ingredients.append({"nom": nom, "quantite": f"{qty} {unit}".strip(), "unite": ""})

    # Fallback : chercher JSON-LD
    if not ingredients:
        ingredients = _extract_jsonld_ingredients(soup)

    return {"titre": titre, "ingredients": ingredients, "source_ref": url, "source_type": "url"}

def _parse_yummix(soup, url):
    titre = ""
    h1 = soup.find("h1")
    if h1: titre = h1.get_text(strip=True)
    ingredients = _extract_jsonld_ingredients(soup)
    return {"titre": titre, "ingredients": ingredients, "source_ref": url, "source_type": "url"}

def _parse_generic(soup, url):
    titre = ""
    h1 = soup.find("h1")
    if h1: titre = h1.get_text(strip=True)
    ingredients = _extract_jsonld_ingredients(soup)
    return {"titre": titre, "ingredients": ingredients, "source_ref": url, "source_type": "url"}

def _extract_jsonld_ingredients(soup):
    """Extrait les ingrédients depuis le JSON-LD Schema.org Recipe"""
    ingredients = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Peut être une liste ou un objet
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Recipe":
                    for ing in item.get("recipeIngredient", []):
                        # Tenter de séparer quantité et nom
                        m = re.match(r'^([\d\/\.,]+\s*(?:g|kg|ml|l|cl|càs|càc|cs|cc|tasse|pincée|boîte|paquet|gousse|tranche)?\s*)?(.+)$', ing.strip(), re.I)
                        if m:
                            ingredients.append({"nom": m.group(2).strip(), "quantite": (m.group(1) or "").strip(), "unite": ""})
                        else:
                            ingredients.append({"nom": ing.strip(), "quantite": "", "unite": ""})
        except Exception:
            continue
    return ingredients
