"""Fitment lookup on the read replica: vehicle + part type -> ACES applications -> qualifiers -> SKUs -> PIES attributes.
Every step filters on indexed columns (acesappsload basevehicleID+parttermID, acesattrload appID, skuacesrel appID,
skuitemrel sku). Filters, in order: engine / position and vehicle qualifiers (per application), brand, part attributes."""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, utils

from app.config import MAX_APPS
from app.db import query
from app.resolve import Resolution, match_qualifier_values

NUMBER = r'\d+(?:\.\d+)?'
STOP_WORDS = {'with', 'without', 'and', 'for', 'the'}


@dataclass
class SearchResult:
    rows: list = field(default_factory=list)
    capped: bool = False
    facets: dict = field(default_factory=dict)               # qualifier type -> Counter(value -> applications)
    qualifier_selection: dict = field(default_factory=dict)  # qualifier type -> [values] being filtered on
    notes: list = field(default_factory=list)


def find_parts(conn, res: Resolution, timings: list) -> SearchResult:
    result = SearchResult()
    apps = query(conn, '''SELECT appID FROM catalog.acesappsload
                          WHERE basevehicleID IN %s AND parttermID IN %s AND deleteflag = 0 LIMIT %s''',
                 (tuple(res.base_vehicle_ids), tuple(res.part_term_ids), MAX_APPS), timings, 'fitment applications')
    if not apps:
        return result
    result.capped = len(apps) >= MAX_APPS
    app_ids = tuple(row[0] for row in apps)
    info = load_qualifiers(conn, app_ids, timings)

    # Engine / position first, then vehicle qualifiers (trim, drive type...) - typed, or picked by clicking
    candidates = [app_id for app_id in app_ids if fits_engine(info[app_id], res) and fits_position(info[app_id], res)]
    for app_id in candidates:
        for qualifier_type, values in info[app_id]['qualifiers'].items():
            result.facets.setdefault(qualifier_type, Counter()).update(values)
    result.qualifier_selection = (res.qualifier_override if res.qualifier_override is not None
                                  else match_qualifiers(res.qualifier_text, result.facets, result.notes))
    keep = tuple(app_id for app_id in candidates if fits_qualifiers(info[app_id], result.qualifier_selection))
    if not keep:
        return result

    parts = load_parts(conn, keep, info, timings)
    if res.brand:
        brand = res.brand.lower()
        matching = {sku: p for sku, p in parts.items() if brand in (p['aaia_brand'].lower(), p['fap_brand'].lower())}
        if not matching:
            result.notes.append(f'No {res.brand} parts for this search ({len(parts)} from other brands).')
        parts = matching
    if not parts:
        return result

    attributes = load_attributes(conn, tuple(parts), timings)
    if res.attribute_text:
        terms = [term.strip() for term in re.split(r'[,;]', res.attribute_text) if term.strip()]
        matching = {sku: p for sku, p in parts.items() if all(matches_attribute(term, attributes.get(sku, [])) for term in terms)}
        no_data = sum(1 for sku in parts if sku not in matching and not attributes.get(sku))
        result.notes.append(f'Part features "{res.attribute_text}": {len(matching)} of {len(parts)} parts match'
                            + (f' ({no_data} have no attribute data to check).' if no_data else '.'))
        parts = matching

    result.rows = sorted((to_row(p, attributes.get(sku, [])) for sku, p in parts.items()),
                         key=lambda r: (r['Brand'] or '', r['Part #'] or ''))
    return result


def load_qualifiers(conn, app_ids: tuple, timings: list) -> dict:
    info = defaultdict(lambda: {'engine_ids': set(), 'engines': set(), 'positions': set(), 'qty': set(), 'notes': set(),
                                'qualifiers': defaultdict(set)})
    for app_id, attr_id, name, attr_type in query(conn, 'SELECT appID, atrID, atrName, atrType FROM catalog.acesattrload WHERE appID IN %s',
                                                  (app_ids,), timings, 'fitment qualifiers'):
        app = info[app_id]
        if attr_type == 'EngineBase':
            app['engine_ids'].add(attr_id)
            app['engines'].add(name)
        elif attr_type == 'Position':
            app['positions'].add(name)
        elif attr_type == 'Qty':
            app['qty'].add(name)
        elif attr_type == 'Note':
            app['notes'].add(name)
        elif attr_type not in ('MfrLabel', 'YearRange') and name:
            app['qualifiers'][attr_type].add(name)
    return info


def load_parts(conn, app_ids: tuple, info: dict, timings: list) -> dict:
    """One entry per SKU, fitment details merged across its applications."""
    parts = {}
    for app_id, sku, aaia_brand, fap_brand, part_number, description in query(
            conn, '''SELECT r.appID, s.sku, s.supbrandaaianame, s.fapbrandname, s.partnumber, s.primarydescription
                     FROM catalog.skuacesrel r JOIN catalog.skumaster s ON s.sku = r.sku
                     WHERE r.appID IN %s AND r.deleteflag = 0
                     AND s.blocked = 0 AND s.record_origin IN ('pies', 'aces', 'duplicate_strip')''',
            (app_ids,), timings, 'SKUs'):
        part = parts.setdefault(sku, {'sku': sku, 'aaia_brand': aaia_brand or '', 'fap_brand': fap_brand or '',
                                      'part_number': part_number, 'description': description,
                                      'engines': set(), 'positions': set(), 'qty': set(), 'notes': set(), 'qualifiers': defaultdict(set)})
        app = info[app_id]
        for key in ('engines', 'positions', 'qty', 'notes'):
            part[key] |= app[key]
        for qualifier_type, values in app['qualifiers'].items():
            part['qualifiers'][qualifier_type] |= values
    return parts


def load_attributes(conn, skus: tuple, timings: list) -> dict:
    """PIES attributes per SKU from catalog.skuitemrel, e.g. ['Amperage Rating: 160A']."""
    attributes = defaultdict(list)
    for sku, name, value, uom in query(conn, '''SELECT sku, atrname, atrvalue, atruom FROM catalog.skuitemrel
                                               WHERE sku IN %s AND disabled = 0''', (skus,), timings, 'PIES attributes'):
        if name and value:
            attributes[sku].append(f'{name}: {value}{" " + uom if uom else ""}')
    return attributes


def to_row(part: dict, attributes: list) -> dict:
    qualifiers = part['qualifiers']
    return {'Brand': part['aaia_brand'] or part['fap_brand'], 'Part #': part['part_number'], 'Description': part['description'],
            'Attributes': '; '.join(sorted(set(attributes))),
            'Engine': ', '.join(sorted(part['engines'])) or 'All',
            'Submodel': ', '.join(sorted(qualifiers.get('SubModel', []))) or 'All',
            'Other fitment': '; '.join(f'{t}: {", ".join(sorted(v))}' for t, v in sorted(qualifiers.items()) if t != 'SubModel'),
            'Position': ', '.join(sorted(part['positions'])), 'Qty': ', '.join(sorted(part['qty'])),
            'Notes': '; '.join(sorted(part['notes'])), 'SKU': part['sku']}


def fits_engine(app: dict, res: Resolution) -> bool:
    # An application with no engine qualifier fits every engine
    return res.engine_base_ids is None or not app['engine_ids'] or bool(app['engine_ids'] & set(res.engine_base_ids))


def fits_position(app: dict, res: Resolution) -> bool:
    if not res.position or not app['positions']:
        return True
    words = res.position.lower().split()
    return any(all(word in position.lower() for word in words) for position in app['positions'])


def fits_qualifiers(app: dict, selection: dict) -> bool:
    # Same rule as engines: an application without that kind of qualifier fits every value of it
    return all(not app['qualifiers'].get(qualifier_type) or app['qualifiers'][qualifier_type] & set(values)
               for qualifier_type, values in selection.items())


def match_qualifiers(text: str, facets: dict, notes: list) -> dict:
    """Which qualifier values present in the results were typed, e.g. 'laramie, 4wd' -> {SubModel: [Laramie], DriveType: [4WD]}."""
    selection = defaultdict(set)
    unmatched = []
    for term in (t.strip() for t in re.split(r'[,;]', text)):
        if not utils.default_process(term):
            continue
        matched = False
        for qualifier_type, counter in facets.items():
            hits = match_qualifier_values(term, counter)
            selection[qualifier_type].update(hits)
            matched = matched or bool(hits)
        if not matched:
            unmatched.append(term)
    if unmatched:
        notes.append(f'{", ".join(unmatched)}: not a submodel, drive type or other fitment detail of these parts - ignored.')
    return {qualifier_type: sorted(values) for qualifier_type, values in selection.items() if values}


def matches_attribute(term: str, attributes: list) -> bool:
    """One typed feature against a part's PIES attributes ('Name: value'). Every number must appear in the value, and
    every word (3+ letters) must match a whole word of the value (spelling-tolerant: 'red' = 'Red', not 'Powdered')
    or start a word of the name ('amp' -> 'Amperage Rating'). So '160 amp' matches 'Amperage Rating: 160A'."""
    numbers = {float(n) for n in re.findall(NUMBER, term)}
    words = [w for w in utils.default_process(re.sub(NUMBER, ' ', term)).split() if len(w) >= 3 and w not in STOP_WORDS]
    if not numbers and not words:
        return True
    for attribute in attributes:
        name, _, value = attribute.partition(': ')
        if numbers and not numbers <= {float(n) for n in re.findall(NUMBER, value)}:
            continue
        name_words, value_words = utils.default_process(name).split(), utils.default_process(value).split()
        if all(any(fuzz.ratio(word, v) >= 85 for v in value_words) or any(n.startswith(word) for n in name_words) for word in words):
            return True
    return False
