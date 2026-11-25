"""Checks what the model understood against VCdb / PCdb: fixes known naming differences, fuzzy-matches typos and,
when something doesn't exist or is ambiguous, returns a question with valid options instead of guessing."""
import re
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, utils

from app.db import query
from app.reference import Reference

MAKE_ALIASES = {'chevy': 'Chevrolet', 'vw': 'Volkswagen', 'mercedes': 'Mercedes-Benz', 'benz': 'Mercedes-Benz',
                'caddy': 'Cadillac', 'olds': 'Oldsmobile', 'mb': 'Mercedes-Benz'}


@dataclass
class Question:
    prompt: str
    options: list = field(default_factory=list)  # [(button label, overrides patch)]
    expanded: bool = False                        # suggestions only: show open instead of collapsed


@dataclass
class Resolution:
    year: int = 0
    vehicle_label: str = ''
    base_vehicle_ids: list = field(default_factory=list)
    engine_label: str = 'All engines'
    engine_base_ids: list = None  # None = every engine
    part_label: str = ''
    part_term_ids: list = field(default_factory=list)
    position: str = ''
    brand: str = ''                  # '' = every brand
    qualifier_text: str = ''         # typed vehicle details (trim, drive...), matched against the results' qualifiers
    qualifier_override: dict = None  # qualifiers picked by clicking - replaces qualifier_text when set
    attribute_text: str = ''         # typed part features, matched against the results' PIES attributes
    notes: list = field(default_factory=list)        # corrections shown to the user
    questions: list = field(default_factory=list)    # must be answered before searching
    suggestions: list = field(default_factory=list)  # optional refinements

    @property
    def ready(self) -> bool:
        return bool(self.year and self.base_vehicle_ids and self.part_term_ids and not self.questions)


def similarity(a: str, b: str) -> float:
    """0-100. Exact-ish matches win over partial ones ('1500' scores higher on '1500' than on '1500 Classic')."""
    a, b = utils.default_process(a), utils.default_process(b)
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), 0.9 * fuzz.WRatio(a, b))


def model_similarity(typed: str, candidate: str) -> float:
    """Like similarity, but numbers in the typed model must appear in the candidate ('1500' never picks the 2500)."""
    score = similarity(typed, candidate)
    typed_numbers = set(re.findall(r'\d+', typed))
    if typed_numbers and not typed_numbers <= set(re.findall(r'\d+', candidate)):
        score -= 20
    return score


def part_similarity(typed: str, candidate: str) -> float:
    """Like similarity, minus 3 per extra word, so 'Oil Filter' ranks 'Engine Oil Filter' above 'Engine Oil Filter Element'."""
    extra_words = set(utils.default_process(candidate).split()) - set(utils.default_process(typed).split())
    return similarity(typed, candidate) - 3 * len(extra_words)


def match_qualifier_values(term: str, values) -> list:
    """Values (trims, drive types...) that a typed term refers to, best rule first:
    1. an exact value - 'Laramie' picks only 'Laramie', not 'Laramie Longhorn'
    2. values containing every typed word - 'Hellcat' picks 'SRT Hellcat', 'SRT Hellcat Redeye', ...
    3. close spelling - a short value inside the typed text ('4WD' in 'laramie 4wd') or a near-identical one (typos)"""
    typed = utils.default_process(term)
    processed = {value: utils.default_process(value) for value in values}
    processed = {value: p for value, p in processed.items() if len(p) >= 2}
    exact = [value for value, p in processed.items() if fuzz.ratio(p, typed) >= 95]
    if exact:
        return exact
    words = set(typed.split())
    containing = [value for value, p in processed.items() if words <= set(p.split())]
    if containing:
        return containing
    return [value for value, p in processed.items()
            if (fuzz.partial_ratio(p, typed) if len(p) <= len(typed) else fuzz.ratio(p, typed)) >= 90]


def resolve(parsed: dict, overrides: dict, ref: Reference, conn, timings: list) -> Resolution:
    res = Resolution(position=(parsed.get('position') or '').strip(),
                     qualifier_text=(parsed.get('qualifiers') or '').strip(),
                     qualifier_override=overrides.get('qualifiers'),
                     attribute_text=(overrides['attributes'] if 'attributes' in overrides else parsed.get('attributes') or '').strip())
    resolve_brand(parsed, overrides, ref, res)
    if not resolve_year(parsed, ref, res):
        return res
    if not resolve_vehicle(parsed, overrides, ref, res, conn, timings):
        return res
    resolve_engine(parsed, overrides, conn, res, timings)
    resolve_part_type(parsed, overrides, ref, conn, res, timings)
    return res


def resolve_brand(parsed: dict, overrides: dict, ref: Reference, res: Resolution) -> None:
    if 'brand' in overrides:
        res.brand = overrides['brand']
        return
    typed = (parsed.get('brand') or '').strip()
    if not typed:
        return
    if typed.lower() in ref.brands:
        res.brand = ref.brands[typed.lower()]
        return
    scored = sorted(((similarity(typed, name), name) for name in ref.brands.values()), reverse=True)
    top_score, top = scored[0]
    options = [(name, {'brand': name}) for _, name in scored[:6]] + [('Any brand', {'brand': ''})]
    if top_score < 75 or (len(scored) > 1 and scored[1][0] >= top_score - 3):
        res.questions.append(Question(f'Which brand is "{typed}"?', options))
        return
    res.brand = top
    res.notes.append(f'"{typed}" → {top}')


def resolve_year(parsed: dict, ref: Reference, res: Resolution) -> bool:
    year = int(parsed.get('year') or 0)
    if not year:
        res.questions.append(Question('Which model year? Add the year to the search.'))
        return False
    if year not in ref.vehicles_by_year:
        res.questions.append(Question(f'No vehicles for year {year} in VCdb. Check the year.'))
        return False
    res.year = year
    return True


def normalize_make_model(year: int, make: str, model: str, notes: list) -> tuple:
    alias = MAKE_ALIASES.get(make.lower())
    if alias:
        notes.append(f'"{make}" → {alias}')
        make = alias
    if year >= 2011 and make.lower() == 'dodge' and model.lower().startswith('ram'):
        rest = model[3:].strip()
        notes.append(f'"Dodge {model}" → Ram {rest} (Ram is its own make from 2011)'.replace('  ', ' '))
        make, model = 'Ram', rest
    return make, model


def resolve_vehicle(parsed: dict, overrides: dict, ref: Reference, res: Resolution, conn, timings: list) -> bool:
    candidates = ref.vehicles_by_year[res.year]  # [(base_vehicle_id, make, model)]
    if overrides.get('base_vehicle_id'):
        chosen = next(c for c in candidates if c[0] == overrides['base_vehicle_id'])
        res.base_vehicle_ids, res.vehicle_label = [chosen[0]], f'{res.year} {chosen[1]} {chosen[2]}'
        return True
    make, model = normalize_make_model(res.year, (parsed.get('make') or '').strip(), (parsed.get('model') or '').strip(), res.notes)
    if not make and not model:
        res.questions.append(Question('Which vehicle? Add the make and model to the search.'))
        return False
    if not model:
        options = sorted((c for c in candidates if similarity(make, c[1]) >= 85), key=lambda c: c[2])
        if options and res.qualifier_text:
            # A trim typed without a model ('dodge hellcat'): VCdb submodels tell which models have it
            with_trim = models_with_trim(res.qualifier_text, options, conn, timings)
            if len(with_trim) == 1:
                chosen = with_trim[0]
                res.base_vehicle_ids, res.vehicle_label = [chosen[0]], f'{res.year} {chosen[1]} {chosen[2]}'
                res.notes.append(f'"{res.qualifier_text}" is a {chosen[2]} trim → {res.vehicle_label}')
                return True
            if with_trim:
                res.questions.append(Question(f'Which {res.year} {make} with "{res.qualifier_text}"?',
                                              [(c[2], {'base_vehicle_id': c[0]}) for c in with_trim]))
                return False
        res.questions.append(Question(f'Which {res.year} {make} model?', [(c[2], {'base_vehicle_id': c[0]}) for c in options][:20]))
        return False
    scored = sorted(((0.35 * (similarity(make, c[1]) if make else 50) + 0.65 * model_similarity(model, c[2]), c) for c in candidates),
                    reverse=True)
    top_score, top = scored[0]
    options = [(f'{c[1]} {c[2]}', {'base_vehicle_id': c[0]}) for _, c in scored[:6]]
    if top_score < 65:
        res.questions.append(Question(f'No {res.year} {make} {model} in VCdb. Did you mean one of these?', options))
        return False
    close = [c for score, c in scored[1:] if score >= top_score - 5]
    if close:
        res.questions.append(Question(f'Which {res.year} vehicle did you mean?', options[:len(close) + 1]))
        return False
    res.base_vehicle_ids, res.vehicle_label = [top[0]], f'{res.year} {top[1]} {top[2]}'
    if utils.default_process(f'{make} {model}') != utils.default_process(f'{top[1]} {top[2]}'):
        res.notes.append(f'"{make} {model}" → {top[1]} {top[2]}')
    others = [(label, patch) for (score, c), (label, patch) in zip(scored[1:4], options[1:4]) if score >= top_score - 15]
    if others:
        res.suggestions.append(Question('Different vehicle?', others))
    return True


def models_with_trim(qualifier_text: str, candidates: list, conn, timings: list) -> list:
    """Candidates [(base_vehicle_id, make, model)] whose VCdb submodels match any typed qualifier term."""
    submodels = defaultdict(set)
    for base_vehicle_id, submodel in query(conn, '''SELECT DISTINCT v.BaseVehicleID, s.SubmodelName FROM vcdb.vehicle v
                                                    JOIN vcdb.submodel s ON s.SubmodelID = v.SubmodelID
                                                    WHERE v.BaseVehicleID IN %s''',
                                           (tuple(c[0] for c in candidates),), timings, 'VCdb submodels for make'):
        submodels[base_vehicle_id].add(submodel)
    terms = [t.strip() for t in re.split(r'[,;]', qualifier_text) if t.strip()]
    return [c for c in candidates if any(match_qualifier_values(term, submodels[c[0]]) for term in terms)]


def related_part_types(typed: str, ref: Reference, conn, res: Resolution, timings: list) -> list:
    """Every PCdb part type whose name contains all the typed words and has fitment for the vehicle,
    e.g. 'Tailgate' -> Tailgate Liner, Tailgate Lock Actuator Motor, ... as (label, patch) options."""
    words = set(utils.default_process(typed).split())
    named = {i: name for i, name in ref.part_names.items() if words <= set(utils.default_process(name).split())}
    if not named:
        return []
    fitted = {row[0] for row in query(conn, '''SELECT DISTINCT parttermID FROM catalog.acesappsload
                                               WHERE basevehicleID IN %s AND parttermID IN %s''',
                                      (tuple(res.base_vehicle_ids), tuple(named)), timings, 'related part types with fitment')}
    return [(named[i], {'part_term_ids': [i]}) for i in sorted(fitted, key=lambda i: named[i])][:16]


def engine_label(liter, block_type, cylinders) -> str:
    try:
        liter = f'{float(liter):.1f}L'
    except (TypeError, ValueError):
        liter = f'{liter}L'
    return f'{liter} {block_type or ""}{cylinders or ""}'.strip()


def resolve_engine(parsed: dict, overrides: dict, conn, res: Resolution, timings: list) -> None:
    engines = query(conn, '''SELECT DISTINCT eb.EngineBaseID, eb.Liter, eb.BlockType, eb.Cylinders FROM vcdb.vehicle v
                             JOIN vcdb.vehicletoengineconfig ve ON ve.VehicleID = v.VehicleID
                             JOIN vcdb.engineconfig ec ON ec.EngineConfigID = ve.EngineConfigID
                             JOIN vcdb.enginebase eb ON eb.EngineBaseID = ec.EngineBaseID
                             WHERE v.BaseVehicleID IN %s ORDER BY eb.Liter''',
                    (tuple(res.base_vehicle_ids),), timings, 'VCdb engines for vehicle')
    labels = {engine_id: engine_label(liter, block, cyl) for engine_id, liter, block, cyl in engines}
    choices = [(label, {'engine_base_ids': [engine_id]}) for engine_id, label in labels.items()] + [('All engines', {'engine_base_ids': []})]
    if 'engine_base_ids' in overrides:
        ids = overrides['engine_base_ids']
        res.engine_base_ids = ids or None
        res.engine_label = ', '.join(labels.get(i, str(i)) for i in ids) if ids else 'All engines'
        return
    typed = (parsed.get('engine') or '').strip()
    liters = re.search(r'\d+(?:\.\d+)?', typed)
    if liters:
        wanted = float(liters.group())
        matches = [engine_id for engine_id, liter, _, _ in engines if _as_float(liter) is not None and abs(_as_float(liter) - wanted) < 0.05]
        if matches:
            res.engine_base_ids = matches
            res.engine_label = ', '.join(labels[i] for i in matches)
            return
        res.notes.append(f'No {wanted:g}L engine for the {res.vehicle_label}')
        res.questions.append(Question(f'The {res.vehicle_label} has no {wanted:g}L engine. Which one?', choices))
        return
    if len(engines) == 1:
        res.engine_base_ids, res.engine_label = [engines[0][0]], labels[engines[0][0]]
    elif engines:
        res.suggestions.append(Question('Narrow to an engine?', choices[:-1]))


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_part_type(parsed: dict, overrides: dict, ref: Reference, conn, res: Resolution, timings: list) -> None:
    if overrides.get('part_term_ids'):
        res.part_term_ids = overrides['part_term_ids']
        res.part_label = ', '.join(ref.part_names.get(i, str(i)) for i in res.part_term_ids)
        return
    typed = (parsed.get('part_type') or '').strip()
    if not typed:
        res.questions.append(Question('Which part? Add the part to the search (e.g. alternator, brake pads).'))
        return
    # Rank every PCdb name and alias in memory, then keep the closest ones that have fitment for this vehicle
    # (one lookup on the basevehicleID + parttermID index - listing every part type a popular vehicle has takes seconds)
    names = [(name, [part_id]) for part_id, name in ref.part_names.items()] + list(ref.aliases.items())
    ranked = sorted(((part_similarity(typed, name), name, ids) for name, ids in names), key=lambda s: s[0], reverse=True)[:40]
    candidate_ids = tuple({i for _, _, ids in ranked for i in ids})
    fitted = {row[0] for row in query(conn, '''SELECT DISTINCT parttermID FROM catalog.acesappsload
                                               WHERE basevehicleID IN %s AND parttermID IN %s''',
                                      (tuple(res.base_vehicle_ids), candidate_ids), timings, 'closest part types with fitment')}
    scored = [(score, name, [i for i in ids if i in fitted]) for score, name, ids in ranked if any(i in fitted for i in ids)]
    if not scored:
        res.questions.append(Question(f'No "{typed}" or similar part type has fitment for the {res.vehicle_label}.'))
        return
    exact = [ids for _, name, ids in scored if name.lower() == typed.lower()]
    if exact:
        res.part_term_ids = sorted({i for ids in exact for i in ids})
        # A broad name ('Bumper') can be an exact PCdb type with few parts while the parts meant are under a
        # related type ('Bumper Cover') - offer the related types with fitment up front
        related = [(label, patch) for label, patch in related_part_types(typed, ref, conn, res, timings)
                   if patch['part_term_ids'] != res.part_term_ids]
        if related:
            res.suggestions.append(Question(f'Other "{typed}" part types for this vehicle', related, expanded=True))
    else:
        top_score, top_name, top_ids = scored[0]
        options = [(name, {'part_term_ids': ids}) for _, name, ids in scored[:8]]
        # The typed name is a real PCdb part type, just not catalogued for this vehicle - say so instead of a vague question
        if any(name.lower() == typed.lower() for _, name, _ in ranked):
            res.questions.append(Question(f'No "{typed}" parts fit the {res.vehicle_label} in the catalog. '
                                          f'Related part types that do:', related_part_types(typed, ref, conn, res, timings) or options))
            return
        if top_score < 60:
            res.questions.append(Question(f'No "{typed}" for the {res.vehicle_label}. Closest part types:', options))
            return
        if top_score < 88 or (len(scored) > 1 and scored[1][0] >= top_score - 6 and scored[1][2] != top_ids):
            res.questions.append(Question(f'Which part type for "{typed}"?', options))
            return
        res.part_term_ids = top_ids
        res.notes.append(f'"{typed}" → {top_name}')
    res.part_label = ', '.join(ref.part_names[i] for i in res.part_term_ids)
