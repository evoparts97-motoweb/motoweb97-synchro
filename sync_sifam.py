"""Synchro SIFAM -> MotoWeb97 (Shopify) : stock + prix, toutes les 30 minutes (GitHub Actions).

Règles (décisions Florent) :
- Stock : quantité SIFAM recopiée sur l'emplacement « Le Gosier » (plafond 999). Article absent chez SIFAM -> 0.
  Produits tagués « stock-gosier » (stock physique au Gosier) : jamais touchés.
- Prix « grille B » (02/11/2026), sur le coût de revient HT (champ « Coût par article ») :
    revient < 20 €  : (revient + 6 €) x 1,085
    revient >= 20 € : revient x coef x 1,085, coef = 1,50 - 0,025 par tranche de 40 € au-delà de 20 €, minimum 1,30
    arrondi vers le haut à ,50 ou ,90
- Quand le prix d'achat SIFAM change : revient = ancien revient + écart d'achat x 1,095 (octroi de mer 9,5 %),
  puis nouveau prix grille B. Le transport compris dans le revient (8 €/kg, grille > 2 kg, 65 € casques) ne bouge pas.
- Équipement pilote (tag « Équipement pilote ») : prix plancher = prix public SIFAM TTC métropole (public HT x 1,20),
  arrondi ,50/,90 (décision 04/11/2026). Prix = max(grille B, plancher), recalculé à chaque passage.
- Produits tagués « prix-manuel » : prix jamais modifiés (le stock, si).

Deux modes :
- rapide (toutes les 30 min) : ne traite que les articles dont le stock ou les prix SIFAM ont changé depuis le passage précédent.
- complet (chaque nuit + à la demande) : relit tout le catalogue Shopify et corrige tous les écarts.

Sécurités : moins de 1 000 articles renvoyés par SIFAM -> arrêt sans rien écrire ; baisse de prix de plus de 30 %
en un passage -> non appliquée, signalée dans le rapport ; SIMULATION=1 -> n'écrit rien, rapport seulement.
"""
import json, math, os, sys, time, urllib.request, urllib.parse, urllib.error

API_VERSION = '2026-10'
LOCATION = 'gid://shopify/Location/90504757388'        # Le Gosier
STOCK_MAX = 999
MIN_ARTICLES_SIFAM = 1000
TVA = 0.085
OCTROI = 0.095
BAISSE_MAX = 0.30
TAG_PILOTE = 'Équipement pilote'
TAG_GOSIER = 'stock-gosier'
TAG_MANUEL = 'prix-manuel'
PREFIXE_SKU = 'MW2-'
DOSSIER_ETAT = os.environ.get('DOSSIER_ETAT', 'etat')
SIMULATION = os.environ.get('SIMULATION', '0') == '1'
rapport = []


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ prix
def arrondi(p):
    """Arrondi vers le haut au prochain x,50 ou x,90."""
    e = math.floor(p)
    for c in (0.50, 0.90):
        if p <= e + c + 1e-9:
            return round(e + c, 2)
    return round(e + 1.50, 2)


def coef_grille_b(revient):
    tranche = math.floor((revient - 20) / 40)
    return max(1.30, 1.50 - 0.025 * tranche)


def prix_grille_b(revient):
    if revient < 20:
        return arrondi((revient + 6) * (1 + TVA))
    return arrondi(revient * coef_grille_b(revient) * (1 + TVA))


def plancher_pilote(public_ht):
    return arrondi(public_ht * 1.20) if public_ht and public_ht > 0 else 0.0


def nouveau_prix(v, s, etat_ref):
    """v = variante Shopify, s = ligne SIFAM, etat_ref = {a: achat de référence}. Renvoie (prix, revient, motif) ou None."""
    if TAG_MANUEL in v['tags']:
        return None
    achat = s['achat']
    revient = v['cout']
    motif = []
    a_ref = etat_ref.get('a')
    if revient is not None and a_ref is not None and achat > 0 and abs(achat - a_ref) >= 0.005:
        revient = round(revient + (achat - a_ref) * (1 + OCTROI), 2)
        motif.append(f'achat SIFAM {a_ref:.2f} -> {achat:.2f}')
    if revient is None or revient <= 0:
        return None
    pilote = TAG_PILOTE in v['tags']
    if not motif and not pilote:
        return None                                  # rien n'a bougé : on garde le prix actuel (prix posés à la main compris)
    prix = prix_grille_b(revient)
    if pilote:
        pl = plancher_pilote(s['public'])
        if pl > prix:
            prix = pl
            motif.append('plancher prix public SIFAM')
    return prix, revient, ', '.join(motif) or 'grille B'


# ------------------------------------------------------------------ SIFAM
def f2(x):
    try:
        return float(str(x).replace(',', '.'))
    except Exception:
        return 0.0


def sifam_stocks():
    url = 'https://api.sifam.fr/api.json/stocks?' + urllib.parse.urlencode(
        {'tarifs': 1, 'ean': 1, 'api_key': os.environ['SIFAM_API_KEY']})
    for essai in range(4):
        try:
            with urllib.request.urlopen(url, timeout=600) as r:
                lignes = json.load(r)
            break
        except Exception as e:
            if essai == 3:
                raise
            log('SIFAM : nouvel essai après erreur', e)
            time.sleep(15 * (essai + 1))
    out = {}
    for l in lignes:
        ref = str(l.get('REFART') or '').strip()
        if not ref:
            continue
        out[ref] = dict(q=max(0, int(f2(l.get('QUANTITE')))), achat=f2(l.get('PRIX_ACHAT')), public=f2(l.get('PRIX_PUBLIC')))
    return out


# ------------------------------------------------------------------ Shopify
class Shopify:
    def __init__(self):
        self.shop = os.environ['SHOPIFY_SHOP'].replace('.myshopify.com', '')
        self.base = f'https://{self.shop}.myshopify.com'
        self.token = self._jeton()

    def _jeton(self):
        if os.environ.get('SHOPIFY_TOKEN'):
            return os.environ['SHOPIFY_TOKEN']
        corps = json.dumps({'client_id': os.environ['SHOPIFY_CLIENT_ID'], 'client_secret': os.environ['SHOPIFY_CLIENT_SECRET'],
                            'grant_type': 'client_credentials'}).encode()
        req = urllib.request.Request(self.base + '/admin/oauth/access_token', corps, {'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)['access_token']

    def gql(self, requete, variables=None):
        corps = json.dumps({'query': requete, 'variables': variables or {}}).encode()
        for essai in range(6):
            req = urllib.request.Request(f'{self.base}/admin/api/{API_VERSION}/graphql.json', corps,
                                         {'Content-Type': 'application/json', 'X-Shopify-Access-Token': self.token})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and essai < 5:
                    time.sleep(2 ** essai)
                    continue
                raise
            if d.get('errors'):
                if any('THROTTLED' in json.dumps(x) for x in d['errors']) and essai < 5:
                    time.sleep(2 ** essai)
                    continue
                raise RuntimeError(json.dumps(d['errors'])[:800])
            dispo = (d.get('extensions') or {}).get('cost', {}).get('throttleStatus', {}).get('currentlyAvailable', 1000)
            if dispo < 200:
                time.sleep(2)
            return d['data']
        raise RuntimeError('Shopify : trop de refus (limite de débit)')

    def catalogue(self):
        """Toutes les variantes SKU MW2-* via une opération groupée (bulk)."""
        q = '''{ productVariants(query: "sku:%s*") { edges { node {
              id sku price inventoryQuantity
              product { id tags }
              inventoryItem { id unitCost { amount } } } } } }''' % PREFIXE_SKU
        r = self.gql('mutation($q: String!) { bulkOperationRunQuery(query: $q) { bulkOperation { id } userErrors { message } } }', {'q': q})
        err = r['bulkOperationRunQuery']['userErrors']
        if err:
            raise RuntimeError(err)
        op_id = r['bulkOperationRunQuery']['bulkOperation']['id']
        while True:
            time.sleep(5)
            op = self.gql('query($id: ID!) { node(id: $id) { ... on BulkOperation { status errorCode url objectCount } } }', {'id': op_id})['node']
            if op['status'] in ('COMPLETED', 'FAILED', 'CANCELED', 'EXPIRED'):
                break
        if op['status'] != 'COMPLETED':
            raise RuntimeError(f"Lecture du catalogue Shopify en échec : {op['status']} {op.get('errorCode')}")
        out = {}
        if not op.get('url'):
            return out
        with urllib.request.urlopen(op['url'], timeout=600) as r:
            for ligne in r:
                n = json.loads(ligne)
                sku = (n.get('sku') or '').strip()
                if not sku.startswith(PREFIXE_SKU):
                    continue
                cout = (n.get('inventoryItem') or {}).get('unitCost') or {}
                out[sku] = dict(id=n['id'], produit=n['product']['id'], tags=n['product'].get('tags') or [],
                                prix=f2(n.get('price')), q=n.get('inventoryQuantity'),
                                item=n['inventoryItem']['id'], cout=f2(cout['amount']) if cout.get('amount') else None)
        return out

    def ecrire_stocks(self, liste):
        m = '''mutation($input: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $input) {
                 userErrors { field message } } }'''
        erreurs = 0
        for i in range(0, len(liste), 250):
            paquet = [{'inventoryItemId': it, 'locationId': LOCATION, 'quantity': q, 'changeFromQuantity': None} for it, q in liste[i:i + 250]]
            r = self.gql(m, {'input': {'name': 'available', 'reason': 'correction', 'quantities': paquet}})
            ue = r['inventorySetQuantities']['userErrors']
            if ue:
                erreurs += len(ue)
                log('Erreurs stock :', ue[:3])
        return erreurs

    def ecrire_prix(self, par_produit):
        m = '''mutation($p: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $p, variants: $v) {
                 userErrors { field message } } }'''
        erreurs = 0
        for pid, variantes in par_produit.items():
            v = [{'id': x['id'], 'price': f"{x['prix']:.2f}", 'inventoryItem': {'cost': f"{x['revient']:.2f}"}} for x in variantes]
            ue = self.gql(m, {'p': pid, 'v': v})['productVariantsBulkUpdate']['userErrors']
            if ue:
                erreurs += len(ue)
                log('Erreurs prix :', pid, ue[:3])
        return erreurs


# ------------------------------------------------------------------ état
def lire(nom, defaut):
    try:
        with open(os.path.join(DOSSIER_ETAT, nom)) as f:
            return json.load(f)
    except Exception:
        return defaut


def ecrire(nom, data):
    os.makedirs(DOSSIER_ETAT, exist_ok=True)
    with open(os.path.join(DOSSIER_ETAT, nom), 'w') as f:
        json.dump(data, f, ensure_ascii=False)


# ------------------------------------------------------------------ passage
def passage(mode):
    t0 = time.time()
    sifam = sifam_stocks()
    log('SIFAM :', len(sifam), 'articles')
    if len(sifam) < MIN_ARTICLES_SIFAM:
        rapport.append(f'**Arrêt de sécurité** : SIFAM n\'a renvoyé que {len(sifam)} articles. Rien n\'a été modifié.')
        return 1
    shop = Shopify()
    etat = lire('etat.json', {})                    # sku -> {q, a, p} : dernier stock écrit, achat et public SIFAM de référence
    cat = None if mode == 'complet' else lire('catalogue.json', None)
    if cat is None:
        mode = 'complet'
        cat = shop.catalogue()
        log('Shopify :', len(cat), 'variantes SIFAM')
        ecrire('catalogue.json', cat)

    stocks, prix_par_produit, baisses, n_prix, bloques = [], {}, [], 0, set()
    for sku, v in cat.items():
        ref = sku[len(PREFIXE_SKU):]
        s = sifam.get(ref) or dict(q=0, achat=0.0, public=0.0)
        e = etat.get(sku)
        nouveau = e is None
        if nouveau:
            e = etat[sku] = {'q': v['q'], 'a': s['achat'] or None, 'p': s['public']}
        # ---- stock
        if TAG_GOSIER not in v['tags']:
            q = min(s['q'], STOCK_MAX)
            reference_q = v['q'] if mode == 'complet' else e.get('q')
            if q != reference_q:
                stocks.append((v['item'], q, sku))
        # ---- prix (seulement si quelque chose a bougé, ou en mode complet, ou équipement pilote)
        change_sifam = (not nouveau) and (abs(s['achat'] - (e.get('a') or 0)) >= 0.005 or abs(s['public'] - (e.get('p') or 0)) >= 0.005)
        if s['achat'] > 0 and (mode == 'complet' or change_sifam):
            r = nouveau_prix(v, s, e)
            if r:
                prix, revient, motif = r
                if abs(prix - v['prix']) >= 0.005 or abs(revient - (v['cout'] or 0)) >= 0.005:
                    if prix < v['prix'] * (1 - BAISSE_MAX):
                        bloques.add(sku)
                        baisses.append(f'{sku} : {v["prix"]:.2f} -> {prix:.2f} € ({motif}) — NON appliqué, à vérifier')
                    else:
                        prix_par_produit.setdefault(v['produit'], []).append(dict(id=v['id'], prix=prix, revient=revient, sku=sku))
                        rapport.append(f'- {sku} : {v["prix"]:.2f} -> {prix:.2f} € ({motif})')
                        n_prix += 1

    log('Stocks à écrire :', len(stocks), '— prix à écrire :', n_prix)
    err = 0
    if SIMULATION:
        rapport.insert(0, '**Simulation** : rien n\'a été écrit.')
    else:
        if stocks:
            err += shop.ecrire_stocks([(it, q) for it, q, _ in stocks])
        if prix_par_produit:
            err += shop.ecrire_prix(prix_par_produit)
    if err == 0 and not SIMULATION:
        # mémoire mise à jour seulement si tout s'est bien écrit (sinon le passage suivant réessaie)
        maj_cat = False
        for it, q, sku in stocks:
            etat[sku]['q'] = q
            cat[sku]['q'] = q
            maj_cat = True
        for variantes in prix_par_produit.values():
            for x in variantes:
                cat[x['sku']]['prix'] = x['prix']
                cat[x['sku']]['cout'] = x['revient']
                maj_cat = True
        for sku in cat:
            s = sifam.get(sku[len(PREFIXE_SKU):])
            if s and s['achat'] > 0 and sku not in bloques:
                etat[sku]['a'] = s['achat']
                etat[sku]['p'] = s['public']
        ecrire('etat.json', etat)
        if maj_cat:
            ecrire('catalogue.json', cat)

    entete = [f'## Synchro SIFAM — passage {mode}',
              f'{len(sifam)} articles SIFAM, {len(cat)} variantes sur le site, {len(stocks)} stocks et {n_prix} prix mis à jour, '
              f'{err} erreur(s), {round(time.time() - t0)} s.']
    if baisses:
        entete.append(f'\n### {len(baisses)} baisse(s) de prix de plus de {int(BAISSE_MAX * 100)} % bloquée(s)')
        entete += [f'- {b}' for b in baisses[:200]]
    if n_prix:
        entete.append('\n### Prix modifiés')
    rapport[:0] = entete
    return 1 if err else 0


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'rapide'
    try:
        code = passage(mode)
    except Exception as e:
        rapport.append(f'**Erreur** : {e}')
        code = 1
    texte = '\n'.join(rapport[:600])
    print(texte)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(texte + '\n')
    sys.exit(code)
