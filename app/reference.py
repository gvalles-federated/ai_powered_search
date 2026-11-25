"""Small VCdb / PCdb tables kept in memory for validating and fuzzy-matching what the model understood."""
from collections import defaultdict
from dataclasses import dataclass

from app.db import query

# SKUs the search may return: unblocked, from the PIES / ACES load (same rule as the catalog publishers' SKU mapping)
SKU_FILTER = "blocked = 0 AND record_origin IN ('pies', 'aces', 'duplicate_strip')"


@dataclass
class Reference:
    makes: dict            # MakeID -> MakeName
    models: dict           # ModelID -> ModelName
    vehicles_by_year: dict  # YearID -> [(BaseVehicleID, MakeName, ModelName)]
    part_names: dict       # PartTerminologyID -> PartTerminologyName
    aliases: dict          # AliasName -> [PartTerminologyID]
    brands: dict           # lowercase brand name -> brand name (AAIA and FAP brand names from skumaster)
    qualifier_labels: dict  # ACES qualifier type -> display name, e.g. DriveType -> Drive Type


def load_reference(conn, timings: list = None) -> Reference:
    makes = dict(query(conn, 'SELECT MakeID, MakeName FROM vcdb.make', timings=timings, label='VCdb makes'))
    models = dict(query(conn, 'SELECT ModelID, ModelName FROM vcdb.model', timings=timings, label='VCdb models'))
    vehicles_by_year = defaultdict(list)
    for base_vehicle_id, year, make_id, model_id in query(conn, 'SELECT BaseVehicleID, YearID, MakeID, ModelID FROM vcdb.basevehicle',
                                                          timings=timings, label='VCdb base vehicles'):
        vehicles_by_year[year].append((base_vehicle_id, makes.get(make_id, ''), models.get(model_id, '')))
    part_names = dict(query(conn, 'SELECT PartTerminologyID, PartTerminologyName FROM pcdb.parts', timings=timings, label='PCdb part types'))
    aliases = defaultdict(list)
    for alias_name, part_id in query(conn, '''SELECT a.AliasName, pa.PartTerminologyID FROM pcdb.partstoalias pa
                                              JOIN pcdb.alias a ON a.AliasID = pa.AliasID''', timings=timings, label='PCdb aliases'):
        aliases[alias_name].append(part_id)
    brands = {}
    for aaia_name, fap_name in query(conn, f'''SELECT DISTINCT supbrandaaianame, fapbrandname FROM catalog.skumaster
                                              WHERE {SKU_FILTER}''', timings=timings, label='brand names'):
        for name in (aaia_name, fap_name):
            if name and name.strip():
                brands.setdefault(name.strip().lower(), name.strip())
    qualifier_labels = {name: display or name for name, display in query(conn, 'SELECT attributename, attrdisplay FROM catalog.acesattrindex',
                                                                         timings=timings, label='qualifier names')}
    qualifier_labels['SubModel'] = 'Submodel'  # the index calls it "Vehicle Model"
    return Reference(makes, models, dict(vehicles_by_year), part_names, dict(aliases), brands, qualifier_labels)
