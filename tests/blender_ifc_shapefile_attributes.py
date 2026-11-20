"""Run inside Blender with Bonsai installed to verify both shapefile naming modes."""
import importlib
import sys
from pathlib import Path

import addon_utils
import bpy

if 'bl_ext.blender_org.bonsai' not in bpy.context.preferences.addons:
    assert addon_utils.enable('bl_ext.blender_org.bonsai', default_set=True) is not None

import ifcopenshell
import bonsai.tool as tool
from bonsai.bim.module.pset.data import ObjectPsetsData


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
addon = addon_utils.enable(root.name, default_set=True)
assert addon is not None
mod = importlib.import_module(root.name + '.operators.io_import_shp')
path = next((root / 'test-data' / 'Examples GIS' / 'BÜ Schleißweiler I').rglob('BÜ Schleißweiler I Schalthäuschen.shp'))

scene = addon.geoscene.GeoScene()
scene.crs = 'EPSG:25832'
with mod.shpReader(str(path)) as reader:
    scene.setOriginPrj(*mod.BBOX(reader.bbox).center, synch=False)
assert bpy.ops.bim.create_project() == {'FINISHED'}

for batch in (False, True):
    for name_from_field, expected_name in ((False, path.stem), (True, '1')):
        options = {'fieldObjName': 'id'} if name_from_field else {}
        if batch:
            mod._batch_files = [str(path)]
            operator = bpy.ops.importgis.shapefile_batch_props_dialog
        else:
            options = {'filepath': str(path), **options}
            operator = bpy.ops.importgis.shapefile_props_dialog
        result = operator('EXEC_DEFAULT', assignIFC=True, georefIFC=False, separateObjects=True,
                          useFieldName=name_from_field, psetName='Custom_GIS', **options)
        assert result == {'FINISHED'}, result
        obj = bpy.context.view_layer.objects.active
        entity = tool.Ifc.get_entity(obj)
        assert entity.Name == expected_name, entity

        displayed = next(row for row in ObjectPsetsData.psetqtos(entity, psets_only=True) if row['Name'] == 'Custom_GIS')
        expected_fields = {'id': 1, 'Fläche ha': 0.001}
        assert {prop['Name']: prop['NominalValue'] for prop in displayed['Properties']} == expected_fields, displayed

        restored = ifcopenshell.file.from_string(tool.Ifc.get().to_string())
        pset = restored.by_id(displayed['id'])
        assert {prop.Name: prop.NominalValue.wrappedValue for prop in pset.HasProperties} == expected_fields

        # Free up the names for the next case. Blender normally suffixes duplicate names.
        obj.name = f'Imported test object {entity.id()}'

addon_utils.disable(root.name, default_set=True)
original = ObjectPsetsData.psetqtos(entity, psets_only=True)
assert all(prop['Name'] != 'id' for row in original for prop in row['Properties'])
print('SHAPEFILE_NAMES_AND_BONSAI_PROPERTIES_OK')
