# -*- coding: utf-8 -*-
"""
geometry_processing.py

Algorithmes de vérification et de correction géométrique pour les couches
de polygones, utilisés par RatfQgisDialog.

Contrôles implémentés :
    1. Distance minimale entre les sommets    (vérifier / corriger)
    2. Distance maximale entre les sommets    (vérifier / corriger)
    3. Proximité des segments intra-géométrie (détecter / montrer)
    4. Proximité des segments inter-géométrie (détecter / montrer)
    5. Angles internes de bordures             (détecter / montrer)
    6. Superficie minimale à considérer         (filtre appliqué aux 1-5)

Les distances/angles sont exprimés dans l'unité du CRS de la couche
(généralement des mètres pour un CRS projeté) ; les angles en degrés.

NOTE : ce module a été rédigé pour l'API PyQGIS mais n'a pas pu être
exécuté dans un vrai QGIS lors de sa génération (environnement de
développement sans QGIS installé). À tester sur un petit jeu de
données avant un usage en production, en particulier les contrôles de
proximité de segments (les plus coûteux en calcul).
"""

import math

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QVariant


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
    """Superficie en hectares. Hypothèse : CRS projeté en mètres."""
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


def correct_min_vertex_distance(layer, min_dist, min_area_ha=0):
    """Fusionne les sommets trop rapprochés via removeDuplicateNodes().
    Retourne le nombre d'entités modifiées."""
    modified = 0
    layer.startEditing()
    try:
        for feature in _features_to_process(layer, min_area_ha):
            geom = QgsGeometry(feature.geometry())
            if geom.removeDuplicateNodes(epsilon=min_dist, useZValues=False):
                layer.changeGeometry(feature.id(), geom)
                modified += 1
    finally:
        layer.commitChanges()
    return modified


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


def correct_max_vertex_distance(layer, max_dist, min_area_ha=0):
    """Densifie les segments trop longs via densifyByDistance().
    Retourne le nombre d'entités modifiées."""
    modified = 0
    layer.startEditing()
    try:
        for feature in _features_to_process(layer, min_area_ha):
            densified = feature.geometry().densifyByDistance(max_dist)
            if densified is not None and not densified.isEmpty():
                layer.changeGeometry(feature.id(), densified)
                modified += 1
    finally:
        layer.commitChanges()
    return modified


# ---------------------------------------------------------------------------
# 3. Proximité des segments intra-géométrie
# ---------------------------------------------------------------------------

def _segment_distance(a1, a2, b1, b2):
    g1 = QgsGeometry.fromPolylineXY([a1, a2])
    g2 = QgsGeometry.fromPolylineXY([b1, b2])
    return g1.distance(g2)


def check_intra_geometry_proximity(layer, tolerance, min_area_ha=0):
    """Détecte les paires de segments NON ADJACENTS d'une même entité
    dont la distance est inférieure à `tolerance`.

    ATTENTION : O(n²) par entité. Pour des polygones très détaillés,
    envisager un filtre de superficie minimale pour limiter la charge.

    Retourne [{"fid", "point", "distance"}].
    """
    issues = []
    for feature in _features_to_process(layer, min_area_ha):
        geom = feature.geometry()
        segments = []
        for part_idx, ring_idx, ring in _iter_rings(geom):
            ring_key = (part_idx, ring_idx)
            for i in range(len(ring) - 1):
                segments.append((ring_key, i, ring[i], ring[i + 1]))

        ring_lengths = {}
        for part_idx, ring_idx, ring in _iter_rings(geom):
            ring_lengths[(part_idx, ring_idx)] = len(ring) - 1

        n = len(segments)
        for i in range(n):
            key_i, seg_i, a1, a2 = segments[i]
            for j in range(i + 1, n):
                key_j, seg_j, b1, b2 = segments[j]

                if key_i == key_j:
                    if abs(seg_i - seg_j) <= 1:
                        continue
                    ring_len = ring_lengths[key_i]
                    if {seg_i, seg_j} == {0, ring_len - 1}:
                        continue

                d = _segment_distance(a1, a2, b1, b2)
                if d < tolerance:
                    mid = QgsPointXY(
                        (a1.x() + a2.x() + b1.x() + b2.x()) / 4.0,
                        (a1.y() + a2.y() + b1.y() + b2.y()) / 4.0,
                    )
                    issues.append({"fid": feature.id(), "point": mid, "distance": d})
    return issues


# ---------------------------------------------------------------------------
# 4. Proximité des segments inter-géométrie
# ---------------------------------------------------------------------------

def check_inter_geometry_proximity(layer, tolerance, min_area_ha=0):
    """Détecte les paires de segments appartenant à deux entités
    différentes dont la distance est inférieure à `tolerance`.
    Utilise un index spatial pour limiter les comparaisons.

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
                    if d < tolerance:
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
    """Construit une couche mémoire de points à partir des anomalies
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
