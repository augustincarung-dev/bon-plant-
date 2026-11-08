# Veille de bons plans (24h/24, gratuit)

Un petit robot qui tourne chez GitHub toutes les 5 minutes (GitHub décale parfois de quelques minutes), cherche des bons plans
(neuf et occasion) et les publie sur une page web avec un lien fixe, consultable sur téléphone.

**Sources**
- **Dealabs** : les offres les plus "hot" et les nouveautés (neuf, promos). Aucune clé nécessaire.
- **eBay** : annonces neuves et d'occasion, via l'API officielle (clé gratuite, voir étape 3).
  Le robot compare chaque prix à la médiane des annonces similaires, écarte les vendeurs
  dont la note est trop basse et marque en vert les prix anormalement bas.

**Vinted et Leboncoin** ne sont pas inclus : ils n'ont pas d'API publique et interdisent
la collecte automatisée. Utilise leurs alertes de recherche intégrées, en complément.

## Installation (environ 10 minutes)

### 1. Créer le dépôt GitHub
1. Crée un compte sur github.com si besoin.
2. Clique sur **New repository**, nomme-le par exemple `bons-plans`, choisis **Public**
   (les pages gratuites l'exigent), puis crée-le.
3. Clique sur **uploading an existing file** et glisse tout le contenu de ce dossier
   (dont le dossier caché `.github`). Si le dossier caché pose problème : **Add file > Create new file**,
   tape `.github/workflows/veille.yml` comme nom et colle le contenu du fichier.

### 2. Activer la page
**Settings > Pages > Build and deployment** : Source = *Deploy from a branch*,
Branch = `main`, dossier = `/docs`, puis **Save**.
Ton lien sera `https://TON-PSEUDO.github.io/bons-plans/` (affiché en haut de cette page de réglages).

### 3. (Optionnel mais recommandé) Brancher eBay
1. Crée un compte sur developer.ebay.com et génère une clé **Production** (Application Keys).
2. Dans ton dépôt : **Settings > Secrets and variables > Actions > New repository secret**.
3. Ajoute `EBAY_CLIENT_ID` (App ID) et `EBAY_CLIENT_SECRET` (Cert ID).

Sans ces clés, le robot fonctionne quand même avec Dealabs seul.

### 4. Lancer
Onglet **Actions > Veille bons plans > Run workflow**. Après une minute, ouvre ton lien.
Ensuite, tout se met à jour tout seul.

## Vêtements : Vinted et Leboncoin via « Mes recherches »
En haut de la page, chaque recherche de `mes_recherches` a un bouton Vinted, Leboncoin et eBay
qui ouvre directement les résultats les plus récents, avec ton prix maximum.
C'est toi qui cliques : le robot ne se connecte à aucun de tes comptes.
Pour être alerté sur téléphone, ouvre la recherche dans l'appli Vinted et enregistre-la
avec les notifications, en ajoutant ta taille avec les filtres.

## Personnaliser
Modifie `config.json` directement sur GitHub (icône crayon) :
- `mes_recherches` : tes vêtements (nom, mots de recherche, prix maximum, taille pour mémoire).
- `ebay.recherches` : tes produits, le prix maximum et l'état (`neuf`, `occasion` ou `tous`).
- `ebay.seuil_bonne_affaire` : 0.7 = signalé si le prix est 30 % sous la médiane.
- `ebay.min_note_vendeur` : note minimale du vendeur en %.
- `dealabs.mots_cles` : mots qui mettent une offre Dealabs en avant.
  Mets `filtrer_par_mots_cles` à `true` pour ne garder que celles-ci.
- `retention_jours` : durée de conservation des offres sur la page.

## À savoir
- La page est **publique** : ne mets rien de personnel dans `config.json`.
- Le "prix bas" est une aide, pas une garantie : ouvre toujours l'annonce, regarde les photos,
  les avis du vendeur, et paie via la protection de la plateforme.
- GitHub met en pause les automatisations d'un dépôt après 60 jours sans activité :
  si la page ne bouge plus, relance-la depuis l'onglet Actions.
- Les flux Dealabs peuvent changer : si une source tombe, le robot l'ignore et continue.
- Test sur ton ordinateur : `python veille.py --demo` génère une page d'exemple dans `docs/index.html`.
