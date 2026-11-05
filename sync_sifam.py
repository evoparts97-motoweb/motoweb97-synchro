# VERSION 3 du 04/11/2026 (correctif stock @idempotent) : ajout hebdomadaire en brouillon (validation manuelle, tag a-valider) + produits archivés ignorés
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

Trois modes :
- ajout (chaque semaine) : crée les articles SIFAM en stock absents du site, sauf catégories écartées
  (kits déco, goodies, pneus, gants hiver, sous-gants, chaussures de sécurité, dropshipping interdit, vendus par lot,
  produits archivés, casques et vêtements à tailles/couleurs). Sans photo, freinage, batteries -> brouillon.
- rapide (toutes les 30 min) : ne traite que les articles dont le stock ou les prix SIFAM ont changé depuis le passage précédent.
- complet (chaque nuit + à la demande) : relit tout le catalogue Shopify et corrige tous les écarts.

Sécurités : moins de 1 000 articles renvoyés par SIFAM -> arrêt sans rien écrire ; baisse de prix de plus de 30 %
en un passage -> non appliquée, signalée dans le rapport ; SIMULATION=1 -> n'écrit rien, rapport seulement.
"""
import json, math, os, sys, time, uuid, urllib.request, urllib.parse, urllib.error

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
        out[ref] = dict(q=max(0, int(f2(l.get('QUANTITE')))), achat=f2(l.get('PRIX_ACHAT')), public=f2(l.get('PRIX_PUBLIC')),
                        cond=int(f2(l.get('CONDITIONNEMENT')) or 1))
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
              product { id tags status }
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
                out[sku] = dict(id=n['id'], produit=n['product']['id'], tags=n['product'].get('tags') or [], statut=n['product'].get('status') or '',
                                prix=f2(n.get('price')), q=n.get('inventoryQuantity'),
                                item=n['inventoryItem']['id'], cout=f2(cout['amount']) if cout.get('amount') else None)
        return out

    def ecrire_stocks(self, liste):
        # API 2026-10 : la directive @idempotent est obligatoire sur les mutations de stock
        m = '''mutation($input: InventorySetQuantitiesInput!) { inventorySetQuantities(input: $input) @idempotent(key: "%s") {
                 userErrors { field message } } }'''
        erreurs = 0
        for i in range(0, len(liste), 250):
            paquet = [{'inventoryItemId': it, 'locationId': LOCATION, 'quantity': q, 'changeFromQuantity': None} for it, q in liste[i:i + 250]]
            r = self.gql(m % uuid.uuid4(), {'input': {'name': 'available', 'reason': 'correction', 'quantities': paquet}})
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
        if v.get('statut') == 'ARCHIVED':
            continue                                 # produits archivés (retirés du catalogue) : ni stock ni prix
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
            if s and s['achat'] > 0 and sku not in bloques and sku in etat:
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



# ================================================================== AJOUT HEBDOMADAIRE
# Articles SIFAM en stock qui ne sont pas (encore) sur le site -> créés automatiquement, sauf catégories écartées.
import gzip, html, re, collections as C, datetime

FAMILLES = ['ELE', 'EQU', 'OUT', 'ECH', 'FIL', 'MOT', 'TRS', 'LUB', 'JAN', 'ALL', 'ADM', 'EMB', 'FRN', 'RLT',
            'SUS', 'FLU', 'BAG', 'ANT', 'ENT', 'KEN', 'ACC', 'CAS', 'VET', 'VEQ', 'VEP', 'ENF']   # sans PNE (pneus) ni GDS (goodies)
FAMILLES_DECLINEES = {'CAS', 'VET', 'VEQ', 'VEP', 'ENF'}   # tailles / couleurs regroupées par modèle : ajout avec Claude
PUBLICATION = 'gid://shopify/Publication/218802454668'      # Boutique en ligne
MAX_AJOUTS = int(os.environ.get('MAX_AJOUTS', '400'))
EUR_KG, SEUIL_GRILLE = 8.0, 2.0
TRANSPORT_CASQUE = 65.0
GRILLE = [(0, 0.5, 30.06), (0.5, 1, 34.26), (1, 1.5, 37.54), (1.5, 2, 39.86), (2, 3, 44.51), (3, 4, 49.15), (4, 5, 53.8), (5, 6, 58.45),
          (6, 7, 63.09), (7, 8, 67.74), (8, 9, 72.38), (9, 10, 77.03), (10, 11, 83.86), (11, 12, 90.68), (12, 13, 97.51), (13, 14, 104.33),
          (14, 15, 111.16), (15, 16, 117.98), (16, 17, 124.81), (17, 18, 131.63), (18, 19, 138.46), (19, 20, 145.28), (20, 21, 152.11),
          (21, 22, 158.93), (22, 23, 165.76), (23, 24, 172.58), (24, 25, 179.41), (25, 26, 186.23), (26, 27, 193.06), (27, 28, 199.88),
          (28, 29, 206.71), (29, 30, 213.53)] + [(k, k + 1, round(216.94 + (k - 30) * 3.4125, 2)) for k in range(30, 70)]
EXCLURE_CHEMIN = ('Pneumatiques', 'Pièces entretien > Pneumatiques', 'Motocross / Supermotard > Pneus', 'Quad > Pneus quad')
AUTRES = {'Transmission': 'Autres pièces transmission', 'Moteur': 'Autres pièces moteur'}
FREINAGE_PHOTOS = {'Plaquettes de frein', 'Mâchoires de frein', 'Disques de frein'}   # photos à contrôler : mis en brouillon
MUT_CREATION = 'mutation($i: ProductSetInput!) { productSet(input: $i, synchronous: true) { product { id } userErrors { field message } } }'
MUT_PUBLIER = 'mutation($id: ID!, $p: ID!) { publishablePublish(id: $id, input: {publicationId: $p}) { userErrors { message } } }'


def sifam_get(chemin, timeout=600):
    url = 'https://api.sifam.fr/api.json/' + chemin + ('&' if '?' in chemin else '?') + 'api_key=' + os.environ['SIFAM_API_KEY']
    for essai in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.load(r)
        except Exception:
            if essai == 3:
                raise
            time.sleep(5 * (essai + 1))


def gant_hiver(a):
    d = (a.get('designation') or '').lower()
    if 'gant' not in d and 'gant' not in (a.get('designation_sfamille') or '').lower():
        return False
    if 'été' in d or ' ete ' in f' {d} ':
        return False
    if 'chauffant' in d or 'grand froid' in d:
        return True
    for i in range(10):
        if (a.get(f'c{i}_label') or '').strip() in ('Saisonalité', 'Utilisation') and re.search(r'hiver|mi-saison', str(a.get(f'c{i}') or ''), re.I):
            return True
    return False


def exclu_choix(a):
    d = ((a.get('designation') or '') + ' ' + (a.get('designation_sfamille') or '')).lower()
    if re.search(r'sous[- ]?gant', d):
        return 'exclu : sous-gants'
    if 'chaussure' in d and re.search(r's[ée]curit[ée]|bout acier', d):
        return 'exclu : chaussures de sécurité'
    return None


def esc(x):
    return html.escape(str(x or '').strip())


def taux_om_chemin(chemin):
    c = chemin.lower()
    if 'chaînes' in c or 'visserie' in c:
        return 4.5
    if c.startswith('pièces entretien > moteur') or any(k in c for k in ('pistons', 'soupapes', 'vilebrequins', 'kits carburateur', 'distribution')):
        return 12.5
    return 9.5


def transport_article(poids, souscat):
    if souscat == 'Casques':
        return TRANSPORT_CASQUE
    if poids > SEUIL_GRILLE:
        for a, b, p in GRILLE:
            if a < poids <= b:
                return p
        return None                                   # > 70 kg : hors grille
    return round(max(poids, 0.1) * EUR_KG, 2)


def position(r):
    av = r.get('AVANT') or r.get('AVANT_GAUCHE') or r.get('AVANT_DROIT')
    ar = r.get('ARRIERE') or r.get('ARRIERE_GAUCHE') or r.get('ARRIERE_DROIT')
    return 'Avant et arrière' if av and ar else 'Avant' if av else 'Arrière' if ar else ''


def compat(ref):
    try:
        lst = sifam_get('applicationsmotoarticle/' + urllib.parse.quote(ref.replace('/', '~')) + '?langue=2', timeout=60) or []
    except Exception:
        lst = []
    g, ids, mids = {}, {}, C.defaultdict(list)
    for r in lst:
        p = position(r)
        k = (str(r.get('MARQUE') or '').strip().upper(), str(r.get('MODELE') or '').strip(), r.get('CYLINDREE') or '', p)
        d, f = r.get('ANNEE_DEBUT') or 0, r.get('ANNEE_FIN') or 0
        g.setdefault(k, []).append((d, f))
        ids.setdefault(k, set()).add(r['IDMOTO'])
        mids[r['IDMOTO']].append(f"{d}-{f}" + {'Avant': 'a', 'Arrière': 'r', 'Avant et arrière': 'x'}.get(p, ''))
    rows = []
    for (ma, mo, cy, pos), ans in g.items():
        ans.sort()
        mg = []
        for d, f in ans:
            if mg and d <= mg[-1][1] + 1:
                mg[-1][1] = max(mg[-1][1], f)
            else:
                mg.append([d, f])
        annees = ', '.join(f"{d}–{f}" if f and f != d else f"{d}" for d, f in mg if d)
        rows.append(dict(marque=ma, modele=mo, cylindree=cy, annees=annees, position=pos, ids=sorted(ids[(ma, mo, cy, pos)])))
    rows.sort(key=lambda r: (r['marque'], r['modele'], str(r['cylindree'])))
    moto_ids = ';'.join(f"{i}:{','.join(sorted(set(v)))}" for i, v in sorted(mids.items()))
    return rows, moto_ids


def compat_html(rows):
    if not rows:
        return ''
    posu = {r['position'] for r in rows}
    n = len(rows)
    s = 's' if n > 1 else ''
    h = [f"<h3>Motos et scooters compatibles ({n} modèle{s})</h3>"]
    if len(posu) == 1 and list(posu)[0]:
        h.append(f"<p>Position : {esc(list(posu)[0].lower())}.</p>")
    abr = {'Avant': 'av.', 'Arrière': 'arr.', 'Avant et arrière': 'av. + arr.', '': ''}
    par = {}
    for r in rows:
        cy = str(r['cylindree'] or '')
        mo = r['modele'] if (not cy or cy in r['modele']) else f"{r['modele']} {cy}"
        ext = r['annees'] + (', ' + abr[r['position']] if len(posu) > 1 and r['position'] else '')
        txt = f"{esc(mo)} ({esc(ext)})" if ext else esc(mo)
        data_m = ' '.join(str(i) for i in r['ids'])
        par.setdefault(r['marque'], []).append(f'<span data-m="{data_m}">{txt}</span>')
    lignes = ''.join(f"<tr><th>{esc(m)}</th><td>{' · '.join(v)}</td></tr>" for m, v in par.items())
    h.append(f'<details class="mw-compat-bloc"><summary>Voir la liste des {n} modèle{s} compatible{s}</summary>'
             f'<table class="mw-compat"><tbody>{lignes}</tbody></table></details>')
    return ''.join(h)


def photos(ref):
    try:
        d = sifam_get('photos/' + urllib.parse.quote(ref.replace('/', '~')) + '?generique=0', timeout=60) or []
    except Exception:
        return []
    urls = []
    for x in d:
        for u in (x.get('fichier') or '').split('|'):
            if u and u not in urls:
                urls.append(u)
    return urls[:5]


def chemin_de(ref, a, cats):
    i = cats['refs'].get(ref)
    if i is None:
        i = cats['familles'].get(f"{(a.get('designation_famille') or '').strip()}|{(a.get('designation_sfamille') or '').strip()}")
        if i is None:
            return None, 'nouvelle référence SIFAM sans catégorie connue (à classer avec Claude)'
    ch = cats['chemins'][i]
    if not ch:
        return None, 'catégorie écartée (kits déco, goodies, « X », « À décider »…)'
    if any(ch.startswith(x) for x in EXCLURE_CHEMIN):
        return None, 'pneus en stand-by'
    return ch, None


def fiche(a, ref, chemin, s, rows, moto_ids, urls):
    niveaux = chemin.split(' > ')
    if a.get('designation_sfamille') == 'Machoire' and niveaux[-1] == 'Plaquettes de frein':
        niveaux[-1] = 'Mâchoires de frein'
    if len(niveaux) == 2 and niveaux[0] == 'Pièces entretien':
        niveaux.append(AUTRES.get(niveaux[1], 'Autres pièces ' + niveaux[1].lower()))
    if niveaux == ['Accessoires']:
        niveaux.append('Autres accessoires')
    souscat = niveaux[-1]
    poids = f2(a.get('poids'))
    poids_estime = poids <= 0
    if poids_estime:
        poids = 0.3
    transport = transport_article(poids, souscat)
    if transport is None:
        return None, 'plus de 70 kg : prix à fixer à la main'
    t_om = taux_om_chemin(' > '.join(niveaux))
    achat, public = s['achat'], s['public']
    om = round((achat + transport) * t_om / 100, 2)
    revient = round(achat + transport + om, 2)
    prix = prix_grille_b(revient)
    if niveaux[0] == 'Équipement pilote':
        prix = max(prix, plancher_pilote(public))
    marque = (a.get('marque') or '').strip() or 'SIFAM'
    des = re.sub(r'\s+', ' ', (a.get('designation') or '').strip()).strip(' -/')
    titre = des
    if ref.lower() not in des.lower():
        titre += f" – {marque} {ref}" if marque.lower() not in des.lower() else f" – {ref}"
    elif marque.lower() not in des.lower():
        titre += f" – {marque}"
    h = [f"<p><strong>{esc(des)}</strong> – marque {esc(marque)}, référence {esc(ref)}.</p>"]
    for para in [x.strip() for x in re.split(r'\n\s*\n', a.get('description') or '') if x.strip()][:5]:
        h.append('<p>' + esc(para).replace('\n', '<br>') + '</p>')
    car = [('Marque', marque), ('Référence', ref)]
    pos = sorted({r['position'] for r in rows if r['position']})
    if len(pos) == 1:
        car.append(('Position', pos[0]))
    for i in range(10):
        v, lab = str(a.get(f'c{i}') or '').strip(), (a.get(f'c{i}_label') or '').strip()
        if lab and v and lab.lower() != 'qualité' and v != '0':
            car.append((lab, v))
    if not poids_estime:
        car.append(('Poids', f"{poids:.2f} kg".replace('.', ',')))
    if a.get('ean'):
        car.append(('EAN', a['ean']))
    h.append('<h3>Caractéristiques</h3><ul>' + ''.join(f"<li><strong>{esc(k)} :</strong> {esc(v)}</li>" for k, v in car) + '</ul>')
    h.append(compat_html(rows))
    if niveaux[0] != 'Équipement pilote':
        h.append("<p>Vérifiez la compatibilité avec votre moto ou scooter avant de commander. En cas de doute, contactez-nous avec la marque, "
                 "le modèle et l'année de votre véhicule : nous vérifions pour vous.</p>")
    h.append("<p>Prix tout compris : transport et octroi de mer inclus, livraison en Guadeloupe.</p>")
    marques_moto = sorted({r['marque'].title() for r in rows})
    tags = niveaux + ['SIFAM', 'import-sifam-' + datetime.date.today().strftime('%Y%m%d'), 'ajout-auto'] + [f'Compatible {m}' for m in marques_moto][:100]
    if poids_estime:
        tags.append('poids-estime')
    ids_moto = sorted({i for r in rows for i in r['ids']})
    tags += [f'moto-{i}' for i in ids_moto][:max(0, 250 - len(tags))]
    brouillon = bool((not urls) or souscat in FREINAGE_PHOTOS or souscat == 'Batteries'
                     or re.search(r'batterie|lithium|booster|spray|a[ée]rosol|bombe', titre, re.I))

    def mf(k, t, v):
        return {'namespace': 'tarif', 'key': k, 'type': t, 'value': str(v)}
    metafields = [mf('ref_sifam', 'single_line_text_field', ref), mf('prix_achat_ht', 'number_decimal', achat),
                  mf('prix_public_ttc', 'number_decimal', round(public * 1.2, 2)), mf('poids_taxable_kg', 'number_decimal', max(poids, 0.1)),
                  mf('mode_transport', 'single_line_text_field', 'avion'), mf('cout_transport', 'number_decimal', transport),
                  mf('taux_om', 'number_decimal', t_om), mf('montant_om', 'number_decimal', om), mf('cout_revient', 'number_decimal', revient),
                  mf('prix_auto', 'number_decimal', prix), mf('regle_appliquee', 'single_line_text_field', 'Grille B (ajout automatique)'),
                  mf('coefficient', 'number_decimal', 1.0)]
    if moto_ids:
        metafields.append({'namespace': 'moto', 'key': 'ids', 'type': 'multi_line_text_field', 'value': moto_ids})
    if rows:
        metafields.append({'namespace': 'moto', 'key': 'compatibilites', 'type': 'json', 'value': json.dumps(
            {'colonnes': ['marque', 'modele', 'cylindree', 'annees', 'position'],
             'lignes': [[r['marque'], r['modele'], r['cylindree'], r['annees'], r['position']] for r in rows]}, ensure_ascii=False, separators=(',', ':'))})
    handle = re.sub(r'[^a-z0-9]+', '-', f"{souscat}-{marque}-{ref}".lower().translate(str.maketrans('éèêëàâäîïôöûüùç', 'eeeeaaaiioouuuc'))).strip('-')
    seo_desc = titre + (f", compatible {', '.join(marques_moto[:4])}" if marques_moto else '') + '. Prix tout compris livré en Guadeloupe.'
    inp = {'title': titre[:255], 'handle': handle, 'descriptionHtml': ''.join(h), 'vendor': marque, 'productType': souscat,
           'status': 'DRAFT', 'tags': tags + ['a-valider'],   # toujours en brouillon : validation par Florent (04/11/2026)
           'seo': {'title': titre[:70], 'description': seo_desc[:320]},
           'productOptions': [{'name': 'Title', 'values': [{'name': 'Default Title'}]}],
           'variants': [{'optionValues': [{'optionName': 'Title', 'name': 'Default Title'}], 'price': f"{prix:.2f}", 'sku': PREFIXE_SKU + ref,
                         'barcode': a.get('ean') or None, 'inventoryPolicy': 'DENY', 'taxable': True,
                         'inventoryItem': {'tracked': True, 'cost': f"{revient:.2f}", 'requiresShipping': True,
                                           'measurement': {'weight': {'value': round(max(poids, 0.1), 3), 'unit': 'KILOGRAMS'}}},
                         'inventoryQuantities': [{'locationId': LOCATION, 'name': 'available', 'quantity': min(s['q'], STOCK_MAX)}]}],
           'metafields': metafields}
    if urls:
        inp['files'] = [{'originalSource': u, 'contentType': 'IMAGE', 'alt': f"{titre} – photo {k + 1}"} for k, u in enumerate(urls)]
    return dict(input=inp, titre=titre, prix=prix, souscat=souscat, brouillon=brouillon), None


def ajout():
    t0 = time.time()
    sifam = sifam_stocks()
    if len(sifam) < MIN_ARTICLES_SIFAM:
        rapport.append(f"**Arrêt de sécurité** : SIFAM n'a renvoyé que {len(sifam)} articles.")
        return 1
    with gzip.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'categories_sifam.json.gz')) as f:
        cats = json.load(f)
    shop = Shopify()
    cat = shop.catalogue()
    ecrire('catalogue.json', cat)
    presents = {sku[len(PREFIXE_SKU):] for sku in cat}          # y compris archivés : un produit retiré n'est jamais recréé
    candidats = {ref: s for ref, s in sifam.items() if ref not in presents and s['q'] > 0 and s['achat'] > 0 and s['public'] > 0}
    log('Candidats (en stock chez SIFAM, absents du site) :', len(candidats))
    ecartes, exemples, a_creer = C.Counter(), C.defaultdict(list), []
    for fam in FAMILLES:
        if len(a_creer) >= MAX_AJOUTS:
            break
        deb = 0
        while True:
            lot = sifam_get(f'articles/{fam}?prices=1&labels=1&langue=2&debut={deb}&nombre=2000') or []
            for a in lot:
                ref = str(a.get('reference') or '').strip()
                s = candidats.get(ref)
                if not s:
                    continue
                ch, raison = chemin_de(ref, a, cats)
                if not raison:
                    if a.get('interdictionDropshipping') == 'O':
                        raison = 'dropshipping interdit par SIFAM'
                    elif gant_hiver(a):
                        raison = 'exclu : gants hiver / mi-saison / chauffants'
                    elif exclu_choix(a):
                        raison = exclu_choix(a)
                    elif s.get('cond', 1) > 1:
                        raison = 'vendu par lot chez SIFAM'
                    elif fam in FAMILLES_DECLINEES:
                        raison = 'casques / vêtements (tailles et couleurs) : à ajouter avec Claude'
                if raison:
                    ecartes[raison] += 1
                    if len(exemples[raison]) < 5:
                        exemples[raison].append(f"{ref} {a.get('designation') or ''}".strip())
                    continue
                a_creer.append((ref, a, ch, s))
            if len(lot) < 2000 or len(a_creer) >= MAX_AJOUTS:
                break
            deb += 2000
    limite = len(a_creer) >= MAX_AJOUTS
    a_creer = a_creer[:MAX_AJOUTS]
    log('À créer :', len(a_creer))
    crees, brouillons, erreurs, lignes = 0, 0, 0, []
    for ref, a, ch, s in a_creer:
        rows, moto_ids = compat(ref)
        urls = photos(ref)
        f, raison = fiche(a, ref, ch, s, rows, moto_ids, urls)
        if raison:
            ecartes[raison] += 1
            continue
        etiquette = f"- {PREFIXE_SKU}{ref} : {f['titre'][:80]} — {f['prix']:.2f} € ({f['souscat']}{', à contrôler de près : pas de photo, freinage ou batterie' if f['brouillon'] else ''})"
        if not SIMULATION:
            try:
                r = shop.gql(MUT_CREATION, {'i': f['input']})['productSet']
            except Exception as e:
                erreurs += 1
                lignes.append(f'- {ref} : ERREUR {str(e)[:150]}')
                continue
            if r.get('userErrors') or not r.get('product'):
                erreurs += 1
                lignes.append(f"- {ref} : ERREUR {json.dumps(r.get('userErrors'), ensure_ascii=False)[:150]}")
                continue
            try:
                shop.gql(MUT_PUBLIER, {'id': r['product']['id'], 'p': PUBLICATION})
            except Exception:
                pass
        crees += 1
        brouillons += f['brouillon']
        lignes.append(etiquette)
    rapport.append('## Ajout hebdomadaire des articles SIFAM de nouveau en stock')
    rapport.append(f"{len(candidats)} articles en stock chez SIFAM absents du site ; {crees} créé(s) en brouillon (tag a-valider), "
                   f"dont {brouillons} à contrôler de près (pas de photo, freinage ou batterie), {erreurs} erreur(s), {round(time.time() - t0)} s."
                   + (" **Simulation : rien n'a été créé.**" if SIMULATION else ''))
    if limite:
        rapport.append(f'Limite de {MAX_AJOUTS} ajouts atteinte : la suite sera ajoutée au prochain passage.')
    if ecartes:
        rapport.append('\n### Non ajoutés')
        for r, n in ecartes.most_common():
            rapport.append(f'- **{r}** : {n}' + (f" (ex. : {'; '.join(exemples[r])})" if exemples[r] else ''))
    if lignes:
        rapport.append('\n### Produits créés')
        rapport.extend(lignes)
    return 1 if erreurs else 0


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'rapide'
    try:
        code = ajout() if mode == 'ajout' else passage(mode)
    except Exception as e:
        rapport.append(f'**Erreur** : {e}')
        code = 1
    texte = '\n'.join(rapport[:600])
    print(texte)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(texte + '\n')
    sys.exit(code)
