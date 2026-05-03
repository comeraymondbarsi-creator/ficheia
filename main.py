"""
FicheIA — Serveur FastAPI
Dev  : uvicorn main:app --reload
Prod : uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os
import uuid

import stripe
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv, find_dotenv
from ecoledirecte_agent import (
    build_qcm_reponses,
    login,
    get_cahier_de_texte,
    generer_fiche,
    _make_session,
)
from database import init_db, get_fiches_count, increment_fiches_count

# Cherche .env dans le dossier du script, puis remonte l'arborescence
_dotenv_path = find_dotenv(
    filename=".env",
    raise_error_if_not_found=False,
    usecwd=True,
) or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_dotenv_path, override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY", "")
APP_BASE_URL      = os.getenv("APP_BASE_URL", "http://localhost:8000")

stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="FicheIA")
app.mount("/static", StaticFiles(directory="static"), name="static")

init_db()

# Sessions en mémoire : session_id → { identifiant, infos, cours_list, paid }
_sessions: dict[str, dict] = {}


# ── Modèles ──────────────────────────────────────────────────────────────────

class ConnexionRequest(BaseModel):
    identifiant: str
    mot_de_passe: str
    jour: str
    mois: str
    annee: str
    classe: str
    prof: str
    nom: str = ""
    prenom: str = ""

class CheckoutRequest(BaseModel):
    app_session_id: str

class VerifyRequest(BaseModel):
    stripe_session_id: str
    app_session_id: str

class FichesRequest(BaseModel):
    session_id: str
    indices: list[int]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.post("/api/cours")
def api_cours(req: ConnexionRequest):
    """Connexion École Directe + récupération du cahier de texte."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY manquante dans .env")

    qcm_reponses = build_qcm_reponses(
        req.jour, req.mois, req.annee, req.classe, req.prof,
        nom=req.nom, prenom=req.prenom, identifiant=req.identifiant,
    )

    try:
        session    = _make_session()
        infos      = login(session, req.identifiant, req.mot_de_passe, qcm_reponses)
        cours_list = get_cahier_de_texte(session, infos)
    except Exception as e:
        raise HTTPException(400, str(e))

    fiches_count = get_fiches_count(req.identifiant)

    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "identifiant": req.identifiant,
        "infos":       infos,
        "cours_list":  cours_list,
        "paid":        False,
    }

    return {
        "session_id":   session_id,
        "infos":        infos,
        "cours_list":   cours_list,
        "fiches_count": fiches_count,
    }


@app.get("/api/session/{session_id}")
def api_get_session(session_id: str):
    """Restaure les données d'une session (utilisé après retour de Stripe)."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session expirée — reconnectez-vous.")
    return {
        "infos":        session["infos"],
        "cours_list":   session["cours_list"],
        "paid":         session["paid"],
        "fiches_count": get_fiches_count(session["identifiant"]),
    }


@app.post("/api/create-checkout-session")
def create_checkout(req: CheckoutRequest):
    """Crée une session Stripe Checkout pour l'abonnement à 9,90€/mois."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "STRIPE_SECRET_KEY manquante dans .env")
    if req.app_session_id not in _sessions:
        raise HTTPException(404, "Session expirée — reconnectez-vous.")

    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "FicheIA — Abonnement mensuel"},
                    "unit_amount": 990,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=(
                f"{APP_BASE_URL}/?paid={{CHECKOUT_SESSION_ID}}"
                f"&app={req.app_session_id}"
            ),
            cancel_url=(
                f"{APP_BASE_URL}/?cancelled=true&app={req.app_session_id}"
            ),
            metadata={"app_session_id": req.app_session_id},
        )
    except stripe.StripeError as e:
        raise HTTPException(400, str(e))

    return {"checkout_url": checkout.url}


@app.post("/api/verify-payment")
def verify_payment(req: VerifyRequest):
    """Vérifie le paiement Stripe et marque la session comme payée."""
    try:
        checkout = stripe.checkout.Session.retrieve(req.stripe_session_id)
    except stripe.StripeError as e:
        raise HTTPException(400, str(e))

    if checkout.payment_status not in ("paid", "no_payment_required"):
        raise HTTPException(402, "Paiement non confirmé.")

    session = _sessions.get(req.app_session_id)
    if not session:
        raise HTTPException(404, "Session expirée — reconnectez-vous.")

    session["paid"] = True
    return {"paid": True}


FREE_LIMIT = 5

@app.post("/api/fiches")
def api_fiches(req: FichesRequest):
    """
    Génère les fiches sélectionnées.
    - Les 5 premières fiches sont gratuites, comptabilisées par identifiant
      École Directe (persisté en base, résistant aux nouvelles sessions).
    - À partir de la 6ème, un abonnement actif est requis.
    - Si la limite est atteinte en cours de batch, les fiches déjà générées
      sont retournées avec paywall_reached=True.
    """
    session = _sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session expirée — reconnectez-vous.")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY manquante dans .env")

    identifiant  = session["identifiant"]
    paid         = session["paid"]
    fiches_count = get_fiches_count(identifiant)  # source de vérité : DB

    if not paid and fiches_count >= FREE_LIMIT:
        raise HTTPException(402, "Limite gratuite atteinte — abonnement requis.")

    infos      = session["infos"]
    cours_list = session["cours_list"]

    fiches          = []
    generated       = 0
    paywall_reached = False

    for i in req.indices:
        if i >= len(cours_list):
            continue

        if not paid and (fiches_count + generated) >= FREE_LIMIT:
            paywall_reached = True
            break

        cours = cours_list[i]
        if not cours["contenu"] and not cours["devoir"]:
            fiches.append({"cours": cours, "texte": None, "erreur": "Aucun contenu disponible."})
            continue

        try:
            texte = generer_fiche(cours, infos, ANTHROPIC_API_KEY)
            fiches.append({"cours": cours, "texte": texte, "erreur": None})
            generated += 1
        except Exception as e:
            fiches.append({"cours": cours, "texte": None, "erreur": str(e)})

    # Persistance en base si des fiches ont été générées
    if generated > 0:
        fiches_count = increment_fiches_count(identifiant, generated)
    else:
        fiches_count = fiches_count + generated  # pas de changement

    return {
        "fiches":          fiches,
        "fiches_count":    fiches_count,
        "free_remaining":  max(0, FREE_LIMIT - fiches_count) if not paid else None,
        "paywall_reached": paywall_reached,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
