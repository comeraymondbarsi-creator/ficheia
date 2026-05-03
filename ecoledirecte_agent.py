"""
FicheIA — Agent École Directe
Utilise requests pour appeler directement l'API École Directe en HTTP.
"""

import json
import re as _re
import base64
import argparse
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
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
    mois_nom = MOIS_FR.get(str(mois).lstrip("0") or "0", str(mois))
    reponses = [
        str(jour).lstrip("0"),
        str(jour),
        mois_nom,
        str(annee),
        classe,
        classe.replace("è", "e").replace("é", "e"),
        prof.lower(),
        nom.upper(),
        nom.lower(),
        prenom.lower(),
        prenom.encode("ascii", "ignore").decode().lower(),
        identifiant.lower(),
    ]
    return list(dict.fromkeys(r for r in reponses if r))


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


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.ecoledirecte.com",
        "Referer": "https://www.ecoledirecte.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    })
    return s


def _api_post(session: requests.Session, url: str, body: dict | None, token: str = "") -> dict:
    headers = {}
    if token:
        headers["X-Token"] = token
    payload = urlencode({"data": json.dumps(body or {})})
    resp = session.post(url, data=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────
# CONNEXION
# ─────────────────────────────────────────────

def login(session: requests.Session, identifiant: str, mot_de_passe: str, qcm_reponses: list[str]) -> dict:
    """
    Connecte l'utilisateur via l'API École Directe.
    Résout automatiquement le QCM grâce à qcm_reponses.
    Retourne les infos du compte (token, eleve_id, prenom, nom, classe).
    """
    url = f"{BASE_URL}/login.awp?v={API_VERSION}"
    data  = _api_post(session, url, {
        "identifiant": identifiant,
        "motdepasse":  mot_de_passe,
        "isRelogin":   False,
    })
    code  = data.get("code")
    token = data.get("token") or data.get("data", {}).get("token", "")

    if code == 250:
        # QCM anti-bot : on choisit la bonne proposition
        for tentative in range(6):
            propositions = data.get("data", {}).get("propositions", [])
            choix = None
            for prop in propositions:
                libelle = prop.get("libelle", "")
                for rep in qcm_reponses:
                    if rep.lower() in libelle.lower():
                        choix = prop.get("id") or prop.get("idProposition")
                        break
                if choix:
                    break

            if not choix:
                labels = [p.get("libelle", "") for p in propositions]
                raise Exception(
                    f"Aucune option QCM reconnue (tentative {tentative + 1}). "
                    f"Propositions : {labels[:15]}"
                )

            auth_url = f"{BASE_URL}/doubleauth.awp?verbe=post&v={API_VERSION}"
            data  = _api_post(session, auth_url, {"choix": str(choix)}, token=token)
            code  = data.get("code")
            token = data.get("token") or data.get("data", {}).get("token") or token

            if code == 200:
                break
            elif code == 250:
                continue
            else:
                raise Exception(f"Échec QCM : {data.get('message', 'Erreur inconnue')}")

    if code != 200:
        raise Exception(f"Connexion échouée : {data.get('message', 'Erreur inconnue')}")

    raw_data = data.get("data") or {}
    accounts = raw_data.get("accounts") or []
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

def get_cahier_de_texte(session: requests.Session, infos: dict) -> list[dict]:
    """Récupère les cours des 3 dernières semaines via l'API HTTP."""
    token    = infos["token"]
    eleve_id = infos["eleve_id"]
    today    = datetime.today()
    lundi    = today - timedelta(days=today.weekday() + 14)

    cours_list = []
    for i in range(21):
        date_str = (lundi + timedelta(days=i)).strftime("%Y-%m-%d")
        url = f"{BASE_URL}/Eleves/{eleve_id}/cahierdetexte/{date_str}.awp?verbe=get&v={API_VERSION}"
        try:
            data = _api_post(session, url, None, token=token)
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
    qcm_reponses = build_qcm_reponses(jour, mois, annee, classe, prof)
    session      = _make_session()

    infos      = login(session, identifiant, mot_de_passe, qcm_reponses)
    cours_list = get_cahier_de_texte(session, infos)

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
    parser.add_argument("--jour",        required=True)
    parser.add_argument("--mois",        required=True)
    parser.add_argument("--annee",       required=True)
    parser.add_argument("--classe",      required=True)
    parser.add_argument("--prof",        required=True)
    parser.add_argument("--api-key",     required=True, dest="api_key")
    parser.add_argument("--cours",       nargs="*", type=int, default=None)
    args = parser.parse_args()

    result = run_agent(
        identifiant       = args.identifiant,
        mot_de_passe      = args.mdp,
        jour              = args.jour,
        mois              = args.mois,
        annee             = args.annee,
        classe            = args.classe,
        prof              = args.prof,
        anthropic_api_key = args.api_key,
        indices_cours     = args.cours,
    )

    infos      = result["infos"]
    cours_list = result["cours_list"]
    fiches     = result["fiches"]

    print(f"Connecté : {infos['prenom']} {infos['nom']} — {infos['classe']}")
    print(f"{len(cours_list)} cours récupérés, {len(fiches)} fiche(s) générée(s)\n")

    for entry in fiches:
        c   = entry["cours"]
        nom = f"fiche_{c['matiere'].replace(' ', '_')}_{c['date']}.txt"
        with open(nom, "w", encoding="utf-8") as f:
            f.write(f"FICHE DE RÉVISION — {c['matiere'].upper()}\n")
            f.write(f"Date du cours : {c['date']}\n")
            f.write(f"Élève : {infos['prenom']} {infos['nom']} — {infos['classe']}\n")
            f.write("═" * 50 + "\n\n")
            f.write(entry["texte"])
        print(f"{nom}")
        print(entry["texte"][:300] + "...\n")


if __name__ == "__main__":
    main()
