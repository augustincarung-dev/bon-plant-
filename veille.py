#!/usr/bin/env python3
"""Veille de bons plans : Dealabs (neuf, promos) + eBay (neuf et occasion).

Génère une page web statique (docs/index.html) et un historique (docs/deals.json).
Peut envoyer une notification sur ton téléphone (ntfy.sh) pour les meilleures affaires.
Aucune dépendance externe : Python 3.9+ suffit.

Usage :
    python veille.py          # mode normal
    python veille.py --demo   # génère la page avec des exemples (test hors-ligne)
"""
import base64
import html
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
UA = "Mozilla/5.0 (compatible; VeilleBonsPlans/2.0)"


def log(*args):
    print(*args, flush=True)


def http(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def to_float(s):
    if s is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(s).replace(" ", "").replace(" ", "").replace(" ", ""))
    return float(m.group().replace(",", ".")) if m else None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def motif_debut_de_mot(mot):
    """Reconnaît le mot (ou ses pluriels / variantes) au début d'un mot : 'veste' trouve 'vestes'."""
    return re.compile(r"(?<!\w)" + re.escape(mot.lower()))


# --------------------------------------------------------------------------- Dealabs
def fetch_dealabs(cfg):
    if not cfg.get("actif", True):
        return []
    motifs = [motif_debut_de_mot(m) for m in cfg.get("mots_cles", [])]
    out = []
    for feed in cfg.get("flux", []):
        try:
            root = ET.fromstring(http(feed))
        except Exception as e:  # réseau, flux changé, etc. : on continue
            log(f"[Dealabs] flux ignoré ({feed}) : {e}")
            continue
        for it in root.iter("item"):
            titre = (it.findtext("title") or "").strip()
            lien = (it.findtext("link") or "").strip()
            if not titre or not lien:
                continue
            desc = strip_html(it.findtext("description"))
            prix_txt, marchand = None, None
            for el in it:
                if el.tag.split("}")[-1].lower() == "merchant":
                    marchand = el.attrib.get("name")
                    prix_txt = el.attrib.get("price")
            texte = (titre + " " + desc).lower()
            match = any(m.search(texte) for m in motifs)
            if cfg.get("filtrer_par_mots_cles") and not match:
                continue
            out.append({
                "id": "dealabs:" + lien,
                "source": "Dealabs",
                "titre": titre,
                "url": lien,
                "prix": to_float(prix_txt),
                "prix_txt": prix_txt or "",
                "etat": "Neuf",
                "vendeur": marchand or "",
                "note_vendeur": None,
                "image": "",
                "alerte": match,
                "remise": 0,
                "info": desc[:160],
            })
    log(f"[Dealabs] {len(out)} offres")
    return out


# --------------------------------------------------------------------------- eBay
def ebay_token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    raw = http(
        "https://api.ebay.com/identity/v1/oauth2/token",
        {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        body,
    )
    return json.loads(raw)["access_token"]


def choisir_recherches(recherches, par_passage, maintenant=None):
    """Roulement : à chaque passage (5 min) on traite un sous-ensemble, pour rester sous les quotas eBay
    quel que soit le nombre de recherches. Chaque recherche est rafraîchie régulièrement."""
    if not par_passage or len(recherches) <= par_passage:
        return list(recherches)
    slot = int((maintenant if maintenant is not None else time.time()) // 300)
    debut = (slot * par_passage) % len(recherches)
    return [recherches[(debut + k) % len(recherches)] for k in range(par_passage)]


# --- Score de rentabilité -------------------------------------------------------------------
# Coefficients de 0 à 1 par marque : "revente" = à quel point elle garde sa valeur d'occasion,
# "reputation" = qualité / popularité. Ce sont des ESTIMATIONS subjectives de départ : corrige-les
# ou ajoute tes marques via "marques" dans config.json.
DEFAULT_MARQUES = {
    "patagonia": {"revente": 0.90, "reputation": 0.90},
    "stone island": {"revente": 0.90, "reputation": 0.85},
    "arc'teryx": {"revente": 0.95, "reputation": 0.95},
    "carhartt": {"revente": 0.85, "reputation": 0.85},
    "north face": {"revente": 0.80, "reputation": 0.85},
    "salomon": {"revente": 0.80, "reputation": 0.80},
    "barbour": {"revente": 0.75, "reputation": 0.80},
    "jordan": {"revente": 0.80, "reputation": 0.80},
    "new balance": {"revente": 0.70, "reputation": 0.80},
    "levi": {"revente": 0.70, "reputation": 0.80},
    "nike": {"revente": 0.65, "reputation": 0.80},
    "dr martens": {"revente": 0.65, "reputation": 0.80},
    "longchamp": {"revente": 0.65, "reputation": 0.75},
    "adidas": {"revente": 0.60, "reputation": 0.75},
    "asics": {"revente": 0.60, "reputation": 0.75},
    "ralph lauren": {"revente": 0.60, "reputation": 0.75},
    "birkenstock": {"revente": 0.60, "reputation": 0.75},
    "ray-ban": {"revente": 0.60, "reputation": 0.80},
    "lacoste": {"revente": 0.55, "reputation": 0.75},
    "champion": {"revente": 0.50, "reputation": 0.65},
    "dickies": {"revente": 0.50, "reputation": 0.70},
    "casio": {"revente": 0.50, "reputation": 0.70},
    "converse": {"revente": 0.45, "reputation": 0.70},
    "vans": {"revente": 0.45, "reputation": 0.70},
    "eastpak": {"revente": 0.40, "reputation": 0.60},
}
MARQUE_INCONNUE = {"revente": 0.40, "reputation": 0.50}


def trouver_marque(titre, marques):
    t = (titre or "").lower()
    for nom in sorted(marques, key=len, reverse=True):  # « north face » avant « north »
        if motif_debut_de_mot(nom).search(t):
            return nom, marques[nom]
    return None, MARQUE_INCONNUE


def coef_etat(texte):
    """État annoncé -> coefficient 0..1 (d'après le texte de la condition eBay)."""
    t = (texte or "").lower()
    if re.search(r"pi[eè]ces|d[eé]fectueux", t):
        return 0.2
    if re.search(r"acceptable|correct", t):
        return 0.55
    if re.search(r"tr[eè]s bon|comme neuf|excellent|parfait", t):
        return 0.9
    if re.search(r"\bbon\b", t):
        return 0.75
    if "neuf" in t:
        return 1.0
    return 0.7  # « Occasion » sans précision


def clamp01(x):
    return max(0.0, min(1.0, x))


def calcul_rentabilite(prix, med_occ, ref_neuf, marque, etat, frais_pct=5.0,
                       percentile=None, suspect=False, peu_avis=False):
    """Score 0-100 : plus il est haut, plus l'achat est intéressant (dont en revente).

    Poids : écart vs médiane des annonces (35), marge de revente estimée (25), marque (10),
    état (10), moins cher que X % des annonces (10), économie vs neuf (10, si on connaît un prix neuf).
    La revente est estimée : médiane × décote de marque × état, moins les frais ; ce n'est PAS un prix garanti."""
    if not prix or not med_occ:
        return None
    revente = med_occ * (0.85 + 0.15 * marque["revente"]) * (0.9 + 0.1 * etat)
    marge = revente * (1 - frais_pct / 100.0) - prix
    comps = [
        (35, clamp01((1 - prix / med_occ) / 0.5)),
        (25, clamp01((marge / prix) / 0.6)),
        (10, clamp01(marque["revente"] * 0.6 + marque["reputation"] * 0.4)),
        (10, etat),
    ]
    if percentile is not None:
        comps.append((10, clamp01(percentile / 100.0)))
    if ref_neuf:
        comps.append((10, clamp01((1 - prix / ref_neuf) / 0.8)))
    score = 100.0 * sum(w * c for w, c in comps) / sum(w for w, _ in comps)
    if suspect:
        score = min(score, 30)
    if peu_avis:
        score -= 15
    return {"rentabilite": int(max(0, min(100, round(score)))),
            "revente_est": round(revente), "marge_est": round(marge)}


def age_heures(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        return 1e9


def reference_neuf(token, q):
    """Médiane des prix NEUFS pour cette recherche (sans plafond de prix). None si trop peu d'annonces."""
    params = urllib.parse.urlencode({
        "q": q,
        "filter": "priceCurrency:EUR,deliveryCountry:FR,conditions:{NEW}",
        "limit": "50",
    })
    data = json.loads(http(
        "https://api.ebay.com/buy/browse/v1/item_summary/search?" + params,
        {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_FR"},
    ))
    prix = [to_float(i.get("price", {}).get("value")) for i in data.get("itemSummaries", [])]
    prix = [p for p in prix if p]
    return round(statistics.median(prix), 2) if len(prix) >= 5 else None


def maj_refs(refs, recherches, token, max_par_passage=3, duree_h=24):
    """Prix neufs de référence, mis en cache 24 h (quelques appels par passage seulement)."""
    n = 0
    for rech in recherches:
        if rech.get("etat", "occasion") == "neuf":
            continue
        r = refs.get(rech["q"])
        if r and age_heures(r.get("maj", "")) < duree_h:
            continue
        if n >= max_par_passage:
            break
        try:
            refs[rech["q"]] = {"neuf": reference_neuf(token, rech["q"]), "maj": now_iso()}
            n += 1
        except Exception as e:
            log(f"[eBay] prix neuf indisponible pour « {rech['q']} » : {e}")


def fetch_ebay(cfg, refs=None):
    if not cfg.get("actif", True):
        return []
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        log("[eBay] clés absentes (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET) : eBay ignoré")
        return []
    try:
        token = ebay_token(cid, secret)
    except Exception as e:
        log(f"[eBay] connexion impossible : {e}")
        return []

    seuil = float(cfg.get("seuil_bonne_affaire", 0.7))
    seuil_suspect = float(cfg.get("seuil_suspect_pct", 70))
    min_note = float(cfg.get("min_note_vendeur", 97))
    exclus = [motif_debut_de_mot(m) for m in cfg.get("mots_exclus", [])]
    recherches = choisir_recherches(cfg.get("recherches", []), int(cfg.get("max_recherches_par_passage", 10)))
    refs = refs if refs is not None else {}
    marques = {**DEFAULT_MARQUES, **{k.lower(): v for k, v in cfg.get("marques", {}).items()}}
    frais = float(cfg.get("frais_revente_pct", 5))
    seuil_rent = float(cfg.get("seuil_rentabilite", 65))
    maj_refs(refs, recherches, token, int(cfg.get("max_refs_neuf_par_passage", 3)))
    out = []
    for rech in recherches:
        filtres = ["priceCurrency:EUR", "deliveryCountry:FR"]
        if rech.get("prix_max"):
            filtres.insert(0, f"price:[1..{int(rech['prix_max'])}]")
        etat = rech.get("etat", "occasion")
        if etat == "neuf":
            filtres.append("conditions:{NEW}")
        elif etat == "occasion":
            filtres.append("conditions:{USED}")
        params = urllib.parse.urlencode({
            "q": rech["q"],
            "filter": ",".join(filtres),
            "sort": "newlyListed",
            "limit": "200",
        })
        try:
            data = json.loads(http(
                "https://api.ebay.com/buy/browse/v1/item_summary/search?" + params,
                {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_FR"},
            ))
        except Exception as e:
            log(f"[eBay] recherche « {rech['q']} » en échec : {e}")
            continue

        items = data.get("itemSummaries", [])
        prix = [to_float(i.get("price", {}).get("value")) for i in items]
        prix = [p for p in prix if p]
        mediane = statistics.median(prix) if len(prix) >= 5 else None
        ref_neuf = (refs.get(rech["q"]) or {}).get("neuf")
        taille = (rech.get("taille") or "").strip()
        motif_taille = re.compile(r"(?<![\w])" + re.escape(taille) + r"(?![\w])", re.I) if taille else None

        for i in items:
            titre = i.get("title", "")
            if any(m.search(titre.lower()) for m in exclus):
                continue  # réplique, pour pièces, défectueux...
            p = to_float(i.get("price", {}).get("value"))
            if not p:
                continue
            vend = i.get("seller", {})
            note = to_float(vend.get("feedbackPercentage"))
            nb_avis = vend.get("feedbackScore")
            if note is not None and note < min_note:
                continue  # vendeur peu fiable : on écarte
            remise = round((1 - p / mediane) * 100) if mediane and p < mediane else 0
            # Un prix « trop beau pour être vrai » est souvent un faux ou une arnaque : on se méfie.
            suspect = remise >= seuil_suspect
            peu_avis = (nb_avis or 0) < 20
            nom_marque, coefs = trouver_marque(titre, marques)
            percentile = round(100 * sum(1 for x in prix if x > p) / len(prix)) if len(prix) >= 5 else None
            rent = calcul_rentabilite(p, mediane, ref_neuf, coefs, coef_etat(i.get("condition")), frais,
                                      percentile, suspect, peu_avis) or {}
            bonne_affaire = bool(
                mediane and not suspect and not peu_avis
                and (p <= mediane * seuil or rent.get("rentabilite", 0) >= seuil_rent))
            info = f"Médiane des annonces : {mediane:.0f} €" if mediane else ""
            out.append({
                "id": "ebay:" + (i.get("itemId") or i.get("itemWebUrl", "")),
                "source": "eBay",
                "titre": titre,
                "url": i.get("itemWebUrl", ""),
                "prix": p,
                "prix_txt": f"{p:.2f} €",
                "etat": i.get("condition") or "?",
                "vendeur": f"{vend.get('username', '')} ({nb_avis} avis)" if nb_avis is not None else vend.get("username", ""),
                "note_vendeur": note,
                "image": (i.get("image") or {}).get("imageUrl", ""),
                "alerte": bonne_affaire,
                "remise": remise,
                "suspect": suspect,
                "peu_avis": peu_avis,
                "rentabilite": rent.get("rentabilite"),
                "revente_est": rent.get("revente_est"),
                "marge_est": rent.get("marge_est"),
                "prix_neuf_ref": ref_neuf,
                "percentile": percentile,
                "marque": nom_marque,
                "taille_ok": bool(motif_taille and motif_taille.search(titre)),
                "info": info,
                "recherche": rech["q"],
            })
    log(f"[eBay] {len(out)} annonces retenues ({len(recherches)} recherches ce passage)")
    return out


# --------------------------------------------------------------------------- Notifications (ntfy.sh)
def envoyer_ntfy(topic, offres, max_msgs=5):
    """Envoie une notification par offre (les meilleures d'abord). Gratuit, sans compte : appli « ntfy »."""
    if not offres or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic or ""):
        return 0
    offres = sorted(offres, key=lambda d: -d.get("score", 0))
    envoyes = 0

    def poster(payload):
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            http("https://ntfy.sh/", {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8"))
            return True
        except Exception as e:
            log(f"[ntfy] envoi impossible : {e}")
            return False

    for d in offres[:max_msgs]:
        ligne2 = " · ".join(x for x in [d.get("etat"), d.get("source"),
                                         f"-{d['remise']} % vs médiane" if d.get("remise", 0) >= 10 else ""] if x)
        envoyes += poster({
            "topic": topic,
            "title": f"{d.get('categorie', 'Bon plan')} · {d.get('prix_txt') or 'offre'}",
            "message": f"{d.get('titre', '')}\n{ligne2}",
            "click": d.get("url") if str(d.get("url", "")).startswith("https://") else None,
            "tags": ["tada"],
        })
    reste = len(offres) - max_msgs
    if reste > 0:
        envoyes += poster({"topic": topic, "title": "Bons plans", "message": f"+{reste} autres bonnes affaires sur ta page"})
    return envoyes


# --------------------------------------------------------------------------- Historique + page
# Catégories classées de la plus intéressante à la moins intéressante.
# L'ordre de cette liste = l'ordre d'affichage sur la page. Modifiable via "categories" dans config.json.
DEFAULT_CATEGORIES = [
    {"nom": "Vêtements", "mots": [
        "veste", "manteau", "doudoune", "blouson", "parka", "pull", "sweat", "hoodie", "gilet", "pantalon",
        "jean", "chemise", "t-shirt", "tee-shirt", "polo", "robe", "jupe", "short", "survêtement", "jogging",
        "carhartt", "levi", "ralph lauren", "north face", "lacoste", "patagonia", "stone island", "dickies",
        "champion", "barbour", "stüssy", "stussy"]},
    {"nom": "Chaussures", "mots": [
        "sneakers", "baskets", "basket", "chaussures", "bottes", "boots", "mocassins", "new balance", "nike air",
        "air force", "air max", "adidas", "converse", "vans", "asics", "puma", "jordan", "salomon", "birkenstock",
        "dr martens", "samba"]},
    {"nom": "Sacs et accessoires", "mots": [
        "sac", "sacoche", "ceinture", "casquette", "bonnet", "écharpe", "gants", "montre", "lunettes",
        "portefeuille", "bijou", "collier", "bracelet", "ray-ban", "longchamp", "eastpak"]},
    {"nom": "High-tech", "mots": [
        "iphone", "samsung", "xiaomi", "ssd", "pc ", "ordinateur", "écouteurs", "airpods", "casque", "tv",
        "téléviseur", "console", "switch", "ps5", "xbox", "tablette", "ipad", "macbook", "enceinte", "montre connectée"]},
    {"nom": "Maison", "mots": [
        "aspirateur", "cafetière", "friteuse", "matelas", "lessive", "tefal", "dyson", "robot", "cuisine",
        "literie", "couette", "lampe", "meuble", "outil", "perceuse"]},
]


def categoriser(titre, cats):
    t = (titre or "").lower()
    for rang, c in enumerate(cats):
        for m in c.get("mots", []):
            if motif_debut_de_mot(m).search(t):
                return rang, c["nom"]
    return len(cats), "Autres"


def score_interet(d):
    """Plus c'est haut, plus c'est intéressant : alerte (50) + remise vs médiane (max 50)
    + vendeur très bien noté (5) + taille dans le titre (10) ; prix suspect : -30."""
    if d.get("rentabilite") is not None:  # annonces eBay : le score de rentabilité fait foi
        return int(d["rentabilite"]) + (10 if d.get("taille_ok") else 0)
    s = 50 if d.get("alerte") else 0
    s += min(int(d.get("remise") or 0), 50)
    if (d.get("note_vendeur") or 0) >= 99:
        s += 5
    if d.get("taille_ok"):
        s += 10
    if d.get("suspect"):
        s -= 30
    if d.get("peu_avis"):
        s -= 25
    return s


def merge(history, nouveaux, retention_jours, max_offres=1000, cats=None):
    cats = cats or DEFAULT_CATEGORIES
    maintenant = now_iso()
    par_id = {d["id"]: d for d in history}
    for d in nouveaux:
        if d["id"] in par_id:
            anc = par_id[d["id"]]
            d["ajoute"] = anc.get("ajoute", maintenant)
        else:
            d["ajoute"] = maintenant
        par_id[d["id"]] = d
    limite = datetime.now(timezone.utc) - timedelta(days=retention_jours)
    gardes = []
    for d in par_id.values():
        try:
            if datetime.fromisoformat(d["ajoute"]) >= limite:
                gardes.append(d)
        except Exception:
            gardes.append(d)
    # On garde les plus récentes si on dépasse la limite...
    gardes.sort(key=lambda d: d["ajoute"], reverse=True)
    gardes = gardes[:max_offres]
    for d in gardes:
        d["rang"], d["categorie"] = categoriser(d.get("titre"), cats)
        d["score"] = score_interet(d)
    # ...puis tri : catégorie (la plus intéressante d'abord), puis score ; à égalité, la plus récente d'abord.
    gardes.sort(key=lambda d: (d["rang"], -d["score"]))
    return gardes


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mes bons plans</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--txt:#1d1d1b;--mut:#6b6b66;--acc:#d9480f;--ok:#2b8a3e;--warn:#c92a2a;--line:#e4e2dc}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--txt:#f1f0ec;--mut:#9b9a93;--acc:#ff8a4c;--ok:#69db7c;--warn:#ff8787;--line:#33332f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:960px;margin:0 auto}
h1{margin:0 0 4px;font-size:1.5rem}
.sub{color:var(--mut);font-size:.9rem}
.bar{display:flex;flex-wrap:wrap;gap:8px;padding:8px 16px;max-width:960px;margin:0 auto}
.bar button,.bar input{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--txt);border-radius:999px;padding:8px 14px}
.bar button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.bar input{flex:1;min-width:160px;border-radius:10px}
main{display:grid;gap:12px;padding:8px 16px 40px;max-width:960px;margin:0 auto}
.card{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;text-decoration:none;color:inherit}
.card.hot{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok) inset}
.card.vu{opacity:.55}
.img{width:84px;height:84px;flex:none;border-radius:8px;background:var(--line) center/cover no-repeat}
.body{min-width:0;flex:1}
.t{font-weight:600;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.p{font-size:1.15rem;font-weight:700;color:var(--acc);margin-top:2px}
.m{color:var(--mut);font-size:.82rem;margin-top:2px}
.tag{display:inline-block;font-size:.72rem;padding:1px 8px;border-radius:999px;border:1px solid var(--line);margin-right:4px;color:var(--mut)}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.new{background:var(--acc);border-color:var(--acc);color:#fff}
.tag.warn{color:var(--warn);border-color:var(--warn)}
.empty{color:var(--mut);text-align:center;padding:40px 0}
.cat{font-size:1.05rem;margin:16px 0 0;padding-top:8px;border-top:2px solid var(--acc)}
.more{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--txt);border-radius:10px;padding:12px}
.links{max-width:960px;margin:0 auto;padding:4px 16px 8px}
.links h2{font-size:1rem;margin:8px 0}
.links details{border-top:1px solid var(--line)}
.links summary{cursor:pointer;font-weight:600;padding:8px 0}
.lk{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 0}
.lk .n{flex:1 1 180px;font-weight:600}
.lk .n small{display:block;font-weight:400;color:var(--mut)}
.lk a{font-size:.85rem;border:1px solid var(--line);background:var(--card);color:var(--txt);border-radius:999px;padding:6px 12px;text-decoration:none}
</style>
</head>
<body>
<header><h1>Mes bons plans</h1><div class="sub" id="maj"></div></header>
<div class="bar" id="bar">
  <button data-f="all" class="on">Tout</button>
  <button data-f="hot">Bonnes affaires</button>
  <button data-f="neuf">Neuf</button>
  <button data-f="occ">Occasion</button>
  <button id="mode" type="button">Tri : intérêt</button>
  <input id="q" type="search" placeholder="Rechercher…">
</div>
<section class="links" id="links"></section>
<main id="list"></main>
<script>
let DATA = __DATA__;
const LIENS = __LIENS__;
let MAJ = "__MAJ__";
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const ago = iso => {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 3600) return "il y a " + Math.max(1, Math.round(s / 60)) + " min";
  if (s < 86400) return "il y a " + Math.round(s / 3600) + " h";
  return "il y a " + Math.round(s / 86400) + " j";
};
const isNew = d => /neuf|new/i.test(d.etat || "");
let filtre = "all", terme = "", nb = 100, mode = "interet";
let vus = new Set();
try { vus = new Set(JSON.parse(localStorage.getItem("vus") || "[]")); } catch (e) {}
function marquerVu(id) {
  vus.add(id);
  try { localStorage.setItem("vus", JSON.stringify(Array.from(vus).slice(-3000))); } catch (e) {}
}
function render() {
  const list = document.getElementById("list");
  list.replaceChildren();
  const rows = DATA.filter(d =>
    (filtre === "all" || (filtre === "hot" && d.alerte) || (filtre === "neuf" && isNew(d)) || (filtre === "occ" && !isNew(d))) &&
    (!terme || (d.titre + " " + d.vendeur).toLowerCase().includes(terme)));
  if (!rows.length) { list.append(el("div", "empty", "Rien pour l'instant.")); return; }
  if (mode === "recent") rows.sort((x, y) => (y.ajoute > x.ajoute ? 1 : -1));
  const counts = {};
  for (const d of rows) counts[d.categorie] = (counts[d.categorie] || 0) + 1;
  let derniere = null;
  for (const d of rows.slice(0, nb)) {
    if (mode === "interet" && d.categorie !== derniere) {
      derniere = d.categorie;
      list.append(el("h2", "cat", (d.categorie || "Autres") + " · " + counts[d.categorie]));
    }
    const ok = /^https?:\\/\\//.test(d.url);
    const a = el("a", "card" + (d.alerte ? " hot" : "") + (vus.has(d.id) ? " vu" : ""));
    if (ok) { a.href = d.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    a.addEventListener("click", () => { marquerVu(d.id); a.classList.add("vu"); });
    const img = el("div", "img");
    if (d.image && /^https?:\\/\\//.test(d.image)) img.style.backgroundImage = "url('" + encodeURI(d.image).replace(/'/g, "%27") + "')";
    const b = el("div", "body");
    b.append(el("div", "t", d.titre));
    b.append(el("div", "p", d.prix_txt || "Voir l'offre"));
    const tags = el("div", "m");
    if (Date.now() - new Date(d.ajoute) < 1800000 && !vus.has(d.id)) tags.append(el("span", "tag new", "Nouveau"));
    if (d.alerte) tags.append(el("span", "tag ok", d.source === "eBay" ? "Prix bas" : "Correspond à tes mots-clés"));
    if (d.remise >= 10) tags.append(el("span", "tag ok", "-" + d.remise + " % vs médiane"));
    if (d.suspect) tags.append(el("span", "tag warn", "Prix suspect : méfiance (faux possible)"));
    if (d.peu_avis) tags.append(el("span", "tag warn", "vendeur avec peu d'avis"));
    if (d.taille_ok) tags.append(el("span", "tag ok", "taille dans le titre"));
    tags.append(el("span", "tag", d.source));
    tags.append(el("span", "tag", d.etat || "?"));
    if (d.note_vendeur != null) tags.append(el("span", "tag", "vendeur " + d.note_vendeur + " %"));
    b.append(tags);
    const meta = [d.vendeur, d.info, ago(d.ajoute)].filter(Boolean).join(" · ");
    b.append(el("div", "m", meta));
    a.append(img, b);
    list.append(a);
  }
  if (rows.length > nb) {
    const more = el("button", "more", "Afficher plus (" + (rows.length - nb) + " restantes)");
    more.onclick = () => { nb += 100; render(); };
    list.append(more);
  }
}
(function () {
  const box = document.getElementById("links");
  if (!LIENS.length) return;
  box.append(el("h2", null, "Mes recherches (résultats les plus récents, triés par nouveauté)"));
  const groupes = new Map();
  for (const l of LIENS) {
    const g = l.categorie || "Autres";
    if (!groupes.has(g)) groupes.set(g, []);
    groupes.get(g).push(l);
  }
  let premier = true;
  for (const [g, items] of groupes) {
    const det = el("details");
    det.open = premier; premier = false;
    det.append(el("summary", null, g + " (" + items.length + ")"));
    for (const l of items) {
      const row = el("div", "lk");
      const n = el("div", "n", l.nom);
      const info = [l.taille ? "taille " + l.taille : "", l.prix_max ? "max " + l.prix_max + " €" : ""].filter(Boolean).join(" · ");
      if (info) n.append(el("small", null, info));
      row.append(n);
      for (const [k, label] of [["vinted", "Vinted"], ["leboncoin", "Leboncoin"], ["ebay", "eBay"]]) {
        if (!/^https:\\/\\//.test(l[k])) continue;
        const a = el("a", null, label);
        a.href = l[k]; a.target = "_blank"; a.rel = "noopener noreferrer";
        row.append(a);
      }
      det.append(row);
    }
    box.append(det);
  }
})();
function majTexte(nouvelles) {
  document.getElementById("maj").textContent = DATA.length + " offres · mise à jour " + ago(MAJ) +
    (nouvelles > 0 ? " · +" + nouvelles + " nouvelle" + (nouvelles > 1 ? "s" : "") : "");
}
majTexte(0);
document.getElementById("bar").addEventListener("click", e => {
  const f = e.target.dataset && e.target.dataset.f; if (!f) return;
  filtre = f; nb = 100;
  document.querySelectorAll("#bar button").forEach(x => x.classList.toggle("on", x === e.target));
  render();
});
document.getElementById("mode").addEventListener("click", e => {
  mode = mode === "interet" ? "recent" : "interet"; nb = 100;
  e.target.textContent = mode === "interet" ? "Tri : intérêt" : "Tri : récent";
  render();
});
document.getElementById("q").addEventListener("input", e => { terme = e.target.value.toLowerCase(); nb = 100; render(); });
render();

// Rafraîchissement automatique : toutes les 60 s (et au retour sur l'onglet), sans recharger la page.
let nouvellesTotal = 0;
async function rafraichir() {
  try {
    const r = await fetch("deals.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return;
    const nouv = await r.json();
    if (!Array.isArray(nouv)) return;
    const sig = x => x.map(d => d.id + "|" + d.prix + "|" + d.score).join(",");
    if (sig(nouv) !== sig(DATA)) {
      const anciens = new Set(DATA.map(d => d.id));
      nouvellesTotal += nouv.filter(d => !anciens.has(d.id)).length;
      DATA = nouv;
      const lm = r.headers.get("last-modified");
      MAJ = lm ? new Date(lm).toISOString() : new Date().toISOString();
      render();
    }
    majTexte(nouvellesTotal);
  } catch (e) { /* hors-ligne ou erreur passagère : on réessaiera */ }
}
setInterval(() => { if (!document.hidden) rafraichir(); }, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) rafraichir(); });
</script>
</body>
</html>
"""


def build_links(recherches):
    """Liens de recherche pré-remplis (ouverts par toi dans ton navigateur, aucune collecte)."""
    liens = []
    for r in recherches:
        q = urllib.parse.quote_plus(r["q"])
        pmax = int(r["prix_max"]) if r.get("prix_max") else None
        vinted = f"https://www.vinted.fr/catalog?search_text={q}&order=newest_first&currency=EUR"
        lbc = f"https://www.leboncoin.fr/recherche?text={q}&sort=time&order=desc"
        ebay = f"https://www.ebay.fr/sch/i.html?_nkw={q}&_sop=10"
        if r.get("etat", "occasion") == "occasion":
            ebay += "&LH_ItemCondition=3000"  # occasion uniquement
        if pmax:
            vinted += f"&price_to={pmax}"
            lbc += f"&price=min-{pmax}"
            ebay += f"&_udhi={pmax}"
        liens.append({
            "nom": r.get("nom", r["q"]),
            "categorie": r.get("categorie", ""),
            "taille": r.get("taille", ""),
            "prix_max": pmax,
            "vinted": vinted,
            "leboncoin": lbc,
            "ebay": ebay,
        })
    return liens


def write_page(deals, liens=None):
    DOCS.mkdir(exist_ok=True)
    data = json.dumps(deals, ensure_ascii=False).replace("</", "<\\/")
    data_liens = json.dumps(liens or [], ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE.replace("__DATA__", data).replace("__LIENS__", data_liens)
            .replace("__MAJ__", now_iso()))
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (DOCS / "deals.json").write_text(json.dumps(deals, ensure_ascii=False, indent=1), encoding="utf-8")


def demo():
    t = now_iso()
    return [
        {"id": "d1", "source": "eBay", "titre": "Veste Carhartt Detroit taille M, très bon état", "url": "https://www.ebay.fr/",
         "prix": 42.0, "prix_txt": "42.00 €", "etat": "Occasion - Très bon état", "vendeur": "vintage_marie (812 avis)",
         "note_vendeur": 99.4, "image": "", "alerte": True, "remise": 51, "taille_ok": True,
         "info": "Médiane des annonces : 85 €", "ajoute": t},
        {"id": "d2", "source": "Dealabs", "titre": "Sneakers New Balance 530 à -40 %", "url": "https://www.dealabs.com/",
         "prix": 59.0, "prix_txt": "59,00€", "etat": "Neuf", "vendeur": "Zalando", "note_vendeur": None,
         "image": "", "alerte": True, "remise": 0, "info": "", "ajoute": t},
        {"id": "d3", "source": "eBay", "titre": "Pull Ralph Lauren col rond bleu marine M", "url": "https://www.ebay.fr/",
         "prix": 29.0, "prix_txt": "29.00 €", "etat": "Occasion", "vendeur": "friperie_lyon (230 avis)",
         "note_vendeur": 98.2, "image": "", "alerte": False, "remise": 24,
         "info": "Médiane des annonces : 38 €", "ajoute": t},
        {"id": "d4", "source": "eBay", "titre": "Doudoune The North Face 700 noire", "url": "https://www.ebay.fr/",
         "prix": 19.0, "prix_txt": "19.00 €", "etat": "Occasion", "vendeur": "promo_top (40 avis)",
         "note_vendeur": 98.0, "image": "", "alerte": False, "remise": 80, "suspect": True,
         "info": "Médiane des annonces : 95 €", "ajoute": t},
        {"id": "d5", "source": "eBay", "titre": "Sac à dos Eastpak bleu, comme neuf", "url": "https://www.ebay.fr/",
         "prix": 15.0, "prix_txt": "15.00 €", "etat": "Occasion", "vendeur": "marie75 (95 avis)",
         "note_vendeur": 99.0, "image": "", "alerte": True, "remise": 40, "info": "Médiane des annonces : 25 €", "ajoute": t},
        {"id": "d6", "source": "Dealabs", "titre": "Casque Sony WH-1000XM5 à prix cassé", "url": "https://www.dealabs.com/",
         "prix": 229.0, "prix_txt": "229,00€", "etat": "Neuf", "vendeur": "Amazon", "note_vendeur": None,
         "image": "", "alerte": False, "remise": 0, "info": "", "ajoute": t},
    ]


def main():
    DOCS.mkdir(exist_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mes_recherches = cfg.get("mes_recherches", [])
    liens = build_links(mes_recherches)
    cats = cfg.get("categories") or DEFAULT_CATEGORIES
    if "--demo" in sys.argv:
        write_page(merge([], demo(), 7, 1000, cats), liens)
        log("Page de démo écrite dans docs/index.html")
        return

    # Les recherches eBay reprennent « mes_recherches » sauf si ebay.recherches est défini à part.
    ebay_cfg = dict(cfg.get("ebay", {}))
    if "recherches" not in ebay_cfg:
        ebay_cfg["recherches"] = [
            {"q": r["q"], "prix_max": r.get("prix_max"), "etat": r.get("etat", "occasion"), "taille": r.get("taille")}
            for r in mes_recherches
        ]
    ebay_cfg.setdefault("mots_exclus", cfg.get("mots_exclus", []))

    nouveaux = fetch_dealabs(cfg.get("dealabs", {})) + fetch_ebay(ebay_cfg)
    try:
        history = json.loads((DOCS / "deals.json").read_text(encoding="utf-8"))
    except Exception:
        history = []
    ids_avant = {d["id"] for d in history}
    deals = merge(history, nouveaux, int(cfg.get("retention_jours", 7)), int(cfg.get("max_offres", 1000)), cats)
    write_page(deals, liens)
    log(f"Terminé : {len(deals)} offres sur la page")

    # Notifications : seulement pour les NOUVELLES bonnes affaires (jamais au tout premier passage).
    ntfy_cfg = cfg.get("ntfy", {})
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic and ntfy_cfg.get("actif", True) and ids_avant:
        score_min = int(ntfy_cfg.get("score_min", 50))
        fraiches = [d for d in deals if d["id"] not in ids_avant and d.get("alerte") and d["score"] >= score_min]
        n = envoyer_ntfy(topic, fraiches, int(ntfy_cfg.get("max_par_passage", 5)))
        log(f"[ntfy] {n} notification(s) envoyée(s)")


if __name__ == "__main__":
    main()
