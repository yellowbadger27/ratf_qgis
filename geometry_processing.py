# -*- coding: utf-8 -*-
"""
geometry_processing.py

Algorithmes de vérification et de correction géométrique pour les couches
de polygones, utilisés par RatfQgisDialog.

Contrôles implémentés :
    1. Distance minimale entre les sommets    
    2. Distance maximale entre les sommets    
    3. Proximité des segments intra-géométrie 
    4. Proximité des segments inter-géométrie 
    5. Angles internes de bordures             
    6. Superficie minimale à considérer         

Les distances/angles sont exprimés dans l'unité du CRS de la couche
(généralement des mètres pour un CRS projeté) ; les angles en degrés.

"""

import math

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor


# ---------------------------------------------------------------------------
# Utilitaires généraux
# ---------------------------------------------------------------------------

def _iter_rings(geometry):
    """Itère sur tous les anneaux (extérieurs + trous) d'une géométrie
    polygone ou multi-polygone.

    Retourne une liste de tuples (part_index, ring_index, [QgsPointXY, ...]).
    Chaque anneau est fermé (premier point == dernier point).
    """
    rings = []
    if geometry is None or geometry.isEmpty():
        return rings

    if geometry.isMultipart():
        for part_idx, polygon in enumerate(geometry.asMultiPolygon()):
            for ring_idx, ring in enumerate(polygon):
                rings.append((part_idx, ring_idx, ring))
    else:
        for ring_idx, ring in enumerate(geometry.asPolygon()):
            rings.append((0, ring_idx, ring))
    return rings


def _feature_area_hectares(geometry):
    """Superficie en hectares. """
    return geometry.area() / 10000.0


def _passes_area_filter(feature, min_area_ha):
    if min_area_ha is None or min_area_ha <= 0:
        return True
    geom = feature.geometry()
    if geom is None or geom.isEmpty():
        return False
    return _feature_area_hectares(geom) >= min_area_ha


def _features_to_process(layer, min_area_ha):
    for feature in layer.getFeatures():
        if _passes_area_filter(feature, min_area_ha):
            yield feature


def unique_layer_name(base_name):
    """Retourne un nom de couche disponible dans le projet, basé sur
    `base_name`. Le premier essai est `base_name` tel quel ; s'il est
    déjà pris, un suffixe numérique est ajouté à partir de 2
    (ex. "X_corrige", puis "X_corrige2", "X_corrige3", ...)."""
    if not QgsProject.instance().mapLayersByName(base_name):
        return base_name
    i = 2
    while QgsProject.instance().mapLayersByName(f"{base_name}{i}"):
        i += 1
    return f"{base_name}{i}"


def _duplicate_layer(layer, name):
    """Duplique `layer` (structure + entités) dans une nouvelle couche
    mémoire, sans toucher à la couche source. Sert de base aux fonctions
    de correction, qui écrivent sur la copie plutôt que sur l'original."""
    geom_type = QgsWkbTypes.displayString(layer.wkbType())
    new_layer = QgsVectorLayer(f"{geom_type}?crs={layer.crs().authid()}", name, "memory")
    provider = new_layer.dataProvider()
    provider.addAttributes(layer.fields())
    new_layer.updateFields()

    new_features = []
    for feature in layer.getFeatures():
        feat = QgsFeature(new_layer.fields())
        feat.setGeometry(QgsGeometry(feature.geometry()))
        feat.setAttributes(feature.attributes())
        new_features.append(feat)
    provider.addFeatures(new_features)
    new_layer.updateExtents()
    return new_layer


# ---------------------------------------------------------------------------
# 1. Distance minimale entre les sommets
# ---------------------------------------------------------------------------

def check_min_vertex_distance(layer, min_dist, min_area_ha=0):
    """Retourne [{"fid", "point", "distance"}] pour chaque sommet plus
    proche que `min_dist` de son successeur."""
    issues = []
    for feature in _features_to_process(layer, min_area_ha):
        for _p, _r, ring in _iter_rings(feature.geometry()):
            for i in range(len(ring) - 1):
                p1, p2 = ring[i], ring[i + 1]
                d = p1.distance(p2)
                if 0 < d < min_dist:
                    issues.append({"fid": feature.id(), "point": QgsPointXY(p1), "distance": d})
    return issues


# ---------------------------------------------------------------------------
# Fusion ciblée des sommets CONSÉCUTIFS trop rapprochés
# ---------------------------------------------------------------------------

def _collapse_close_consecutive_points(ring, min_dist):
    """Retourne un nouvel anneau (fermé) où chaque sommet trop proche
    (< min_dist) de son PRÉDÉCESSEUR CONSERVÉ est retiré.

    Ne descend jamais sous un anneau valide (au moins 3 sommets distincts).
    """
    if len(ring) < 4:
        return list(ring)

    pts = ring[:-1]  # anneau ouvert (le dernier point == le premier)
    result = [pts[0]]
    for pt in pts[1:]:
        if result[-1].distance(pt) < min_dist:
            continue  # trop proche du sommet précédent conservé : on saute
        result.append(pt)

    # Vérifier aussi la fermeture (dernier sommet conservé vs premier)
    if len(result) >= 3 and result[-1].distance(result[0]) < min_dist:
        result.pop()

    if len(result) < 3:
        return list(ring)  # ne pas dégénérer : garder l'anneau original

    result.append(QgsPointXY(result[0]))  # refermer l'anneau
    return result


def _collapse_close_consecutive_vertices(geometry, min_dist):
    """Applique _collapse_close_consecutive_points() à chaque anneau
    (extérieur + trous) d'une géométrie polygone ou multi-polygone."""
    if geometry.isMultipart():
        new_parts = []
        for polygon in geometry.asMultiPolygon():
            new_parts.append(
                [_collapse_close_consecutive_points(ring, min_dist) for ring in polygon])
        return QgsGeometry.fromMultiPolygonXY(new_parts)
    else:
        new_rings = [_collapse_close_consecutive_points(ring, min_dist)
                     for ring in geometry.asPolygon()]
        return QgsGeometry.fromPolygonXY(new_rings)


def correct_min_vertex_distance(layer, min_dist, min_area_ha=0, target_layer=None):
    """Fusionne les sommets CONSÉCUTIFS trop rapprochés (voir
    _collapse_close_consecutive_vertices) sans toucher aux sommets non
    adjacents proches.

    Ne modifie JAMAIS `layer` : les corrections sont appliquées sur une
    couche mémoire distincte.
    - Si `target_layer` est fourni (ex. issue d'une correction précédente
      dans la même exécution), les corrections y sont appliquées
      directement, pour chaîner plusieurs corrections sans dupliquer la
      couche à chaque étape.
    - Sinon, une nouvelle couche mémoire "<nom>_corrige" est créée à
      partir de `layer`.

    Retourne (couche_corrigee, nombre_anomalies_corrigees). 
    """
    out_layer = target_layer if target_layer is not None \
        else _duplicate_layer(layer, unique_layer_name(f"{layer.name()}_corrige"))

    issues_avant = check_min_vertex_distance(out_layer, min_dist, min_area_ha)
    fids_a_corriger = {issue["fid"] for issue in issues_avant}

    out_layer.startEditing()
    try:
        for feature in out_layer.getFeatures():
            if feature.id() not in fids_a_corriger:
                continue
            geom = feature.geometry()
            new_geom = _collapse_close_consecutive_vertices(geom, min_dist)
            if new_geom is not None and not new_geom.isEmpty():
                out_layer.changeGeometry(feature.id(), new_geom)
    finally:
        out_layer.commitChanges()

    issues_apres = check_min_vertex_distance(out_layer, min_dist, min_area_ha)
    corrigees = len(issues_avant) - len(issues_apres)
    return out_layer, corrigees



# ---------------------------------------------------------------------------
# 2. Distance maximale entre les sommets
# ---------------------------------------------------------------------------

def check_max_vertex_distance(layer, max_dist, min_area_ha=0):
    """Retourne [{"fid", "point_debut", "point_fin", "distance"}] pour
    chaque segment plus long que `max_dist`."""
    issues = []
    for feature in _features_to_process(layer, min_area_ha):
        for _p, _r, ring in _iter_rings(feature.geometry()):
            for i in range(len(ring) - 1):
                p1, p2 = ring[i], ring[i + 1]
                d = p1.distance(p2)
                if d > max_dist:
                    issues.append({
                        "fid": feature.id(),
                        "point_debut": QgsPointXY(p1),
                        "point_fin": QgsPointXY(p2),
                        "distance": d,
                    })
    return issues


def correct_max_vertex_distance(layer, max_dist, min_area_ha=0, target_layer=None):
    """Densifie les segments trop longs via densifyByDistance().

    Retourne (couche_corrigee, nombre_anomalies_corrigees).
    """
    out_layer = target_layer if target_layer is not None \
        else _duplicate_layer(layer, unique_layer_name(f"{layer.name()}_corrige"))

    issues_avant = check_max_vertex_distance(out_layer, max_dist, min_area_ha)
    fids_a_corriger = {issue["fid"] for issue in issues_avant}

    out_layer.startEditing()
    try:
        for feature in out_layer.getFeatures():
            if feature.id() not in fids_a_corriger:
                continue
            densified = feature.geometry().densifyByDistance(max_dist)
            if densified is not None and not densified.isEmpty():
                out_layer.changeGeometry(feature.id(), densified)
    finally:
        out_layer.commitChanges()

    issues_apres = check_max_vertex_distance(out_layer, max_dist, min_area_ha)
    corrigees = len(issues_avant) - len(issues_apres)
    return out_layer, corrigees


# ---------------------------------------------------------------------------
# 3. Proximité des segments intra-géométrie
# ---------------------------------------------------------------------------

def _segment_distance(a1, a2, b1, b2):
    g1 = QgsGeometry.fromPolylineXY([a1, a2])
    g2 = QgsGeometry.fromPolylineXY([b1, b2])
    return g1.distance(g2)


def check_intra_geometry_proximity(layer, tolerance, min_area_ha=0):
    """Détecte les SOMMETS d'une même entité qui ont au moins un autre
    sommet de la même entité (adjacent ou non, tous anneaux/parties
    confondus) à moins de `tolerance`.

    IMPORTANT : contrairement à une intuition "segment à segment", ce
    contrôle compare les SOMMETS entre eux, sans exclure les paires
    consécutives. En pratique, deux sommets consécutifs "normaux" sont
    presque toujours bien plus éloignés que `tolerance`, donc ça ne
    déclenche que pour de vrais segments dégénérés (déjà repérés par
    check_min_vertex_distance) ou pour de vrais sommets non adjacents
    rapprochés (auto-approche du contour). Une anomalie est rapportée
    par SOMMET concerné (dédoublonné), pas par paire — un sommet partagé
    entre deux paires proches n'est donc compté qu'une seule fois.

    ATTENTION : O(n²) par entité. Pour des polygones très détaillés,
    envisager un filtre de superficie minimale pour limiter la charge.

    Retourne [{"fid", "point", "distance"}] (une entrée par sommet
    concerné ; "distance" est la plus petite distance trouvée vers un
    autre sommet de la même entité).
    """
    issues = []
    for feature in _features_to_process(layer, min_area_ha):
        geom = feature.geometry()

        all_verts = []
        for _p, _r, ring in _iter_rings(geom):
            all_verts.extend(ring[:-1])

        n = len(all_verts)
        min_dist_par_sommet = {}
        for i in range(n):
            for j in range(i + 1, n):
                d = all_verts[i].distance(all_verts[j])
                if d < tolerance:
                    if i not in min_dist_par_sommet or d < min_dist_par_sommet[i]:
                        min_dist_par_sommet[i] = d
                    if j not in min_dist_par_sommet or d < min_dist_par_sommet[j]:
                        min_dist_par_sommet[j] = d

        for idx, d in min_dist_par_sommet.items():
            issues.append({
                "fid": feature.id(),
                "point": QgsPointXY(all_verts[idx]),
                "distance": d,
            })
    return issues


# ---------------------------------------------------------------------------
# 4. Proximité des segments inter-géométrie
# ---------------------------------------------------------------------------

def check_inter_geometry_proximity(layer, tolerance, min_area_ha=0,
                                    coincidence_epsilon=1e-6):
    """Détecte les paires de segments appartenant à deux entités
    différentes dont la distance est inférieure à `tolerance`.
    Utilise un index spatial pour limiter les comparaisons.

    IMPORTANT : deux polygones adjacents qui partagent légitimement une
    bordure (cas normal dans une mosaïque de blocs) ont des segments
    coïncidents, donc à distance ~0 le long de toute la bordure commune.
    Ce n'est PAS une anomalie. `coincidence_epsilon` définit le seuil en
    dessous duquel une distance est considérée comme une coïncidence de
    bordure valide (à cause des imprécisions flottantes, une bordure
    parfaitement partagée ne donne pas toujours exactement 0.0) plutôt
    que comme une vraie anomalie de proximité. Seules les distances
    strictement comprises entre `coincidence_epsilon` et `tolerance`
    (un vrai interstice ou "sliver", ni coïncident ni assez éloigné)
    sont retenues comme anomalies.

    Retourne [{"fid_a", "fid_b", "point", "distance"}].
    """
    issues = []
    features = {f.id(): f for f in _features_to_process(layer, min_area_ha)}
    if len(features) < 2:
        return issues

    index = QgsSpatialIndex()
    for f in features.values():
        index.addFeature(f)

    checked_pairs = set()

    for fid_a, feat_a in features.items():
        geom_a = feat_a.geometry()
        bbox = geom_a.boundingBox()
        bbox.grow(tolerance)

        for fid_b in index.intersects(bbox):
            if fid_b == fid_a or fid_b not in features:
                continue
            pair_key = tuple(sorted((fid_a, fid_b)))
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            geom_b = features[fid_b].geometry()
            if geom_a.distance(geom_b) > tolerance:
                continue

            segments_a = [(ring[i], ring[i + 1])
                          for _p, _r, ring in _iter_rings(geom_a)
                          for i in range(len(ring) - 1)]
            segments_b = [(ring[i], ring[i + 1])
                          for _p, _r, ring in _iter_rings(geom_b)
                          for i in range(len(ring) - 1)]

            for a1, a2 in segments_a:
                for b1, b2 in segments_b:
                    d = _segment_distance(a1, a2, b1, b2)
                    if coincidence_epsilon < d < tolerance:
                        mid = QgsPointXY(
                            (a1.x() + a2.x() + b1.x() + b2.x()) / 4.0,
                            (a1.y() + a2.y() + b1.y() + b2.y()) / 4.0,
                        )
                        issues.append({"fid_a": fid_a, "fid_b": fid_b, "point": mid, "distance": d})
    return issues


# ---------------------------------------------------------------------------
# 5. Angles internes de bordures
# ---------------------------------------------------------------------------

def _interior_angle_degrees(prev_pt, vertex_pt, next_pt):
    v1 = (prev_pt.x() - vertex_pt.x(), prev_pt.y() - vertex_pt.y())
    v2 = (next_pt.x() - vertex_pt.x(), next_pt.y() - vertex_pt.y())
    len1, len2 = math.hypot(*v1), math.hypot(*v2)
    if len1 == 0 or len2 == 0:
        return None
    dot = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (len1 * len2)))
    return math.degrees(math.acos(dot))


def check_internal_angles(layer, min_angle_deg, min_area_ha=0):
    """Détecte les sommets dont l'angle interne est inférieur à
    `min_angle_deg` (pointes / artefacts de digitalisation).

    Retourne [{"fid", "point", "angle"}].
    """
    issues = []
    for feature in _features_to_process(layer, min_area_ha):
        for _p, _r, ring in _iter_rings(feature.geometry()):
            pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
            n = len(pts)
            if n < 3:
                continue
            for i in range(n):
                angle = _interior_angle_degrees(pts[(i - 1) % n], pts[i], pts[(i + 1) % n])
                if angle is not None and angle < min_angle_deg:
                    issues.append({"fid": feature.id(), "point": QgsPointXY(pts[i]), "angle": angle})
    return issues


# ---------------------------------------------------------------------------
# Création d'une couche de résultats (points) pour l'option "Montrer"
# ---------------------------------------------------------------------------

def build_issues_point_layer(issues, crs, layer_name, extra_fields=None):
    """Construit une couche mémoire de points à partir des erreurs
    détectées, pour affichage dans le projet QGIS."""
    extra_fields = extra_fields or []

    mem_layer = QgsVectorLayer(f"Point?crs={crs.authid()}", layer_name, "memory")
    provider = mem_layer.dataProvider()

    fields = QgsFields()
    has_fid = bool(issues) and "fid" in issues[0]
    if has_fid:
        fields.append(QgsField("fid", QVariant.LongLong))
    for key in extra_fields:
        fields.append(QgsField(key, QVariant.Double))
    provider.addAttributes(fields)
    mem_layer.updateFields()

    new_features = []
    for issue in issues:
        point = issue.get("point")
        if point is None:
            continue
        feat = QgsFeature(mem_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(point))
        attrs = []
        if has_fid:
            attrs.append(issue.get("fid"))
        for key in extra_fields:
            attrs.append(issue.get(key))
        feat.setAttributes(attrs)
        new_features.append(feat)

    provider.addFeatures(new_features)
    mem_layer.updateExtents()
    return mem_layer


# ---------------------------------------------------------------------------
# Normalisation et fusion des anomalies dans une seule couche
# ---------------------------------------------------------------------------

def normalize_issues(issues, type_label, value_key, fid_key="fid", fid_b_key=None, value_suffix=""):
    """Convertit une liste d'anomalies brutes (format spécifique à chaque
    check_...) en une liste de dictionnaires au format commun :
        {"type", "fid", "fid_b", "point", "valeur"}

    - `type_label` : étiquette affichée dans le champ "type" de la couche
      finale (ex. "Distance min. entre sommets").
    - `value_key` : nom de la clé contenant la valeur numérique à afficher
      (ex. "distance", "angle").
    - `fid_key` / `fid_b_key` : noms des clés d'identifiant d'entité dans
      les dictionnaires source (varient selon le contrôle : "fid" seul,
      ou "fid_a"/"fid_b" pour la proximité inter-géométrie).
    - `value_suffix` : suffixe d'affichage (" m", "°", ...).

    Si l'anomalie ne contient pas de clé "point" mais bien
    "point_debut"/"point_fin" (cas de check_max_vertex_distance), le point
    milieu du segment est utilisé.
    """
    normalized = []
    for issue in issues:
        point = issue.get("point")
        if point is None and "point_debut" in issue and "point_fin" in issue:
            p1, p2 = issue["point_debut"], issue["point_fin"]
            point = QgsPointXY((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
        if point is None:
            continue

        value = issue.get(value_key)
        valeur_txt = f"{value:.2f}{value_suffix}" if value is not None else ""

        entry = {
            "type": type_label,
            "fid": issue.get(fid_key),
            "fid_b": issue.get(fid_b_key) if fid_b_key else None,
            "point": point,
            "valeur": valeur_txt,
        }
        normalized.append(entry)
    return normalized


def build_combined_point_layer(anomalies, crs, layer_name):
    """Construit une seule couche mémoire de points regroupant toutes les
    anomalies déjà normalisées (via normalize_issues), distinguées par le
    champ "type". Retourne None si `anomalies` est vide."""
    if not anomalies:
        return None

    mem_layer = QgsVectorLayer(f"Point?crs={crs.authid()}", layer_name, "memory")
    provider = mem_layer.dataProvider()

    fields = QgsFields()
    fields.append(QgsField("type", QVariant.String))
    fields.append(QgsField("fid", QVariant.LongLong))
    fields.append(QgsField("fid_b", QVariant.LongLong))
    fields.append(QgsField("valeur", QVariant.String))
    provider.addAttributes(fields)
    mem_layer.updateFields()

    new_features = []
    for anomaly in anomalies:
        point = anomaly.get("point")
        if point is None:
            continue
        feat = QgsFeature(mem_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(point))
        feat.setAttributes([
            anomaly.get("type"),
            anomaly.get("fid"),
            anomaly.get("fid_b"),
            anomaly.get("valeur"),
        ])
        new_features.append(feat)

    provider.addFeatures(new_features)
    mem_layer.updateExtents()
    return mem_layer


# ---------------------------------------------------------------------------
# Symbologie catégorisée de la couche d'erreurs (une couleur par "type")
# ---------------------------------------------------------------------------

# Palette de couleurs distinctes (issue de ColorBrewer "Set1"), suffisante
# pour les 5 types d'anomalies possibles ; se répète au-delà si jamais de
# nouveaux types de contrôle sont ajoutés.
_PALETTE_TYPES = [
    QColor("#e41a1c"),  # rouge
    QColor("#377eb8"),  # bleu
    QColor("#4daf4a"),  # vert
    QColor("#984ea3"),  # violet
    QColor("#ff7f00"),  # orange
    QColor("#ffff33"),  # jaune
    QColor("#a65628"),  # brun
]


# Libellés "type" connus, dans l'ordre voulu pour la légende — doivent
# rester synchronisés avec les `type_label` passés à normalize_issues()
# dans ratf_qgis_dialog.py.
ALL_ERROR_TYPES = [
    "Distance min. entre sommets",
    "Distance max. entre sommets",
    "Proximité intra-géométrie",
    "Proximité inter-géométrie",
    "Angles interne de bordure",
]


def apply_error_type_symbology(layer, field_name="type", symbol_size=1.5, all_types=None):
    """Applique une symbologie catégorisée à `layer` (couche de points
    d'erreurs produite par build_combined_point_layer), une couleur
    distincte par valeur unique du champ `field_name` (par défaut "type").

    Toutes les valeurs de `all_types` (par défaut ALL_ERROR_TYPES) figurent
    dans la légende, même si aucune entité de ce type n'est présente dans
    la couche pour cette exécution.
    Toute valeur trouvée dans les données mais absente de `all_types` est
    tout de même ajoutée à la suite (par ordre alphabétique), pour ne
    jamais perdre silencieusement une catégorie inattendue.

    Modifie `layer` en place (son renderer) et déclenche un rafraîchissement
    de l'affichage. Ne fait rien si le champ n'existe pas sur la couche.
    """
    if layer is None or layer.fields().indexFromName(field_name) < 0:
        return

    valeurs_connues = list(all_types) if all_types is not None else list(ALL_ERROR_TYPES)

    valeurs_presentes = {
        f[field_name] for f in layer.getFeatures()
        if f[field_name] not in (None, "")
    }
    valeurs_inattendues = sorted(valeurs_presentes - set(valeurs_connues))

    valeurs = valeurs_connues + valeurs_inattendues
    if not valeurs:
        return

    categories = []
    for i, valeur in enumerate(valeurs):
        symbol = QgsMarkerSymbol.createSimple({"name": "circle", "size": str(symbol_size)})
        symbol.setColor(_PALETTE_TYPES[i % len(_PALETTE_TYPES)])
        categories.append(QgsRendererCategory(valeur, symbol, str(valeur)))

    renderer = QgsCategorizedSymbolRenderer(field_name, categories)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
