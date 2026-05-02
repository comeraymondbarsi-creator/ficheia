"""
FicheIA — Agent École Directe
Utilise Playwright (vrai navigateur Chromium) pour contourner la protection
anti-bot d'École Directe, puis génère des fiches de révision via Claude.

Installation :
    pip install playwright anthropic fastapi uvicorn
    playwright install chromium

Usage CLI :
    python ecoledirecte_agent.py --identifiant ComeRB --mdp MonMdp \
        --jour 19 --mois janvier --annee 2011 --classe 3eme3 --prof olivero \
        --api-key sk-ant-...
"""

import re as _re
import base64
import argparse
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
import anthropic

BASE_URL    = "https://api.ecoledirecte.com/v3"
API_VERSION = "4.99.3"

MOIS_FR = {
    "1": "janvier", "01": "janvier",
    "2": "février",  "02": "février",
    "3": "mars",     "03": "mars",
    "4": "avril",    "04": "avril",
    "5": "mai",      "05": "mai",
    "6": "juin",     "06": "juin",
    "7": "juillet",  "07": "juillet",
    "8": "août",     "08": "août",
    "9": "septembre","09": "septembre",
    "10": "octobre",
    "11": "novembre",
    "12": "décembre",
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def build_qcm_reponses(
    jour: str, mois: str, annee: str, classe: str, prof: str,
    nom: str = "", prenom: str = "", identifiant: str = "",
) -> list[str]:
    """Construit la liste des réponses possibles au QCM depuis les infos utilisateur."""
    mois_nom = MOIS_FR.get(str(mois).lstrip("0") or "0", str(mois))
    reponses = [
        str(jour).lstrip("0"),
        str(jour),
        mois_nom,
        str(annee),
        classe,
        classe.replace("è", "e").replace("é", "e"),
        prof.lower(),
        nom.upper(),                          # "BREBANT"
        nom.lower(),                          # "brebant"
        prenom.lower(),                       # "côme" ou "come"
        prenom.encode("ascii", "ignore").decode().lower(),  # sans accents
        identifiant.lower(),                  # "comerb"
    ]
    return list(dict.fromkeys(r for r in reponses if r))  # déduplique, ordre conservé


def _strip_html(text: str) -> str:
    text = _re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return " ".join(text.split())


def _decode_b64(content) -> str:
    if not content or not isinstance(content, str):
        return ""
    try:
        texte = base64.b64decode(content).decode("utf-8")
        texte = texte.replace("<br/>", "\n").replace("<br>", "\n")
        texte = "".join(c for c in texte if c.isprintable() or c == "\n")
    except Exception:
        texte = content
    return _strip_html(texte) if "<" in texte else texte


def _api_post(page, url: str, payload: str, token: str = "") -> dict:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.ecoledirecte.com",
        "Referer": "https://www.ecoledirecte.com/",
    }
    if token:
        headers["X-Token"] = token

    return page.evaluate("""
        async ([url, payload, headers]) => {
            const resp = await fetch(url, {
                method: "POST",
                headers: headers,
                body: payload,
            });
            return await resp.json();
        }
    """, [url, payload, headers])


# ─────────────────────────────────────────────
# CONNEXION
# ─────────────────────────────────────────────

def login(page, identifiant: str, mot_de_passe: str, qcm_reponses: list[str]) -> dict:
    """
    Connecte l'utilisateur sur École Directe via Playwright.
    Résout automatiquement le QCM grâce à qcm_reponses.
    Retourne les infos du compte (token, eleve_id, prenom, nom, classe).
    """
    intercepted = {}

    def capturer(response):
        if "api.ecoledirecte.com" not in response.url:
            return
        try:
            body = response.json()
        except Exception:
            return
        code = body.get("code")
        if "login.awp" in response.url and code in (200, 250):
            intercepted.update(body)
        elif "doubleauth.awp" in response.url and "verbe=post" in response.url and code == 200:
            if body.get("data", {}).get("token"):
                intercepted["code"]  = 200
                intercepted["token"] = body["data"]["token"]
                intercepted["data"]  = body.get("data", {})

    page.on("response", capturer)

    page.goto("https://www.ecoledirecte.com/login", wait_until="load", timeout=60000)
    page.wait_for_selector("input#username", timeout=15000)
    page.wait_for_timeout(1000)

    page.fill("input#username", identifiant)
    page.fill("input#password", mot_de_passe)
    page.click('button[type="submit"]')
    page.wait_for_timeout(4000)

    code = intercepted.get("code")
    if not code:
        raise Exception("Aucune réponse de l'API après soumission du formulaire.")

    if code == 250:
        for tentative in range(6):
            try:
                page.wait_for_selector("text=CONFIRMEZ VOTRE IDENTITÉ", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(800)

            if "/login" not in page.url:
                break

            cliqué = False
            modal = page.locator(".modal, [class*='modal'], [role='dialog']").first
            labels_loc = modal.locator("label") if modal.count() > 0 else page.locator("label")

            for label in labels_loc.all():
                try:
                    texte = label.inner_text().strip()
                except Exception:
                    continue
                for rep in qcm_reponses:
                    if rep.lower() in texte.lower():
                        label.scroll_into_view_if_needed()
                        page.wait_for_timeout(200)
                        label.click()
                        cliqué = True
                        break
                if cliqué:
                    break

            if not cliqué:
                visible = [l.inner_text().strip() for l in page.locator("label").all() if l.inner_text().strip()]
                raise Exception(f"Aucune option QCM reconnue (tentative {tentative + 1}). Labels visibles : {visible[:15]}")

            page.locator('button:has-text("Envoyer ma réponse")').first.click()

            for _ in range(12):
                page.wait_for_timeout(1000)
                if "/login" not in page.url:
                    break
                if page.locator("text=CONFIRMEZ VOTRE IDENTITÉ").count() == 0:
                    page.wait_for_timeout(2000)
                    break

        if "/login" not in page.url:
            for _ in range(8):
                if intercepted.get("code") == 200 and intercepted.get("data", {}).get("accounts"):
                    break
                page.wait_for_timeout(1000)
            if "/login" not in page.url:
                intercepted["code"] = 200

    if intercepted.get("code") != 200:
        raise Exception(f"Connexion échouée : {intercepted.get('message', 'Erreur inconnue')}")

    data     = intercepted.get("data") or {}
    token    = intercepted.get("token") or data.get("token")
    accounts = data.get("accounts") or []
    account  = next((a for a in accounts if a.get("typeCompte") == "E"), None)
    if not account and accounts:
        account = accounts[0]

    if not account or not token:
        raise Exception("Impossible de récupérer les informations du compte après connexion.")

    return {
        "token":    token,
        "eleve_id": account["id"],
        "prenom":   account["prenom"],
        "nom":      account["nom"],
        "classe":   account.get("profile", {}).get("classe", {}).get("libelle", ""),
    }


# ─────────────────────────────────────────────
# CAHIER DE TEXTE
# ─────────────────────────────────────────────

def get_cahier_de_texte(page, infos: dict) -> list[dict]:
    """Récupère les cours des 3 dernières semaines. Retourne une liste de cours."""
    token    = infos["token"]
    eleve_id = infos["eleve_id"]
    today    = datetime.today()
    lundi    = today - timedelta(days=today.weekday() + 14)

    cours_list = []
    for i in range(21):
        date_str = (lundi + timedelta(days=i)).strftime("%Y-%m-%d")
        url = f"{BASE_URL}/Eleves/{eleve_id}/cahierdetexte/{date_str}.awp?verbe=get&v={API_VERSION}"
        try:
            data = _api_post(page, url, "data={}", token=token)
        except Exception:
            continue

        if data.get("code") != 200 or not data.get("data"):
            continue

        raw      = data["data"]
        matieres = raw.get("matieres") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])

        for cours in (matieres or []):
            contenu        = _decode_b64(cours.get("contenuDeSeance", ""))
            devoirs        = cours.get("aFaire") or {}
            contenu_devoir = _decode_b64(devoirs.get("contenu", ""))

            cours_list.append({
                "date":        date_str,
                "matiere":     cours.get("matiere", ""),
                "prof":        cours.get("nomProf", ""),
                "contenu":     contenu,
                "devoir":      contenu_devoir,
                "date_devoir": devoirs.get("donneLe", ""),
                "fichiers":    cours.get("fichiers", []),
            })

    return cours_list


# ─────────────────────────────────────────────
# GÉNÉRATION DE FICHE
# ─────────────────────────────────────────────

def generer_fiche(cours: dict, infos: dict, anthropic_api_key: str) -> str:
    """Génère une fiche de révision via Claude. Retourne le texte de la fiche."""
    contenu = cours["contenu"] or "Contenu non disponible"
    if cours["devoir"]:
        contenu += f"\n\nDevoirs associés :\n{cours['devoir']}"

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    prompt = f"""Tu es un assistant pédagogique. Génère une fiche de révision pour {infos['prenom']}, élève de {infos['classe']}, pour le cours de {cours['matiere']} du {cours['date']}.

Contenu du cours :
\"\"\"
{contenu[:3000]}
\"\"\"

Génère la fiche avec ces 4 sections :

**RÉSUMÉ**
Un résumé clair en 3-5 phrases.

**POINTS CLÉS**
- Point 1
- Point 2
(6 à 8 points essentiels)

**FLASHCARDS**
Q: [question] | R: [réponse courte]
(4 flashcards)

**QCM**
Q: [question]
A) [option] B) [option] C) [option] D) [option]
Bonne réponse: [lettre]
(3 questions)

Réponds directement sans introduction."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ─────────────────────────────────────────────
# ORCHESTRATEUR PRINCIPAL (appelé par FastAPI)
# ─────────────────────────────────────────────

def run_agent(
    identifiant: str,
    mot_de_passe: str,
    jour: str,
    mois: str,
    annee: str,
    classe: str,
    prof: str,
    anthropic_api_key: str,
    indices_cours: list[int] | None = None,
) -> dict:
    """
    Point d'entrée principal pour FastAPI (et les tests).

    Paramètres :
        identifiant / mot_de_passe : credentials École Directe
        jour / mois / annee        : date de naissance pour le QCM
        classe / prof              : infos supplémentaires pour le QCM
        anthropic_api_key          : clé API Claude
        indices_cours              : liste d'indices à ficher ; None = tous les cours avec contenu

    Retourne :
        {
          "infos":      { prenom, nom, classe, ... },
          "cours_list": [ { date, matiere, prof, contenu, devoir, ... }, ... ],
          "fiches":     [ { cours: {...}, texte: "..." }, ... ],
        }
    """
    qcm_reponses = build_qcm_reponses(jour, mois, annee, classe, prof)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        infos      = login(page, identifiant, mot_de_passe, qcm_reponses)
        cours_list = get_cahier_de_texte(page, infos)

        browser.close()

    if indices_cours is None:
        selection = [c for c in cours_list if c["contenu"] or c["devoir"]]
    else:
        selection = [cours_list[i] for i in indices_cours if i < len(cours_list)]

    fiches = []
    for cours in selection:
        if not cours["contenu"] and not cours["devoir"]:
            continue
        texte = generer_fiche(cours, infos, anthropic_api_key)
        fiches.append({"cours": cours, "texte": texte})

    return {"infos": infos, "cours_list": cours_list, "fiches": fiches}


# ─────────────────────────────────────────────
# CLI (usage local / debug)
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FicheIA — Agent École Directe")
    parser.add_argument("--identifiant", required=True)
    parser.add_argument("--mdp",         required=True)
    parser.add_argument("--jour",        required=True, help="Jour de naissance (ex: 19)")
    parser.add_argument("--mois",        required=True, help="Mois de naissance en français (ex: janvier)")
    parser.add_argument("--annee",       required=True, help="Année de naissance (ex: 2011)")
    parser.add_argument("--classe",      required=True, help="Libellé de classe (ex: 3eme3)")
    parser.add_argument("--prof",        required=True, help="Nom du professeur principal (ex: olivero)")
    parser.add_argument("--api-key",     required=True, dest="api_key")
    parser.add_argument("--cours",       nargs="*", type=int, default=None,
                        help="Indices des cours à ficher (ex: 0 1 3). Omis = tous.")
    args = parser.parse_args()

    print("🎓 FicheIA — Agent École Directe\n")

    result = run_agent(
        identifiant      = args.identifiant,
        mot_de_passe     = args.mdp,
        jour             = args.jour,
        mois             = args.mois,
        annee            = args.annee,
        classe           = args.classe,
        prof             = args.prof,
        anthropic_api_key= args.api_key,
        indices_cours    = args.cours,
    )

    infos      = result["infos"]
    cours_list = result["cours_list"]
    fiches     = result["fiches"]

    print(f"✅ Connecté : {infos['prenom']} {infos['nom']} — {infos['classe']}")
    print(f"📚 {len(cours_list)} cours récupérés, {len(fiches)} fiche(s) générée(s)\n")

    for entry in fiches:
        c   = entry["cours"]
        nom = f"fiche_{c['matiere'].replace(' ', '_')}_{c['date']}.txt"
        with open(nom, "w", encoding="utf-8") as f:
            f.write(f"FICHE DE RÉVISION — {c['matiere'].upper()}\n")
            f.write(f"Date du cours : {c['date']}\n")
            f.write(f"Élève : {infos['prenom']} {infos['nom']} — {infos['classe']}\n")
            f.write("═" * 50 + "\n\n")
            f.write(entry["texte"])
        print(f"✅ {nom}")
        print(entry["texte"][:300] + "...\n")


if __name__ == "__main__":
    main()
