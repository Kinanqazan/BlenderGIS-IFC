"""Run with Blender in background to exercise the batch importer end to end."""
import importlib
import os
import sys
import uuid
from pathlib import Path

import addon_utils
import bpy

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
package = root.name
addon = addon_utils.enable(package, default_set=True)
assert addon is not None, 'Could not enable add-on'
mod = importlib.import_module(package + '.operators.io_import_shp')
shapefile = importlib.import_module(package + '.core.lib.shapefile')

def run():
    directory = str(root / "tests")
    paths = []
    fixture_id = uuid.uuid4().hex
    for index, (name, height, base) in enumerate((('Tree A', 4, 10), ('Tree B', 7, 20))):
        path = os.path.join(directory, '_batch_fixture_%s_%d.shp' % (fixture_id, index))
        with shapefile.Writer(path, shapeType=shapefile.POLYGON) as writer:
            writer.field('NAME', 'C')
            writer.field('HEIGHT', 'N', decimal=0)
            writer.field('BASE', 'N', decimal=0)
            if index == 0:
                writer.field('ONLY_ONE', 'C')
            writer.poly([[[index*10, 0], [index*10, 2], [index*10+2, 2], [index*10+2, 0], [index*10, 0]]])
            values = [name, height, base] + (['x'] if index == 0 else [])
            writer.record(*values)
        paths.append(path)

    assert mod._batch_common_fields(paths) == ['BASE', 'HEIGHT', 'NAME']
    assert mod._batch_common_fields(paths, numeric=True) == ['BASE', 'HEIGHT']
    mod._batch_files = paths[:]
    scene = addon.geoscene.GeoScene()
    scene.crs = 'EPSG:3857'
    scene.setOriginPrj(0, 0, synch=False)
    result = bpy.ops.importgis.shapefile_batch_props_dialog(
        'EXEC_DEFAULT', assignIFC=False, separateObjects=True,
        vertsElevSource='FIELD', fieldElevName='BASE', verticalGap=0.25,
        useFieldExtrude=True, fieldExtrudeName='HEIGHT', extrusionAxis='Z',
        useFieldName=True, fieldObjName='NAME', useRandomColor=True, fieldColorName='NAME')
    assert 'FINISHED' in result, result
    for name, base, height in [('Tree A', 10, 4), ('Tree B', 20, 7)]:
        obj = bpy.data.objects[name]
        z = [v.co.z + obj.location.z for v in obj.data.vertices]
        assert abs(min(z)-base) < 1e-5 and abs(max(z)-base-height) < 1e-5, (name, z)
        assert obj.data.materials, name
    # Exercise the projected IFC import branch while stubbing IFC project operations.
    for obj in list(bpy.data.objects):
        if obj.name in ('Tree A', 'Tree B'):
            bpy.data.objects.remove(obj, do_unlink=True)
    mod._batch_files = paths[:]
    mod._ifc_crs_plan = lambda operator, files, scene: ('EPSG:3857', {p: 'EPSG:3857' for p in files})
    mod._establish_ifc_origin = lambda geoscn, target, bbox, mode: (0, 0)
    assigned = []
    mod.assign_ifc_class_to_object = lambda obj, *args, **kwargs: assigned.append((obj.name, kwargs['sync_placement'], kwargs['pset_name'])) or True
    result = bpy.ops.importgis.shapefile_batch_props_dialog(
        'EXEC_DEFAULT', assignIFC=True, separateObjects=True,
        vertsElevSource='FIELD', fieldElevName='BASE',
        useVerticalGap=True, verticalGap=0.25,
        useFieldExtrude=True, fieldExtrudeName='HEIGHT', extrusionAxis='Z',
        useFieldName=True, fieldObjName='NAME', psetName='My_GIS_Data')
    assert 'FINISHED' in result, result
    assert assigned == [('Tree A', True, 'My_GIS_Data'), ('Tree B', True, 'My_GIS_Data')], assigned
    for name, base, height in [('Tree A', 10, 4), ('Tree B', 20.25, 7)]:
        obj = bpy.data.objects[name]
        z = [v.co.z + obj.location.z for v in obj.data.vertices]
        assert abs(min(z)-base) < 1e-5 and abs(max(z)-base-height) < 1e-5, (name, z)
    print('BATCH_OPTIONS_AND_GEOMETRY_OK')
    for path in paths:
        for ext in ('.shp', '.shx', '.dbf'):
            Path(path).with_suffix(ext).unlink(missing_ok=True)
run()
addon_utils.disable(package, default_set=True)
