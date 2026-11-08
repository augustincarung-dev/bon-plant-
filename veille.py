#!/usr/bin/env python3
"""Veille de bons plans : Dealabs (neuf, promos) + eBay (neuf et occasion).

Génère une page web statique (docs/index.html) et un historique (docs/deals.json).
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
UA = "Mozilla/5.0 (compatible; VeilleBonsPlans/1.0)"


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
    m = re.search(r"\d+(?:[.,]\d+)?", str(s).replace(" ", "").replace(" ", ""))
    return float(m.group().replace(",", ".")) if m else None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- Dealabs
def fetch_dealabs(cfg):
    if not cfg.get("actif", True):
        return []
    mots = [m.lower() for m in cfg.get("mots_cles", [])]
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
            match = any(m in (titre + " " + desc).lower() for m in mots)
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


def fetch_ebay(cfg):
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
    min_note = float(cfg.get("min_note_vendeur", 97))
    out = []
    for rech in cfg.get("recherches", []):
        filtres = ["priceCurrency:EUR", "deliveryCountry:FR"]
        if rech.get("prix_max"):
            filtres.insert(0, f"price:[1..{int(rech['prix_max'])}]")
        etat = rech.get("etat", "tous")
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

        for i in items:
            p = to_float(i.get("price", {}).get("value"))
            if not p:
                continue
            vend = i.get("seller", {})
            note = to_float(vend.get("feedbackPercentage"))
            nb_avis = vend.get("feedbackScore")
            if note is not None and note < min_note:
                continue  # vendeur peu fiable : on écarte
            bonne_affaire = bool(mediane and p <= mediane * seuil and (nb_avis or 0) >= 20)
            info = f"Médiane des annonces : {mediane:.0f} €" if mediane else ""
            remise = round((1 - p / mediane) * 100) if mediane and p < mediane else 0
            out.append({
                "id": "ebay:" + (i.get("itemId") or i.get("itemWebUrl", "")),
                "source": "eBay",
                "titre": i.get("title", ""),
                "url": i.get("itemWebUrl", ""),
                "prix": p,
                "prix_txt": f"{p:.2f} €",
                "etat": i.get("condition") or "?",
                "vendeur": f"{vend.get('username', '')} ({nb_avis} avis)" if nb_avis is not None else vend.get("username", ""),
                "note_vendeur": note,
                "image": (i.get("image") or {}).get("imageUrl", ""),
                "alerte": bonne_affaire,
                "remise": remise,
                "info": info,
                "recherche": rech["q"],
            })
    log(f"[eBay] {len(out)} annonces retenues")
    return out


# --------------------------------------------------------------------------- Historique + page
# Catégories classées de la plus intéressante à la moins intéressante.
# L'ordre de cette liste = l'ordre d'affichage sur la page. Modifiable via "categories" dans config.json.
DEFAULT_CATEGORIES = [
    {"nom": "Vêtements", "mots": [
        "veste", "manteau", "doudoune", "blouson", "parka", "pull", "sweat", "hoodie", "gilet", "pantalon",
        "jean", "chemise", "t-shirt", "tee-shirt", "polo", "robe", "jupe", "short", "survêtement", "jogging",
        "carhartt", "levi", "ralph lauren", "north face", "lacoste", "patagonia", "stone island", "dickies"]},
    {"nom": "Chaussures", "mots": [
        "sneakers", "baskets", "basket", "chaussures", "bottes", "boots", "mocassins", "new balance", "nike air",
        "air force", "air max", "adidas", "converse", "vans", "asics", "puma", "jordan", "salomon", "birkenstock"]},
    {"nom": "Sacs et accessoires", "mots": [
        "sac", "sacoche", "ceinture", "casquette", "bonnet", "écharpe", "gants", "montre", "lunettes",
        "portefeuille", "bijou", "collier", "bracelet"]},
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
            if re.search(r"(?<!\w)" + re.escape(m.lower()), t):
                return rang, c["nom"]
    return len(cats), "Autres"


def score_interet(d):
    """Plus c'est haut, plus c'est intéressant : alerte (50) + remise vs médiane (max 50) + vendeur très bien noté (5)."""
    s = 50 if d.get("alerte") else 0
    s += min(int(d.get("remise") or 0), 50)
    if (d.get("note_vendeur") or 0) >= 99:
        s += 5
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
:root{--bg:#f6f5f2;--card:#fff;--txt:#1d1d1b;--mut:#6b6b66;--acc:#d9480f;--ok:#2b8a3e;--line:#e4e2dc}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--txt:#f1f0ec;--mut:#9b9a93;--acc:#ff8a4c;--ok:#69db7c;--line:#33332f}}
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
.img{width:84px;height:84px;flex:none;border-radius:8px;background:var(--line) center/cover no-repeat}
.body{min-width:0;flex:1}
.t{font-weight:600;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.p{font-size:1.15rem;font-weight:700;color:var(--acc);margin-top:2px}
.m{color:var(--mut);font-size:.82rem;margin-top:2px}
.tag{display:inline-block;font-size:.72rem;padding:1px 8px;border-radius:999px;border:1px solid var(--line);margin-right:4px;color:var(--mut)}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.empty{color:var(--mut);text-align:center;padding:40px 0}
.cat{font-size:1.05rem;margin:16px 0 0;padding-top:8px;border-top:2px solid var(--acc)}
.more{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--txt);border-radius:10px;padding:12px}
.links{max-width:960px;margin:0 auto;padding:4px 16px 8px}
.links h2{font-size:1rem;margin:8px 0}
.lk{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 0;border-top:1px solid var(--line)}
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
    const a = el("a", "card" + (d.alerte ? " hot" : ""));
    if (ok) { a.href = d.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    const img = el("div", "img");
    if (d.image && /^https?:\\/\\//.test(d.image)) img.style.backgroundImage = "url('" + encodeURI(d.image).replace(/'/g, "%27") + "')";
    const b = el("div", "body");
    b.append(el("div", "t", d.titre));
    b.append(el("div", "p", d.prix_txt || "Voir l'offre"));
    const tags = el("div", "m");
    if (d.alerte) tags.append(el("span", "tag ok", d.source === "eBay" ? "Prix bas" : "Correspond à tes mots-clés"));
    if (d.remise >= 10) tags.append(el("span", "tag ok", "-" + d.remise + " % vs médiane"));
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
  box.append(el("h2", null, "Mes recherches (ouvre directement les résultats les plus récents)"));
  for (const l of LIENS) {
    const row = el("div", "lk");
    const n = el("div", "n", l.nom);
    const det = [l.taille ? "taille " + l.taille : "", l.prix_max ? "max " + l.prix_max + " €" : ""].filter(Boolean).join(" · ");
    if (det) n.append(el("small", null, det));
    row.append(n);
    for (const [k, label] of [["vinted", "Vinted"], ["leboncoin", "Leboncoin"], ["ebay", "eBay"]]) {
      if (!/^https:\\/\\//.test(l[k])) continue;
      const a = el("a", null, label);
      a.href = l[k]; a.target = "_blank"; a.rel = "noopener noreferrer";
      row.append(a);
    }
    box.append(row);
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
        ebay = f"https://www.ebay.fr/sch/i.html?_nkw={q}&_sop=10&LH_BIN=1"
        if pmax:
            vinted += f"&price_to={pmax}"
            lbc += f"&price=min-{pmax}"
            ebay += f"&_udhi={pmax}"
        liens.append({
            "nom": r.get("nom", r["q"]),
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
         "note_vendeur": 99.4, "image": "", "alerte": True, "remise": 51, "info": "Médiane des annonces : 85 €", "ajoute": t},
        {"id": "d2", "source": "Dealabs", "titre": "Sneakers New Balance 530 à -40 %", "url": "https://www.dealabs.com/",
         "prix": 59.0, "prix_txt": "59,00€", "etat": "Neuf", "vendeur": "Zalando", "note_vendeur": None,
         "image": "", "alerte": True, "info": "", "ajoute": t},
        {"id": "d3", "source": "eBay", "titre": "Pull Ralph Lauren col rond bleu marine M", "url": "https://www.ebay.fr/",
         "prix": 29.0, "prix_txt": "29.00 €", "etat": "Occasion", "vendeur": "friperie_lyon (230 avis)",
         "note_vendeur": 98.2, "image": "", "alerte": False, "remise": 24, "info": "Médiane des annonces : 38 €", "ajoute": t},
        {"id": "d4", "source": "Dealabs", "titre": "Casque Sony WH-1000XM5 à prix cassé", "url": "https://www.dealabs.com/",
         "prix": 229.0, "prix_txt": "229,00€", "etat": "Neuf", "vendeur": "Amazon", "note_vendeur": None,
         "image": "", "alerte": False, "remise": 0, "info": "", "ajoute": t},
        {"id": "d5", "source": "eBay", "titre": "Sac à dos Eastpak bleu, comme neuf", "url": "https://www.ebay.fr/",
         "prix": 15.0, "prix_txt": "15.00 €", "etat": "Occasion", "vendeur": "marie75 (95 avis)",
         "note_vendeur": 99.0, "image": "", "alerte": True, "remise": 40, "info": "Médiane des annonces : 25 €", "ajoute": t},
        {"id": "d6", "source": "Dealabs", "titre": "Aspirateur robot Roborock Q Revo", "url": "https://www.dealabs.com/",
         "prix": 349.0, "prix_txt": "349,00€", "etat": "Neuf", "vendeur": "Boulanger", "note_vendeur": None,
         "image": "", "alerte": False, "remise": 0, "info": "", "ajoute": t},
    ]


def main():
    DOCS.mkdir(exist_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    liens = build_links(cfg.get("mes_recherches", []))
    cats = cfg.get("categories") or DEFAULT_CATEGORIES
    if "--demo" in sys.argv:
        write_page(merge([], demo(), 7, 1000, cats), liens)
        log("Page de démo écrite dans docs/index.html")
        return
    nouveaux = fetch_dealabs(cfg.get("dealabs", {})) + fetch_ebay(cfg.get("ebay", {}))
    try:
        history = json.loads((DOCS / "deals.json").read_text(encoding="utf-8"))
    except Exception:
        history = []
    deals = merge(history, nouveaux, int(cfg.get("retention_jours", 7)), int(cfg.get("max_offres", 1000)), cats)
    write_page(deals, liens)
    log(f"Terminé : {len(deals)} offres sur la page")


if __name__ == "__main__":
    main()
