from pathlib import Path
import ifcopenshell
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.unit
import ifcopenshell.api.context
import ifcopenshell.api.georeference
import ifcopenshell.util.geolocation
import importlib.util
spec = importlib.util.spec_from_file_location('ifc_georeference', Path(__file__).parents[1] / 'core' / 'ifc_georeference.py')
georef = importlib.util.module_from_spec(spec)
spec.loader.exec_module(georef)

for unit_name, prefix, scale in [('m', None, 1.0), ('mm', 'MILLI', 0.001)]:
    model = ifcopenshell.api.project.create_file(version='IFC4')
    ifcopenshell.api.root.create_entity(model, ifc_class='IfcProject', name='GIS test')
    unit = ifcopenshell.api.unit.add_si_unit(model, unit_type='LENGTHUNIT', prefix=prefix)
    ifcopenshell.api.unit.assign_unit(model, units=[unit])
    ifcopenshell.api.context.add_context(model, context_type='Model')
    georef.create_map_conversion(model, 3537000, 5429000,
                                 'DB_REF / 3-degree Gauss-Kruger zone 3 (E-N)', scale, 'GNTRANS2016')
    point = ifcopenshell.util.geolocation.auto_xyz2enh(model, 50 / scale, 20 / scale, 0)
    assert abs(point[0] - 3537050) < 0.000001 and abs(point[1] - 5429020) < 0.000001, point
    print('ROUNDTRIP', unit_name, point)
    reopened = ifcopenshell.file.from_string(model.to_string())
    restored = ifcopenshell.util.geolocation.auto_xyz2enh(reopened, 50 / scale, 20 / scale, 0)
    assert abs(restored[0] - 3537050) < 0.000001 and abs(restored[1] - 5429020) < 0.000001, restored
    assert reopened.by_type('IfcProjectedCRS')[0].VerticalDatum == 'GNTRANS2016'
    print('IFC_SERIALIZATION_OK', unit_name)
    try:
        georef.create_map_conversion(model, 0, 0, 'different CRS', scale)
    except georef.GeoreferenceError:
        print('EXISTING_GEOREFERENCE_PRESERVED')
    else:
        raise AssertionError('Existing georeferencing must not be overwritten')
