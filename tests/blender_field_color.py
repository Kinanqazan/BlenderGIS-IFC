"""Run in Blender to verify field materials and serialized IFC styles."""
import importlib
import sys
from pathlib import Path
import bpy
import ifcopenshell
import ifcopenshell.api
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.context
import ifcopenshell.api.geometry
import ifcopenshell.api.style

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root.parent))
mod = importlib.import_module(root.name + '.operators.io_import_shp')
model = ifcopenshell.api.project.create_file(version='IFC4')
ifcopenshell.api.root.create_entity(model, ifc_class='IfcProject')
context = ifcopenshell.api.context.add_context(model, context_type='Model')
body_context = ifcopenshell.api.context.add_context(model, context_type='Model', context_identifier='Body', target_view='MODEL_VIEW', parent=context)

class Ifc:
    links = {}
    @classmethod
    def get_entity(cls, obj):
        return cls.links.get(obj.name)
    @classmethod
    def link(cls, entity, obj):
        cls.links[obj.name] = entity
    @staticmethod
    def run(operation, **kwargs):
        return ifcopenshell.api.run(operation, model, **kwargs)

class Style:
    @staticmethod
    def get_name(obj): return obj.name
    @staticmethod
    def can_support_rendering_style(obj): return False
    @staticmethod
    def get_surface_shading_attributes(obj):
        c = obj.diffuse_color
        return {'SurfaceColour': {'Name': None, 'Red': c[0], 'Green': c[1], 'Blue': c[2]}, 'Transparency': 0.0}

class Tool:
    pass
Tool.Ifc = Ifc
Tool.Style = Style

colors = {}
assert mod._get_color_for_field_value('A', colors) == mod._get_color_for_field_value('A', {})
assert mod._get_color_for_field_value('A', colors) != mod._get_color_for_field_value('B', colors)
materials = {}
for value in ('A', 'A', 'B'):
    mesh = bpy.data.meshes.new('mesh')
    obj = bpy.data.objects.new('feature', mesh)
    color = mod._get_color_for_field_value(value, colors)
    if value not in materials:
        materials[value] = mod.assign_blender_material(obj, color, 'TYPE', value)
    element = ifcopenshell.api.root.create_entity(model, ifc_class='IfcBuildingElementProxy')
    rep = ifcopenshell.api.geometry.add_wall_representation(model, context=body_context)
    ifcopenshell.api.geometry.assign_representation(model, product=element, representation=rep)
    mod._assign_ifc_surface_style(model, element, materials[value], Tool)

restored = ifcopenshell.file.from_string(model.to_string())
elements = restored.by_type('IfcBuildingElementProxy')
styles = [e.Representation.Representations[0].Items[0].StyledByItem[0].Styles[0] for e in elements]
assert styles[0] == styles[1] and styles[0] != styles[2]
assert len(restored.by_type('IfcSurfaceStyle')) == 2
assert all(style.Name.startswith('TYPE:') for style in styles)
assert tuple(materials['A'].diffuse_color) == tuple(materials['A'].node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value)
print('FIELD_COLOR_IFC_SERIALIZATION_OK')
