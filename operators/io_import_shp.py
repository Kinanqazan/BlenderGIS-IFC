# -*- coding:utf-8 -*-
import os, sys, time
import json
import re
import random
import hashlib
import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy.types import Operator
import bmesh
import math
from mathutils import Vector

import logging
log = logging.getLogger(__name__)

from ..core.lib.shapefile import Reader as shpReader

from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS
from ..core import BBOX
from ..core import bonsai_pset_display
from ..core.proj import Reproj
from ..core.proj.shapefile_crs import CRSInspectionError, inspect_shapefile_set, transform_coordinates
from ..core.ifc_georeference import (GeoreferenceError, create_map_conversion, existing_map_origin,
	set_direct_site_placement, validate_new_direct_placement, validate_new_map_conversion)
from ..core.utils import perf_clock

from .utils import adjust3Dview, getBBOX, DropToGround

def _get_bonsai_tool():
	"""Use Bonsai only after it has been initialized by Blender.

	Importing bonsai.tool directly before Bonsai starts can cause a circular
	import. Checking at use time also supports enabling Bonsai after this add-on.
	"""
	tool = sys.modules.get("bonsai.tool")
	return tool if tool is not None and hasattr(tool, "Ifc") else None


def _prepare_ifc_geometry(context, model):
	"""Check Bonsai's kernel before it can partially modify the IFC model."""
	props = getattr(context.scene, "BIMProjectProperties", None)
	if props is None or not hasattr(props, "geometry_library"):
		return False
	import ifcopenshell.geom
	def check(library):
		return ifcopenshell.geom.iterator(
			ifcopenshell.geom.settings(), model, 1, geometry_library=library)
	library = props.geometry_library
	try:
		check(library)
		return False
	except RuntimeError as exc:
		if library == 'opencascade' or 'Invalid hybrid kernel' not in str(exc):
			raise
		log.warning("Bonsai's %s kernel is unavailable; checking OpenCASCADE", library)
		check('opencascade')
		props.geometry_library = 'opencascade'
		return True


def _prepare_pset_properties(properties):
	"""Convert shapefile field values to IFC-compatible property dict."""
	pset_props = {}
	for key, value in properties.items():
		if isinstance(value, bytes):
			value = value.decode('utf-8', errors='replace').strip()
		elif value is not None and not isinstance(value, (str, int, float)):
			value = str(value)
		pset_props[key] = value
	return pset_props


def _create_pset_for_element(model, element, properties, pset_name="Pset_GIS_Attributes"):
	"""Create a property set containing shapefile attributes on the IFC element."""
	if not properties:
		return
	from ifcopenshell.api.pset import add_pset, edit_pset

	pset_name = pset_name.strip() or "Pset_GIS_Attributes"
	try:
		pset_props = _prepare_pset_properties(properties)
		pset = add_pset(model, product=element, name=pset_name)
		edit_pset(model, pset=pset, properties=pset_props, should_purge=False)
		log.info(f"Created Pset '{pset_name}' with {len(pset_props)} properties on element {element.id()}")
	except Exception as e:
		log.error(f"Failed to create IFC Pset for element {element.id()}: {e}", exc_info=True)
		raise


def _generate_random_color(rng):
	"""Generate a random color with good visibility."""
	# Use saturated, bright colors for visibility.
	hue = rng.random()
	saturation = 0.6 + rng.random() * 0.4  # 0.6-1.0 for vibrant colors
	value = 0.7 + rng.random() * 0.3  # 0.7-1.0 for bright colors

	# Convert HSV to RGB
	import colorsys
	rgb = colorsys.hsv_to_rgb(hue, saturation, value)
	return (rgb[0], rgb[1], rgb[2], 1.0)  # RGBA


def _get_color_for_field_value(value, color_map):
	"""Get or generate a color for a specific field value."""
	if value in color_map:
		return color_map[value]
	else:
		seed = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest(), "big")
		color = _generate_random_color(random.Random(seed))
		color_map[value] = color
		return color


def _assign_ifc_surface_style(model, element, material, tool):
	"""Link a Bonsai style to the material and the element's body geometry."""
	import bonsai.core.style
	import ifcopenshell.api.style

	style = tool.Ifc.get_entity(material)
	if style is None:
		style = bonsai.core.style.add_style(tool.Ifc, tool.Style, material)
	representation = getattr(element, "Representation", None)
	bodies = [rep for rep in (representation.Representations if representation else ())
		if rep.RepresentationIdentifier == "Body"]
	if not bodies:
		raise RuntimeError("IFC element has no Body representation")
	for body in bodies:
		ifcopenshell.api.style.assign_representation_styles(model, shape_representation=body, styles=[style])


def assign_blender_material(obj, color, field, value):
	"""Create a material for a field value and return it for IFC styling."""
	mat = bpy.data.materials.new(name=f"{field}: {value}")
	mat.diffuse_color = color
	mat.use_nodes = True
	bsdf = mat.node_tree.nodes.get("Principled BSDF")
	bsdf.inputs["Base Color"].default_value = color
	bsdf.inputs["Alpha"].default_value = color[3]
	if obj.data.materials:
		obj.data.materials[0] = mat
	else:
		obj.data.materials.append(mat)
	return mat


def assign_ifc_class_to_object(obj, ifc_class, predefined_type="", user_defined_type="", properties=None, sync_placement=False, pset_name="Pset_GIS_Attributes"):
	"""Assign IFC class to a Blender object using Bonsai API if available, otherwise use custom properties.
	If properties dict is provided and Bonsai is active, creates an IFC Pset with the shapefile field values."""
	properties = properties or {}
	tool = _get_bonsai_tool()
	if tool is not None:
		# Save object name before any operations (Bonsai may rename the object)
		obj_name = obj.name
		try:
			# Check if there's an active IFC project
			model = tool.Ifc.get()
			if model is None:
				log.warning("No active IFC project in Bonsai, falling back to custom properties")
				obj["IfcClass"] = ifc_class
				if predefined_type:
					obj["IfcPredefinedType"] = predefined_type
				if user_defined_type:
					obj["IfcUserDefinedType"] = user_defined_type
				return True

			# Record existing IFC elements before assignment so we can identify
			# the newly created element afterwards
			existing_elements = set(model)

			# Evaluate the new object's placement before Bonsai reads its world matrix.
			bpy.context.view_layer.update()
			placement = obj.matrix_world.copy() if sync_placement else None
			# Use Bonsai's built-in assign_class operator to properly handle
			# object naming, navigation panel updates, and IFC geometry representation
			bpy.ops.bim.assign_class(
				obj=obj_name,
				ifc_class=ifc_class,
				predefined_type=predefined_type or "",
				userdefined_type=user_defined_type or ""
			)

			# Find the newly created IFC element by looking for elements
			# that were not in the model before assign_class was called
			new_element = None
			for element in model:
				if element not in existing_elements and element.is_a() == ifc_class:
					new_element = element
					break

			if new_element is None:
				# Fallback: search by linked object name if element identity check failed
				for element in model:
					if element.is_a() == ifc_class:
						linked_obj = tool.Ifc.get_object(element)
						if linked_obj and linked_obj.name == obj_name:
							new_element = element
							break

			if new_element is not None:
				if sync_placement:
					linked_obj = tool.Ifc.get_object(new_element)
					if linked_obj is None or not getattr(new_element, 'Representation', None):
						raise RuntimeError('Bonsai did not create IFC geometry for ' + obj_name)
					# Bonsai's tessellation assignment can leave the IFC placement at the origin.
					linked_obj.matrix_world = placement
					bpy.context.view_layer.update()
					if 'FINISHED' not in bpy.ops.bim.edit_object_placement(obj=linked_obj.name):
						raise RuntimeError('Bonsai did not write IFC placement for ' + obj_name)
				_create_pset_for_element(model, new_element, properties, pset_name)
			else:
				if sync_placement:
					raise RuntimeError('Bonsai did not create an IFC element for ' + obj_name)
				log.warning(f"Could not identify new IFC element for '{obj_name}' to create Pset")

			log.info(f"Assigned IFC class '{ifc_class}' to object '{obj_name}' using Bonsai API")
			return True
		except Exception as e:
			log.error(f"Failed to assign IFC class using Bonsai API: {e}", exc_info=True)
			# Try to find the object by original name in case it was renamed
			try:
				if obj_name in bpy.data.objects:
					obj = bpy.data.objects[obj_name]
					obj["IfcClass"] = ifc_class
					if predefined_type:
						obj["IfcPredefinedType"] = predefined_type
					if user_defined_type:
						obj["IfcUserDefinedType"] = user_defined_type
			except Exception as fallback_e:
				log.error(f"Could not set fallback IFC properties: {fallback_e}")
			return False
	else:
		# Bonsai not available, use custom properties
		obj["IfcClass"] = ifc_class
		if predefined_type:
			obj["IfcPredefinedType"] = predefined_type
		if user_defined_type:
			obj["IfcUserDefinedType"] = user_defined_type
		log.info(f"Assigned IFC class '{ifc_class}' to object '{obj.name}' using custom properties")
		return True

PKG, SUBPKG = __package__.split('.', maxsplit=1)

# Temporary storage for batch import file paths
_batch_files = []


def _crs_preview(filepaths, inspection=None):
	groups, errors = inspection if inspection is not None else inspect_shapefile_set(filepaths)
	lines = []
	for group in groups:
		name = group['crs'].get('name', 'Unknown CRS')
		candidates = group['candidates']
		match = (' / '.join('%s (%d%%)' % item for item in candidates[:2])
			if candidates else 'no EPSG match')
		lines.append('%s: %s' % (name, match))
		lines.append('%d file(s)' % len(group['files']))
	for path, error in errors:
		log.warning('Cannot inspect CRS for %s: %s', path, error)
	if errors:
		lines.append('%d file(s): CRS unavailable; see logs' % len(errors))
	return json.dumps(lines)


def _draw_crs_preview(layout, preview):
	if not preview:
		return
	box = layout.box()
	box.label(text='Source CRS information (review before import):')
	for line in json.loads(preview):
		box.label(text=line)


_DIRECT_PLACEMENT_KEY = 'bgis_direct_site_placement'
_PLACEMENT_MODES = (
	('MAP', 'IFC map conversion', 'Store the CRS and projected offset in the IFC map conversion'),
	('DIRECT', 'Direct Site placement', 'Write projected coordinates into Site placement for viewers that display local IFC coordinates'),
)


def _direct_project_state(scene, model):
	state = json.loads(scene.get(_DIRECT_PLACEMENT_KEY, '[]'))
	projects = model.by_type('IfcProject')
	return state if state and len(projects) == 1 and state[0] == projects[0].GlobalId else None


def _init_crs_dialog(operator, files, scene):
	groups, errors = inspect_shapefile_set(files)
	operator.suggestedCRS = ''
	operator.projectCRS = ''
	operator.crsPreview = _crs_preview(files, (groups, errors))
	operator.crsMatches = json.dumps([group['candidates'][0] for group in groups
		if group['candidates']])
	tool = _get_bonsai_tool()
	model = tool.Ifc.get() if tool else None
	if model is not None and model.schema.upper() == 'IFC4':
		state = _direct_project_state(scene, model)
		if state:
			operator.placementMode = 'DIRECT'
			operator.projectCRS = state[1]
			operator.targetCRS = state[1]
			return
		operations = model.by_type('IfcCoordinateOperation')
		if len(operations) == 1:
			operator.placementMode = 'MAP'
			operator.projectCRS = operations[0].TargetCRS.Name or ''
			operator.targetCRS = operator.projectCRS
			return
	if groups and not errors and all(len(group['candidates']) == 1
		and group['crs']['type'] in ('ProjectedCRS', 'CompoundCRS') for group in groups):
		candidates = {group['candidates'][0][0] for group in groups}
		if len(candidates) == 1:
			operator.suggestedCRS = candidates.pop()
			operator.targetCRS = operator.suggestedCRS


def _draw_ifc_crs(operator):
	box = operator.layout.box()
	if operator.projectCRS:
		label = 'Direct Site placement' if operator.placementMode == 'DIRECT' else 'IFC map conversion'
		box.label(text='Project placement: ' + label)
	else:
		box.prop(operator, 'placementMode')
	if operator.placementMode == 'DIRECT':
		box.label(text='Writes Site coordinates without an IFC map conversion.', icon='INFO')
	matches = json.loads(operator.crsMatches or '[]')
	uncertain = any(confidence < 100 for _, confidence in matches)
	if operator.projectCRS:
		box.label(text='Existing IFC CRS: ' + operator.projectCRS)
	elif operator.suggestedCRS:
		label = 'Suggested IFC target: ' if uncertain else 'Detected IFC target: '
		box.label(text=label + operator.suggestedCRS)
		box.prop(operator, 'overrideTarget')
		if operator.overrideTarget:
			box.prop(operator, 'targetCRS')
	else:
		box.label(text='Choose a projected CRS for this IFC project.')
		box.prop(operator, 'targetCRS')
	target = _selected_ifc_crs(operator)
	reproject = bool(target) and any(source != target for source, _ in matches)
	if uncertain:
		box.label(text='Check the suggested source CRS in QGIS before confirming.', icon='INFO')
	if reproject:
		box.label(text='Layers will be converted to the IFC target CRS.', icon='INFO')
	if uncertain and reproject:
		box.prop(operator, 'confirmCRS', text='Confirm source CRS and coordinate conversion')
	elif uncertain:
		box.prop(operator, 'confirmCRS')
	elif reproject:
		box.prop(operator, 'confirmCRS', text='Confirm coordinate conversion')


def _selected_ifc_crs(operator):
	if operator.projectCRS:
		return operator.projectCRS.strip().upper()
	if operator.suggestedCRS and not operator.overrideTarget:
		return operator.suggestedCRS
	return operator.targetCRS.strip().upper()


def _ifc_crs_plan(operator, files, scene):
	from ifcopenshell.util.unit import calculate_unit_scale
	target = _selected_ifc_crs(operator)
	if not re.fullmatch(r'EPSG:[0-9]+', target):
		raise CRSInspectionError('Enter a projected target EPSG code, for example EPSG:25832')
	tool = _get_bonsai_tool()
	model = tool.Ifc.get() if tool else None
	if model is None:
		raise GeoreferenceError('Open or create an IFC4 project in Bonsai first')
	scale = calculate_unit_scale(model)
	_ifc_project_origin(scene, tool, model, target, scale, operator.placementMode)
	groups, errors = inspect_shapefile_set(files)
	if errors or not groups:
		raise CRSInspectionError(errors[0][1] if errors else 'No shapefile CRS was found')
	sources = {}
	for group in groups:
		matches = group['candidates']
		if not matches or (len(matches) > 1 and matches[0][1] <= matches[1][1]):
			raise CRSInspectionError('Ambiguous source CRS. In QGIS, verify and re-export: ' + os.path.basename(group['files'][0]))
		source, confidence = matches[0]
		if (confidence < 100 or source != target) and not operator.confirmCRS:
			reason = ('Verify the suggested source CRS in QGIS' if confidence < 100
				else 'Confirm conversion to the IFC target CRS')
			raise CRSInspectionError(reason + ' before importing ' + os.path.basename(group['files'][0]))
		operation = transform_coordinates(source, target)
		log.info('%s to %s: %s (accuracy %s m)', source, target, operation['description'], operation['accuracy_metres'])
		for path in group['files']:
			with shpReader(path) as shp:
				if shp.shapeType not in (1, 3, 5):
					raise CRSInspectionError('Georeferenced import currently supports 2D points, lines and polygons: ' + os.path.basename(path))
			sources[path] = source
	return target, sources


class IMPORTGIS_OT_ifc_map_conversion(Operator):
	bl_idname = 'importgis.ifc_map_conversion'
	bl_label = 'Set IFC Project Placement'
	bl_options = {'INTERNAL', 'UNDO'}
	parameters: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	placementMode: EnumProperty(items=_PLACEMENT_MODES, default='MAP', options={'HIDDEN', 'SKIP_SAVE'})
	transaction_key = ''

	def execute(self, context):
		from bonsai.bim.ifc import IfcStore
		return IfcStore.execute_ifc_operator(self, context)

	def _execute(self, context):
		tool = _get_bonsai_tool()
		model = tool.Ifc.get()
		parameters = json.loads(self.parameters)
		if self.placementMode == 'MAP':
			create_map_conversion(model, *parameters)
		else:
			eastings, northings, target, scale = parameters
			set_direct_site_placement(model, *parameters)
			props = tool.Georeference.get_georeference_props()
			# Keep the Blender scene close to zero while Bonsai writes projected placements.
			props.blender_offset_x = str(eastings / scale)
			props.blender_offset_y = str(northings / scale)
			props.blender_offset_z = '0'
			props.blender_x_axis_abscissa = '1'
			props.blender_x_axis_ordinate = '0'
			props.has_blender_offset = True
			context.scene[_DIRECT_PLACEMENT_KEY] = json.dumps([
				model.by_type('IfcProject')[0].GlobalId, target, eastings, northings])
			for element in model.by_type('IfcSpatialStructureElement'):
				obj = tool.Ifc.get_object(element)
				if obj is not None:
					tool.Geometry.get_blender_offset_type(obj)
		return {'FINISHED'}


def _ifc_project_origin(scene, tool, model, target, scale, mode):
	if model.schema.upper() != 'IFC4':
		raise GeoreferenceError('Shapefile georeferencing requires an IFC4 project')
	props = tool.Georeference.get_georeference_props()
	state = _direct_project_state(scene, model)
	if state:
		if mode != 'DIRECT' or target != state[1]:
			raise GeoreferenceError('This IFC already uses Direct Site placement. Use a fresh project to change placement mode or CRS')
		from ifcopenshell.util.placement import get_local_placement
		sites = model.by_type('IfcSite')
		if (not props.has_blender_offset or model.by_type('IfcCoordinateOperation') or len(sites) != 1
			or not sites[0].ObjectPlacement or not sites[0].ObjectPlacement.is_a('IfcLocalPlacement')):
			raise GeoreferenceError('The IFC direct placement or its Blender offset has changed')
		placement = get_local_placement(sites[0].ObjectPlacement)
		origin = state[2], state[3]
		if (any(abs(actual * scale - expected) > 1e-6
				for actual, expected in zip(placement[:3, 3], (*origin, 0)))
			or any(abs(float(actual) * scale - expected) > 1e-6 for actual, expected in zip(
				(props.blender_offset_x, props.blender_offset_y, props.blender_offset_z), (*origin, 0)))
			or float(props.blender_x_axis_abscissa) != 1 or float(props.blender_x_axis_ordinate) != 0):
			raise GeoreferenceError('The IFC Site or Blender offset no longer matches the original direct placement')
		return origin
	if props.has_blender_offset:
		raise GeoreferenceError('This IFC has an existing Bonsai Blender offset. Start with a fresh IFC4 project')
	if mode == 'DIRECT':
		validate_new_direct_placement(model, 0, 0, target, scale)
		return None
	origin = existing_map_origin(model, target, scale)
	if origin is None:
		validate_new_map_conversion(model, 0, 0, target, scale)
	return origin


def _establish_ifc_origin(geoscn, target, bbox, mode):
	from ifcopenshell.util.unit import calculate_unit_scale
	tool = _get_bonsai_tool()
	model = tool.Ifc.get() if tool else None
	if model is None:
		raise GeoreferenceError('Open an IFC4 project in Bonsai first')
	if geoscn.scale != 1:
		raise GeoreferenceError('Use a scene without BlenderGIS map scaling')
	scale = calculate_unit_scale(model)
	origin = _ifc_project_origin(geoscn.scn, tool, model, target, scale, mode)
	if geoscn.hasOriginPrj:
		if geoscn.crs != target:
			raise GeoreferenceError('Scene already uses another CRS. Start in a fresh IFC4 scene')
		scene_origin = geoscn.getOriginPrj()
		if origin and any(abs(a - b) > 1e-6 for a, b in zip(origin, scene_origin)):
			raise GeoreferenceError('The scene origin differs from the IFC origin. Use a fresh scene')
	else:
		scene_origin = bbox.center
	if origin is None:
		origin = scene_origin
		result = bpy.ops.importgis.ifc_map_conversion(parameters=json.dumps([*origin, target, scale]), placementMode=mode)
		if 'FINISHED' not in result:
			raise GeoreferenceError('Could not set the IFC project placement')
	if geoscn.crs != target:
		geoscn.crs = target
	if not geoscn.hasOriginPrj:
		geoscn.setOriginPrj(*origin, synch=False)
	return origin

featureType={
0:'Null',
1:'Point',
3:'PolyLine',
5:'Polygon',
8:'MultiPoint',
11:'PointZ',
13:'PolyLineZ',
15:'PolygonZ',
18:'MultiPointZ',
21:'PointM',
23:'PolyLineM',
25:'PolygonM',
28:'MultiPointM',
31:'MultiPatch'
}


"""
dbf fields type:
	C is ASCII characters
	N is a double precision integer limited to around 18 characters in length
	D is for dates in the YYYYMMDD format, with no spaces or hyphens between the sections
	F is for floating point numbers with the same length limits as N
	L is for logical data which is stored in the shapefile's attribute table as a short integer as a 1 (true) or a 0 (false).
	The values it can receive are 1, 0, y, n, Y, N, T, F or the python builtins True and False
"""


class IMPORTGIS_OT_shapefile_file_dialog(Operator):
	"""Select shp file, loads the fields and start importgis.shapefile_props_dialog operator"""

	bl_idname = "importgis.shapefile_file_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {'INTERNAL'}

	# Import dialog properties
	filepath: StringProperty(
		name="File Path",
		description="Filepath used for importing the file",
		maxlen=1024,
		subtype='FILE_PATH' )

	filename_ext = ".shp"

	filter_glob: StringProperty(
			default = "*.shp",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Options will be available")
		layout.label(text="after selecting a file")

	def execute(self, context):
		if os.path.exists(self.filepath):
			bpy.ops.importgis.shapefile_props_dialog('INVOKE_DEFAULT', filepath=self.filepath)
		else:
			self.report({'ERROR'}, "Invalid filepath")
		return{'FINISHED'}



class IMPORTGIS_OT_shapefile_props_dialog(Operator):
	"""Shapefile importer properties dialog"""

	bl_idname = "importgis.shapefile_props_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"INTERNAL"}

	filepath: StringProperty()
	crsPreview: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	crsMatches: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	projectCRS: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	suggestedCRS: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	georefIFC: BoolProperty(default=True, options={'HIDDEN'})
	placementMode: EnumProperty(name='IFC placement', items=_PLACEMENT_MODES, default='MAP', options={'SKIP_SAVE'})
	overrideTarget: BoolProperty(name='Choose a different target CRS', options={'SKIP_SAVE'})
	targetCRS: StringProperty(name='IFC target CRS', options={'SKIP_SAVE'})
	confirmCRS: BoolProperty(name='I verified the source CRS in QGIS', options={'SKIP_SAVE'})

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listFields(self, context):
		fieldsItems = []
		try:
			shp = shpReader(self.filepath)
		except Exception as e:
			log.warning("Unable to read shapefile fields", exc_info=True)
			return fieldsItems
		fields = [field for field in shp.fields if field[0] != 'DeletionFlag'] #ignore default DeletionFlag field
		for i, field in enumerate(fields):
			#put each item in a tuple (key, label, tooltip)
			fieldsItems.append( (field[0], field[0], '') )
		return fieldsItems

	# Shapefile CRS definition
	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				#put each object in a tuple (key, label, tooltip) and add this to the objects list
				objs.append((object.name, object.name, "Object named " + object.name))
		return objs

	reprojection: BoolProperty(
			name="Specifiy shapefile CRS",
			description="Specifiy shapefile CRS if it's different from scene CRS",
			default=False )

	shpCRS: EnumProperty(
		name = "Shapefile CRS",
		description = "Choose a Coordinate Reference System",
		items = listPredefCRS)

	# Elevation source
	vertsElevSource: EnumProperty(
			name="Elevation source",
			description="Select the source of vertices z value",
			items=[
			('NONE', 'None', "Flat geometry"),
			('GEOM', 'Geometry', "Use z value from shape geometry if exists"),
			('FIELD', 'Field', "Extract z elevation value from an attribute field"),
			('OBJ', 'Object', "Get z elevation value from an existing ground mesh"),
			('MANUAL', 'Manual', "Enter one base elevation for all imported features"),
			],
			default='GEOM')
	manualElevation: FloatProperty(name='Base elevation', unit='LENGTH',
		description='Set the same base Z for all imported features, before any extrusion')

	# Elevation object
	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	# Elevation field
	'''
	useFieldElev: BoolProperty(
			name="Elevation from field",
			description="Extract z elevation value from an attribute field",
			default=False )
	'''
	fieldElevName: EnumProperty(
		name = "Elev. field",
		description = "Choose field",
		items = listFields )

	#Extrusion field
	useFieldExtrude: BoolProperty(
			name="Extrusion from field",
			description="Extract z extrusion value from an attribute field",
			default=False )

	fieldExtrudeName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")] )

	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Warning : can be very slow with lot of features",
			default=False )

	#Name objects from field
	useFieldName: BoolProperty(
			name="Object name from field",
			description="Extract name for created objects from an attribute field",
			default=False )
	fieldObjName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )

	# IFC Assignment
	assignIFC: BoolProperty(
			name="Create IFC elements",
			description="Assign IFC class to imported objects for Bonsai/BlenderBIM",
			default=True )
	
	ifcClass: StringProperty(
			name="IFC Class",
			description="IFC class name (e.g., IfcBuilding, IfcSite, IfcBuildingElementProxy)",
			default="IfcBuildingElementProxy" )
	
	ifcPredefinedType: StringProperty(
			name="IFC Predefined Type",
			description="IFC predefined type (optional)",
			default="" )
	
	ifcUserDefinedType: StringProperty(
			name="IFC User Defined Type",
			description="IFC user defined type (optional)",
			default="" )

	psetName: StringProperty(
			name="Property set name",
			description="IFC property set for shapefile attributes (blank uses the default)",
			default="Pset_GIS_Attributes" )

	# Random coloring
	useRandomColor: BoolProperty(
			name="Random color by field",
			description="Assign random colors (IfcSurfaceStyles) to objects based on unique field values",
			default=False )

	fieldColorName: EnumProperty(
		name = "Color field",
		description = "Choose field to base random coloring on",
		items = listFields )


	def draw(self, context):
		#Function used by blender to draw the panel.
		scn = context.scene
		layout = self.layout

		#
		layout.prop(self, 'vertsElevSource')
		#
		#layout.prop(self, 'useFieldElev')
		if self.vertsElevSource == 'FIELD':
			layout.prop(self, 'fieldElevName')
		elif self.vertsElevSource == 'OBJ':
			layout.prop(self, 'objElevLst')
		elif self.vertsElevSource == 'MANUAL':
			layout.prop(self, 'manualElevation')
		#
		layout.prop(self, 'useFieldExtrude')
		if self.useFieldExtrude:
			layout.prop(self, 'fieldExtrudeName')
			layout.prop(self, 'extrusionAxis')
		#
		layout.prop(self, 'separateObjects')
		if self.separateObjects:
			layout.prop(self, 'useFieldName')
		else:
			self.useFieldName = False
		if self.separateObjects and self.useFieldName:
			layout.prop(self, 'fieldObjName')
		#
		layout.separator()
		layout.prop(self, 'assignIFC')
		if self.assignIFC:
			layout.prop(self, 'ifcClass')
			layout.prop(self, 'ifcPredefinedType')
			layout.prop(self, 'ifcUserDefinedType')
			if self.separateObjects:
				layout.prop(self, 'psetName')
		#
		if self.separateObjects:
			layout.prop(self, 'useRandomColor')
			if self.useRandomColor:
				layout.prop(self, 'fieldColorName')
		#
		geoscn = GeoScene()
		_draw_crs_preview(layout, self.crsPreview)
		_draw_ifc_crs(self)
		if self.assignIFC and self.georefIFC:
			return
		#geoscnPrefs = context.preferences.addons['geoscene'].preferences
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.shpCRSInputLayout(context)
			#
		else:
			self.shpCRSInputLayout(context)
		georefManagerLayout(self, context)


	def shpCRSInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		#row.prop(self, "shpCRS", text='CRS')
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "shpCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')


	def invoke(self, context, event):
		_init_crs_dialog(self, [self.filepath], context.scene)
		return context.window_manager.invoke_props_dialog(self, width=540)

	def execute(self, context):

		#elevField = self.fieldElevName if self.useFieldElev else ""
		elevField = self.fieldElevName if self.vertsElevSource == 'FIELD' else ""
		extrudField = self.fieldExtrudeName if self.useFieldExtrude else ""
		nameField = self.fieldObjName if self.useFieldName else ""
		if self.vertsElevSource == 'OBJ':
			if not self.objElevLst:
				self.report({'ERROR'}, "No elevation object")
				return {'CANCELLED'}
			else:
				objElevName = self.objElevLst
		else:
			objElevName = '' #will not be used

		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if geoscn.hasCRS:
			if self.reprojection:
				shpCRS = self.shpCRS
			else:
				shpCRS = geoscn.crs
		else:
			shpCRS = self.shpCRS
		target = ''
		if self.assignIFC and self.georefIFC:
			try:
				target, sources = _ifc_crs_plan(self, [self.filepath], context.scene)
				shpCRS = sources[self.filepath]
			except (ValueError, OSError) as exc:
				self.report({'ERROR'}, str(exc))
				return {'CANCELLED'}

		# Prepare IFC parameters
		ifcClass = self.ifcClass if self.assignIFC else ""
		ifcPredefinedType = self.ifcPredefinedType if self.assignIFC else ""
		ifcUserDefinedType = self.ifcUserDefinedType if self.assignIFC else ""

		# Prepare coloring parameters
		colorField = self.fieldColorName if self.separateObjects and self.useRandomColor else ""

		log.info(f"IFC Assignment: assignIFC={self.assignIFC}, ifcClass={ifcClass}")
		log.info(f"Random coloring: useRandomColor={self.useRandomColor}, colorField={colorField}")

		try:
			result = bpy.ops.importgis.shapefile('INVOKE_DEFAULT', filepath=self.filepath, shpCRS=shpCRS,
				targetCRS=target, placementMode=self.placementMode, elevSource=self.vertsElevSource,
				manualElevation=self.manualElevation,
				fieldElevName=elevField, objElevName=objElevName, fieldExtrudeName=extrudField, fieldObjName=nameField,
				extrusionAxis=self.extrusionAxis, separateObjects=self.separateObjects,
				ifcClass=ifcClass, ifcPredefinedType=ifcPredefinedType, ifcUserDefinedType=ifcUserDefinedType, psetName=self.psetName,
				useRandomColor=self.separateObjects and self.useRandomColor, fieldColorName=colorField)
		except Exception as e:
			log.error('Shapefile import fails', exc_info=True)
			self.report({'ERROR'}, 'Shapefile import fails, check logs.')
			return {'CANCELLED'}
		if 'CANCELLED' in result:
			return {'CANCELLED'}

		return{'FINISHED'}


class IMPORTGIS_OT_shapefile(Operator):
	"""Import from ESRI shapefile file format (.shp)"""

	bl_idname = "importgis.shapefile" # important since its how bpy.ops.import.shapefile is constructed (allows calling operator from python console or another script)
	#bl_idname rules: must contain one '.' (dot) charactere, no capital letters, no reserved words (like 'import')
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"UNDO"}

	filepath: StringProperty()

	shpCRS: StringProperty(name = "Shapefile CRS", description = "Coordinate Reference System")
	targetCRS: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	placementMode: EnumProperty(items=_PLACEMENT_MODES, default='MAP', options={'HIDDEN', 'SKIP_SAVE'})

	elevSource: StringProperty(name = "Elevation source", description = "Elevation source", default='GEOM')
	manualElevation: FloatProperty(name='Base elevation', unit='LENGTH')
	verticalGap: FloatProperty(name='Vertical gap', unit='LENGTH', min=0, default=0)
	featureStart: IntProperty(default=0, min=0, options={'HIDDEN', 'SKIP_SAVE'})
	objElevName: StringProperty(name = "Elevation object name", description = "")

	fieldElevName: StringProperty(name = "Elevation field", description = "Field name")
	fieldExtrudeName: StringProperty(name = "Extrusion field", description = "Field name")
	fieldObjName: StringProperty(name = "Objects names field", description = "Field name")

	# IFC Assignment
	ifcClass: StringProperty(name = "IFC Class", description = "IFC class name for Bonsai/BlenderBIM", default="")
	ifcPredefinedType: StringProperty(name = "IFC Predefined Type", description = "IFC predefined type", default="")
	ifcUserDefinedType: StringProperty(name = "IFC User Defined Type", description = "IFC user defined type", default="")
	psetName: StringProperty(name="Property set name", default="Pset_GIS_Attributes")

	# Random coloring
	useRandomColor: BoolProperty(name = "Random color by field", description = "Assign random colors based on field values", default=False)
	fieldColorName: StringProperty(name = "Color field", description = "Field name for random coloring", default="")

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")]
			)
	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Import to separate objects instead one large object",
			default=False
			)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def __del__(self):
		bpy.context.window.cursor_set('DEFAULT')

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences
		ifc_failures = 0
		if self.ifcClass:
			tool = _get_bonsai_tool()
			model = tool.Ifc.get() if tool is not None else None
			if model is None:
				self.report({'WARNING'}, "No active Bonsai IFC project: only custom properties will be saved")
			else:
				try:
					if _prepare_ifc_geometry(context, model):
						self.report({'WARNING'}, "Bonsai geometry kernel unavailable; switched to OpenCASCADE")
				except Exception:
					log.error("Bonsai geometry kernel is unavailable", exc_info=True)
					self.report({'ERROR'}, "Bonsai geometry kernel unavailable; check Bonsai installation and geometry settings")
					return {'CANCELLED'}

		log.info(f"IFC parameters received: ifcClass={self.ifcClass}, ifcPredefinedType={self.ifcPredefinedType}, ifcUserDefinedType={self.ifcUserDefinedType}")

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')
		t0 = perf_clock()

		bpy.ops.object.select_all(action='DESELECT')

		#Path
		shpName = os.path.basename(self.filepath)[:-4]

		#Get shp reader
		log.info("Read shapefile...")
		try:
			shp = shpReader(self.filepath)
		except Exception as e:
			log.error("Unable to read shapefile", exc_info=True)
			self.report({'ERROR'}, "Unable to read shapefile, check logs")
			return {'CANCELLED'}

		#Check shape type
		shpType = featureType[shp.shapeType]
		log.info('Feature type : ' + shpType)
		if shpType not in ['Point','PolyLine','Polygon','PointZ','PolyLineZ','PolygonZ']:
			self.report({'ERROR'}, "Cannot process multipoint, multipointZ, pointM, polylineM, polygonM and multipatch feature type")
			return {'CANCELLED'}

		if self.elevSource != 'FIELD':
			self.fieldElevName = ''

		if self.elevSource == 'OBJ':
			scn = bpy.context.scene
			elevObj = scn.objects[self.objElevName]
			rayCaster = DropToGround(scn, elevObj)

		#Get fields
		fields = [field for field in shp.fields if field[0] != 'DeletionFlag'] #ignore default DeletionFlag field
		fieldsNames = [field[0] for field in fields]
		log.debug("DBF fields : "+str(fieldsNames))

		if self.useRandomColor and (not self.separateObjects or not self.fieldColorName):
			self.report({'ERROR'}, "Random color requires separate objects and a color field")
			return {'CANCELLED'}

		if self.separateObjects or self.fieldElevName or self.fieldObjName or self.fieldExtrudeName or (self.useRandomColor and self.fieldColorName):
			self.useDbf = True
		else:
			self.useDbf = False

		if self.fieldObjName and self.separateObjects:
			try:
				nameFieldIdx = fieldsNames.index(self.fieldObjName)
			except Exception as e:
				log.error('Unable to find name field', exc_info=True)
				self.report({'ERROR'}, "Unable to find name field")
				return {'CANCELLED'}

		if self.fieldElevName:
			try:
				zFieldIdx = fieldsNames.index(self.fieldElevName)
			except Exception as e:
				log.error('Unable to find elevation field', exc_info=True)
				self.report({'ERROR'}, "Unable to find elevation field")
				return {'CANCELLED'}

			if fields[zFieldIdx][1] not in ['N', 'F', 'L'] :
				self.report({'ERROR'}, "Elevation field do not contains numeric values")
				return {'CANCELLED'}

		if self.fieldExtrudeName:
			try:
				extrudeFieldIdx = fieldsNames.index(self.fieldExtrudeName)
			except ValueError:
				log.error('Unable to find extrusion field', exc_info=True)
				self.report({'ERROR'}, "Unable to find extrusion field")
				return {'CANCELLED'}

			if fields[extrudeFieldIdx][1] not in ['N', 'F', 'L'] :
				self.report({'ERROR'}, "Extrusion field do not contains numeric values")
				return {'CANCELLED'}

		# Color field lookup
		colorFieldIdx = None
		colorMap = {}
		colorMaterials = {}
		if self.useRandomColor and self.fieldColorName:
			try:
				colorFieldIdx = fieldsNames.index(self.fieldColorName)
				log.info(f"Color field '{self.fieldColorName}' found at index {colorFieldIdx}")
			except ValueError:
				log.error('Unable to find color field', exc_info=True)
				self.report({'ERROR'}, "Unable to find color field")
				return {'CANCELLED'}

		#Get shp and scene georef infos
		shpCRS = self.shpCRS
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		scale = geoscn.scale #TODO

		projected_points = None
		if self.targetCRS:
			try:
				if shp.shapeType not in (1, 3, 5):
					raise GeoreferenceError('Georeferenced import currently supports 2D points, lines and polygons')
				bbox = BBOX(shp.bbox)
				if shpCRS != self.targetCRS:
					points = list(dict.fromkeys(tuple(pt[:2]) for shape in shp.iterShapes() for pt in shape.points))
					if not points:
						raise GeoreferenceError('Shapefile has no coordinates')
					transformed = transform_coordinates(shpCRS, self.targetCRS, points)['points']
					projected_points = dict(zip(points, transformed))
					x, y = zip(*transformed)
					bbox = BBOX(min(x), min(y), max(x), max(y))
				dx, dy = _establish_ifc_origin(geoscn, self.targetCRS, bbox, self.placementMode)
			except (ValueError, OSError, RuntimeError) as exc:
				self.report({'ERROR'}, str(exc))
				return {'CANCELLED'}

		if not self.targetCRS and not geoscn.hasCRS:
			try:
				geoscn.crs = shpCRS
			except Exception as e:
				log.error("Cannot set scene crs", exc_info=True)
				self.report({'ERROR'}, "Cannot set scene crs, check logs for more infos")
				return {'CANCELLED'}

		#Init reprojector class
		if not self.targetCRS and geoscn.crs != shpCRS:
			log.info("Data will be reprojected from {} to {}".format(shpCRS, geoscn.crs))
			try:
				rprj = Reproj(shpCRS, geoscn.crs)
			except Exception as e:
				log.error('Reprojection fails', exc_info=True)
				self.report({'ERROR'}, "Unable to reproject data, check logs for more infos.")
				return {'CANCELLED'}
			if rprj.iproj == 'EPSGIO':
				if shp.numRecords > 100:
					self.report({'ERROR'}, "Reprojection through online epsg.io engine is limited to 100 features. \nPlease install GDAL or pyproj module.")
					return {'CANCELLED'}

		#Get bbox
		if not self.targetCRS:
			bbox = BBOX(shp.bbox)
			if geoscn.crs != shpCRS:
				bbox = rprj.bbox(bbox)
			if not geoscn.isGeoref:
				dx, dy = bbox.center
				geoscn.setOriginPrj(dx, dy)
			else:
				dx, dy = geoscn.getOriginPrj()

		#Get reader iterator (using iterator avoids loading all data in memory)
		#warn, shp with zero field will return an empty shapeRecords() iterator
		#to prevent this issue, iter only on shapes if there is no field required
		if self.useDbf:
			#Note: using shapeRecord solve the issue where number of shapes does not match number of table records
			#because it iter only on features with geom and record
			shpIter = shp.iterShapeRecords()
		else:
			shpIter = shp.iterShapes()
		nbFeats = shp.numRecords

		#Create an empty BMesh
		bm = bmesh.new()
		#Extrusion is exponentially slow with large bmesh
		#it's fastest to extrude a small bmesh and then join it to a final large bmesh
		if not self.separateObjects and self.fieldExtrudeName:
			finalBm = bmesh.new()

		progress = -1

		if self.separateObjects:
			layer = bpy.data.collections.new(shpName)
			context.scene.collection.children.link(layer)

		#Main iteration over features
		for i, feat in enumerate(shpIter):

			if self.useDbf:
				shape = feat.shape
				record = feat.record
			else:
				shape = feat

			#Progress infos
			pourcent = round(((i+1)*100)/nbFeats)
			if pourcent in list(range(0, 110, 10)) and pourcent != progress:
				progress = pourcent
				if pourcent == 100:
					print(str(pourcent)+'%')
				else:
					print(str(pourcent), end="%, ")
				sys.stdout.flush() #we need to flush or it won't print anything until after the loop has finished

			#Deal with multipart features
			#If the shape record has multiple parts, the 'parts' attribute will contains the index of
			#the first point of each part. If there is only one part then a list containing 0 is returned
			if (shpType == 'PointZ' or shpType == 'Point'): #point layer has no attribute 'parts'
				partsIdx = [0]
			else:
				try: #prevent "_shape object has no attribute parts" error
					partsIdx = shape.parts
				except Exception as e:
					log.warning('Cannot access "parts" attribute for feature {} : {}'.format(i, e))
					partsIdx = [0]
			nbParts = len(partsIdx)

			#Get list of shape's points
			pts = shape.points
			nbPts = len(pts)

			#Skip null geom
			if nbPts == 0:
				continue #go to next iteration of the loop

			#Reproj geom
			if projected_points is not None:
				pts = [projected_points[tuple(pt[:2])] for pt in pts]
			elif not self.targetCRS and geoscn.crs != shpCRS:
				pts = rprj.pts(pts)

			#Get extrusion offset
			if self.fieldExtrudeName:
				try:
					offset = float(record[extrudeFieldIdx])
				except Exception as e:
					log.warning('Cannot extract extrusion value for feature {} : {}'.format(i, e))
					offset = 0 #null values will be set to zero

			#Iter over parts
			for j in range(nbParts):

				# EXTRACT 3D GEOM

				geom = [] #will contains a list of 3d points

				#Find first and last part index
				idx1 = partsIdx[j]
				if j+1 == nbParts:
					idx2 = nbPts
				else:
					idx2 = partsIdx[j+1]

				#Build 3d geom
				for k, pt in enumerate(pts[idx1:idx2]):

					if self.elevSource == 'OBJ':
						rcHit = rayCaster.rayCast(x=pt[0]-dx, y=pt[1]-dy)
						z = rcHit.loc.z #will be automatically set to zero if not rcHit.hit

					elif self.elevSource == 'FIELD':
						try:
							z = float(record[zFieldIdx])
						except Exception as e:
							log.warning('Cannot extract elevation value for feature {} : {}'.format(i, e))
							z = 0 #null values will be set to zero

					elif self.elevSource == 'MANUAL':
						z = self.manualElevation

					elif shpType[-1] == 'Z' and self.elevSource == 'GEOM':
						z = shape.z[idx1:idx2][k]

					else:
						z = 0

					geom.append((pt[0], pt[1], z + (self.featureStart + i) * self.verticalGap))

				#Shift coords
				geom = [(pt[0]-dx, pt[1]-dy, pt[2]) for pt in geom]


				# BUILD BMESH

				# POINTS
				if (shpType == 'PointZ' or shpType == 'Point'):
					vert = [bm.verts.new(pt) for pt in geom]
					#Extrusion
					if self.fieldExtrudeName and offset > 0:
						vect = (0, 0, offset) #along Z
						result = bmesh.ops.extrude_vert_indiv(bm, verts=vert)
						verts = result['verts']
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# LINES
				if (shpType == 'PolyLine' or shpType == 'PolyLineZ'):
					verts = [bm.verts.new(pt) for pt in geom]
					edges = []
					for i in range(len(geom)-1):
						edge = bm.edges.new( [verts[i], verts[i+1] ])
						edges.append(edge)
					#Extrusion
					if self.fieldExtrudeName and offset > 0:
						vect = (0, 0, offset) # along Z
						result = bmesh.ops.extrude_edge_only(bm, edges=edges)
						verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)]
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# NGONS
				if (shpType == 'Polygon' or shpType == 'PolygonZ'):
					#According to the shapefile spec, polygons points are clockwise and polygon holes are counterclockwise
					#in Blender face is up if points are in anticlockwise order
					geom.reverse() #face up
					geom.pop() #exlude last point because it's the same as first pt
					if len(geom) >= 3: #needs 3 points to get a valid face
						verts = [bm.verts.new(pt) for pt in geom]
						face = bm.faces.new(verts)
						#update normal to avoid null vector
						face.normal_update()
						if face.normal.z < 0: #this is a polygon hole, bmesh cannot handle polygon hole
							pass #TODO
						#Extrusion
						if self.fieldExtrudeName and offset > 0:
							#build translate vector
							if self.extrusionAxis == 'NORMAL':
								normal = face.normal
								vect = normal * offset
							elif self.extrusionAxis == 'Z':
								vect = (0, 0, offset)
							faces = bmesh.ops.extrude_discrete_faces(bm, faces=[face]) #return {'faces': [BMFace]}
							verts = faces['faces'][0].verts
							if self.elevSource == 'OBJ':
								# Making flat roof (TODO add an user input parameter to setup this behaviour)
								z = max([v.co.z for v in verts]) + offset #get max z coord
								for v in verts:
									v.co.z = z
							else:
								##result = bmesh.ops.extrude_face_region(bm, geom=[face]) #return dict {"geom":[BMVert, BMEdge, BMFace]}
								##verts = [elem for elem in result['geom'] if isinstance(elem, bmesh.types.BMVert)] #geom type filter
								bmesh.ops.translate(bm, verts=verts, vec=vect)


			if self.separateObjects:

				name = shpName
				if self.fieldObjName:
					value = record[nameFieldIdx]
					if value is not None:
						name = str(value).strip() or shpName

				#Calc bmesh bbox
				_bbox = getBBOX.fromBmesh(bm)

				#Calc bmesh geometry origin and translate coords according to it
				#then object location will be set to initial bmesh origin
				#its a work around to bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')
				ox, oy, oz = _bbox.center
				oz = _bbox.zmin
				bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

				#Create new mesh from bmesh
				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.clear()

				#Validate new mesh
				mesh.validate(verbose=False)

				#Place obj
				obj = bpy.data.objects.new(name, mesh)
				layer.objects.link(obj)
				context.view_layer.objects.active = obj
				obj.select_set(True)
				obj.location = (ox, oy, oz)

				# bpy operators can be very cumbersome when scene contains lot of objects
				# because it cause implicit scene updates calls
				# so we must avoid using operators when created many objects with the 'separate objects' option)
				##bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

				#write attributes data and build properties dict for IFC Psets
				shapefile_props = dict(zip(fieldsNames, record))
				for field, value in _prepare_pset_properties(shapefile_props).items():
					if value is not None:
						# Blender object properties have a narrower integer range than IFC.
						obj[field] = float(value) if type(value) is int else value

				# Assign IFC properties if enabled
				if self.ifcClass:
					if not assign_ifc_class_to_object(obj, self.ifcClass, self.ifcPredefinedType, self.ifcUserDefinedType, properties=shapefile_props, sync_placement=bool(self.targetCRS), pset_name=self.psetName):
						ifc_failures += 1
						break

				# Reuse a single material and IFC style for each distinct field value.
				if self.useRandomColor and colorFieldIdx is not None:
					try:
						field_value = record[colorFieldIdx]
						if isinstance(field_value, bytes):
							field_value = field_value.decode('utf-8', errors='replace').strip()
						elif field_value is None:
							field_value = ""
						else:
							field_value = str(field_value)
						color = _get_color_for_field_value(field_value, colorMap)
						material = colorMaterials.get(field_value)
						if material is None:
							material = assign_blender_material(obj, color, self.fieldColorName, field_value)
							colorMaterials[field_value] = material
						elif obj.data.materials:
							obj.data.materials[0] = material
						else:
							obj.data.materials.append(material)
						tool = _get_bonsai_tool()
						if tool is not None and self.ifcClass and tool.Ifc.get() is not None:
							element = tool.Ifc.get_entity(obj)
							if element is None:
								raise RuntimeError("Imported object is not linked to an IFC element")
							_assign_ifc_surface_style(tool.Ifc.get(), element, material, tool)
					except Exception:
						log.error("Failed to apply field color", exc_info=True)
						ifc_failures += 1
						break

			elif self.fieldExtrudeName:
				#Join to final bmesh (use from_mesh method hack)
				buff = bpy.data.meshes.new(".temp")
				bm.to_mesh(buff)
				finalBm.from_mesh(buff)
				bpy.data.meshes.remove(buff)
				bm.clear()

		#Write back the whole mesh
		if not self.separateObjects:

			mesh = bpy.data.meshes.new(shpName)

			if self.fieldExtrudeName:
				bm.free()
				bm = finalBm

			if prefs.mergeDoubles:
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
			bm.to_mesh(mesh)

			#Finish
			#mesh.update(calc_edges=True)
			mesh.validate(verbose=False) #return true if the mesh has been corrected
			obj = bpy.data.objects.new(shpName, mesh)
			context.scene.collection.objects.link(obj)
			context.view_layer.objects.active = obj
			obj.select_set(True)
			bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

			# Assign IFC properties if enabled (no individual shapefile record for merged object)
			if self.ifcClass:
				if not assign_ifc_class_to_object(obj, self.ifcClass, self.ifcPredefinedType, self.ifcUserDefinedType, sync_placement=bool(self.targetCRS)):
					ifc_failures += 1

		#free the bmesh
		bm.free()
		if ifc_failures:
			self.report({'ERROR'}, "IFC assignment or coloring failed; model may be partially changed. Undo and check logs")
			return {'CANCELLED'}

		t = perf_clock() - t0
		log.info('Build in %f seconds' % t)

		#Adjust grid size
		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy) #convert shapefile bbox in 3d view space
			adjust3Dview(context, bbox)


		return {'FINISHED'}

class IMPORTGIS_OT_shapefile_batch_file_dialog(Operator):
	"""Select multiple shp files and start batch import"""

	bl_idname = "importgis.shapefile_batch_file_dialog"
	bl_description = 'Batch import ESRI shapefiles (.shp)'
	bl_label = "Batch Import SHP"
	bl_options = {'INTERNAL'}

	directory: StringProperty(
		name="Directory",
		description="Directory of selected files",
		subtype='DIR_PATH')

	files: bpy.props.CollectionProperty(
		type=bpy.types.OperatorFileListElement,
		options={'HIDDEN'})

	filter_glob: StringProperty(
			default = "*.shp",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Select multiple .shp files")
		layout.label(text="Options will be available after selection")

	def execute(self, context):
		global _batch_files
		_batch_files = []
		for file_entry in self.files:
			if file_entry.name.lower().endswith('.shp'):
				_batch_files.append(os.path.join(self.directory, file_entry.name))
		if not _batch_files:
			self.report({'ERROR'}, "No .shp files selected")
			return {'CANCELLED'}
		bpy.ops.importgis.shapefile_batch_props_dialog('INVOKE_DEFAULT')
		return{'FINISHED'}


def _batch_common_fields(filepaths, numeric=False):
	"""Fields shared by all files; numeric choices must be numeric in every file."""
	common = None
	for path in filepaths:
		with shpReader(path) as shp:
			fields = {field[0] for field in shp.fields if field[0] != 'DeletionFlag'
				and (not numeric or field[1] in ('N', 'F', 'L'))}
		common = fields if common is None else common & fields
	return sorted(common or ())


# Blender keeps references to enum strings returned by callbacks.
_batch_field_items = {}


def _batch_field_choices(numeric=False):
	try:
		fields = _batch_common_fields(_batch_files, numeric=numeric)
	except Exception as exc:
		log.warning('Unable to read batch fields: %s', exc)
		fields = []
	key = (tuple(_batch_files), numeric, tuple(fields))
	if key not in _batch_field_items:
		_batch_field_items[key] = [(name, name, 'Field shared by all selected files') for name in fields]
	return _batch_field_items[key]


class IMPORTGIS_OT_shapefile_batch_props_dialog(Operator):
	"""Batch shapefile importer properties dialog"""

	bl_idname = "importgis.shapefile_batch_props_dialog"
	bl_description = 'Batch import ESRI shapefiles (.shp)'
	bl_label = "Batch Import SHP"
	bl_options = {"INTERNAL"}
	crsPreview: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	crsMatches: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	projectCRS: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	suggestedCRS: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
	georefIFC: BoolProperty(default=True, options={'HIDDEN'})
	placementMode: EnumProperty(name='IFC placement', items=_PLACEMENT_MODES, default='MAP', options={'SKIP_SAVE'})
	overrideTarget: BoolProperty(name='Choose a different target CRS', options={'SKIP_SAVE'})
	targetCRS: StringProperty(name='IFC target CRS', options={'SKIP_SAVE'})
	confirmCRS: BoolProperty(name='I verified the source CRS in QGIS', options={'SKIP_SAVE'})

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listFields(self, context):
		return _batch_field_choices()

	def listNumericFields(self, context):
		return _batch_field_choices(numeric=True)

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				objs.append((object.name, object.name, "Object named " + object.name))
		return objs

	reprojection: BoolProperty(
			name="Specifiy shapefile CRS",
			description="Specifiy shapefile CRS if it's different from scene CRS",
			default=False )

	shpCRS: EnumProperty(
		name = "Shapefile CRS",
		description = "Choose a Coordinate Reference System",
		items = listPredefCRS)

	vertsElevSource: EnumProperty(
			name="Elevation source",
			description="Select the source of vertices z value",
			items=[
			('NONE', 'None', "Flat geometry"),
			('GEOM', 'Geometry', "Use z value from shape geometry if exists"),
			('FIELD', 'Field', "Extract z elevation value from an attribute field"),
			('OBJ', 'Object', "Get z elevation value from an existing ground mesh"),
			('MANUAL', 'Manual', "Enter one base elevation for all imported features"),
			],
			default='GEOM')
	manualElevation: FloatProperty(name='Base elevation', unit='LENGTH',
		description='Set the same base Z for all imported features, before any extrusion')
	useVerticalGap: BoolProperty(name='Vertical gap per feature', default=False,
		description='Stack successive features at increasing elevations')
	verticalGap: FloatProperty(name='Gap', unit='LENGTH', min=0, default=0,
		description='Raise each successive feature by this additional amount; changes exported geometry elevations')

	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	fieldElevName: EnumProperty(name="Elevation field", description="Numeric field shared by all selected files", items=listNumericFields)
	useFieldExtrude: BoolProperty(name="Extrusion from field", description="Extrude using a numeric field shared by all selected files", default=False)
	fieldExtrudeName: EnumProperty(name="Extrusion field", description="Numeric field shared by all selected files", items=listNumericFields)
	extrusionAxis: EnumProperty(name="Extrude along", items=[('Z', 'Z axis', 'Extrude along Z axis'), ('NORMAL', 'Normal', 'Extrude along face normal')], default='Z')
	useFieldName: BoolProperty(name="Object name from field", description="Use a field shared by all selected files", default=False)
	fieldObjName: EnumProperty(name="Name field", description="Field shared by all selected files", items=listFields)
	useRandomColor: BoolProperty(name="Random color by field", description="Assign random colors based on field values", default=False)
	fieldColorName: EnumProperty(name="Color field", description="Field shared by all selected files", items=listFields)

	separateObjects: BoolProperty(
			name="Separate objects",
			description="Warning : can be very slow with lot of features",
			default=False )

	assignIFC: BoolProperty(
			name="Create IFC elements",
			description="Assign IFC class to imported objects for Bonsai/BlenderBIM",
			default=True )

	ifcClass: StringProperty(
			name="IFC Class",
			description="IFC class name (e.g., IfcBuilding, IfcSite, IfcBuildingElementProxy)",
			default="IfcBuildingElementProxy" )

	ifcPredefinedType: StringProperty(
			name="IFC Predefined Type",
			description="IFC predefined type (optional)",
			default="" )

	ifcUserDefinedType: StringProperty(
			name="IFC User Defined Type",
			description="IFC user defined type (optional)",
			default="" )

	psetName: StringProperty(
			name="Property set name",
			description="IFC property set for shapefile attributes (blank uses the default)",
			default="Pset_GIS_Attributes" )

	def draw(self, context):
		scn = context.scene
		layout = self.layout

		layout.prop(self, 'vertsElevSource')
		if self.vertsElevSource == 'OBJ':
			layout.prop(self, 'objElevLst')
		elif self.vertsElevSource == 'FIELD':
			layout.prop(self, 'fieldElevName')
		elif self.vertsElevSource == 'MANUAL':
			layout.prop(self, 'manualElevation')
		row = layout.row()
		extrude_column = row.column()
		gap_column = row.column()
		extrude_column.prop(self, 'useFieldExtrude')
		if self.useFieldExtrude:
			extrude_column.prop(self, 'fieldExtrudeName')
			extrude_column.prop(self, 'extrusionAxis')
		gap_column.prop(self, 'useVerticalGap')
		if self.useVerticalGap:
			gap_column.prop(self, 'verticalGap')
			layout.label(text='Successive features stack; IFC elevations change.', icon='INFO')
		layout.prop(self, 'separateObjects')
		if self.separateObjects:
			layout.prop(self, 'useFieldName')
			if self.useFieldName:
				layout.prop(self, 'fieldObjName')
		if self.separateObjects:
			layout.prop(self, 'useRandomColor')
			if self.useRandomColor:
				layout.prop(self, 'fieldColorName')
		if len(_batch_files) > 1:
			layout.label(text='Field choices are shared by all selected files.', icon='INFO')
		layout.label(text='Elevation and extrusion use scene units; no vertical CRS conversion.', icon='INFO')

		layout.separator()
		layout.prop(self, 'assignIFC')
		if self.assignIFC:
			layout.prop(self, 'ifcClass')
			layout.prop(self, 'ifcPredefinedType')
			layout.prop(self, 'ifcUserDefinedType')
			if self.separateObjects:
				layout.prop(self, 'psetName')

		geoscn = GeoScene()
		_draw_crs_preview(layout, self.crsPreview)
		_draw_ifc_crs(self)
		if self.assignIFC and self.georefIFC:
			return
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.shpCRSInputLayout(context)
		else:
			self.shpCRSInputLayout(context)
		georefManagerLayout(self, context)

	def shpCRSInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "shpCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	def invoke(self, context, event):
		_init_crs_dialog(self, _batch_files, context.scene)
		return context.window_manager.invoke_props_dialog(self, width=540)

	def execute(self, context):
		global _batch_files

		try:
			common = set(_batch_common_fields(_batch_files))
			numeric = set(_batch_common_fields(_batch_files, numeric=True))
			for enabled, value, choices, label in (
				(self.vertsElevSource == 'FIELD', self.fieldElevName, numeric, 'elevation'),
				(self.useFieldExtrude, self.fieldExtrudeName, numeric, 'extrusion'),
				(self.separateObjects and self.useFieldName, self.fieldObjName, common, 'name'),
				(self.separateObjects and self.useRandomColor, self.fieldColorName, common, 'color'),
			):
				if enabled and value not in choices:
					self.report({'ERROR'}, 'Choose a valid %s field shared by every selected shapefile' % label)
					return {'CANCELLED'}
		except Exception as exc:
			self.report({'ERROR'}, 'Cannot inspect shapefile fields: %s' % exc)
			return {'CANCELLED'}

		if self.vertsElevSource == 'OBJ':
			if not self.objElevLst:
				self.report({'ERROR'}, "No elevation object")
				return {'CANCELLED'}
			else:
				objElevName = self.objElevLst
		else:
			objElevName = ''

		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if geoscn.hasCRS:
			if self.reprojection:
				shpCRS = self.shpCRS
			else:
				shpCRS = geoscn.crs
		else:
			shpCRS = self.shpCRS
		target = ''
		sources = {}
		if self.assignIFC and self.georefIFC:
			try:
				target, sources = _ifc_crs_plan(self, _batch_files, context.scene)
			except (ValueError, OSError) as exc:
				self.report({'ERROR'}, str(exc))
				return {'CANCELLED'}

		ifcClass = self.ifcClass if self.assignIFC else ""
		ifcPredefinedType = self.ifcPredefinedType if self.assignIFC else ""
		ifcUserDefinedType = self.ifcUserDefinedType if self.assignIFC else ""

		errors = []
		imported = 0
		feature_start = 0
		vertical_gap = self.verticalGap if self.useVerticalGap else 0
		for filepath in _batch_files:
			try:
				if vertical_gap:
					with shpReader(filepath) as reader:
						feature_count = reader.numRecords
				result = bpy.ops.importgis.shapefile(
					filepath=filepath,
					shpCRS=sources.get(filepath, shpCRS),
					targetCRS=target,
					placementMode=self.placementMode,
					elevSource=self.vertsElevSource,
					manualElevation=self.manualElevation,
					verticalGap=vertical_gap,
					featureStart=feature_start,
					fieldElevName=self.fieldElevName if self.vertsElevSource == 'FIELD' else '',
					objElevName=objElevName,
					fieldExtrudeName=self.fieldExtrudeName if self.useFieldExtrude else '',
					fieldObjName=self.fieldObjName if self.separateObjects and self.useFieldName else '',
					extrusionAxis=self.extrusionAxis,
					separateObjects=self.separateObjects,
					ifcClass=ifcClass,
					ifcPredefinedType=ifcPredefinedType,
					ifcUserDefinedType=ifcUserDefinedType,
					psetName=self.psetName,
					useRandomColor=self.separateObjects and self.useRandomColor,
					fieldColorName=self.fieldColorName if self.separateObjects and self.useRandomColor else '')
				if 'FINISHED' in result:
					imported += 1
					if vertical_gap:
						feature_start += feature_count
				else:
					errors.append(os.path.basename(filepath))
					break
			except Exception as e:
				log.error('Batch shapefile import fails for %s', filepath, exc_info=True)
				errors.append(os.path.basename(filepath))
				break

		_batch_files = []

		if errors:
			self.report({'ERROR'}, "Imported %i file(s); stopped on %s. Check logs and undo if IFC changed" % (imported, ', '.join(errors)))
			return {'CANCELLED'}
		else:
			self.report({'INFO'}, "Successfully imported %i shapefile(s)" % imported)

		return{'FINISHED'}


classes = [
	IMPORTGIS_OT_ifc_map_conversion,
	IMPORTGIS_OT_shapefile_file_dialog,
	IMPORTGIS_OT_shapefile_props_dialog,
	IMPORTGIS_OT_shapefile,
	IMPORTGIS_OT_shapefile_batch_file_dialog,
	IMPORTGIS_OT_shapefile_batch_props_dialog
]

def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)
	if bonsai_pset_display.install() is not None:
		bpy.app.timers.register(bonsai_pset_display.install, persistent=True)

def unregister():
	if bpy.app.timers.is_registered(bonsai_pset_display.install):
		bpy.app.timers.unregister(bonsai_pset_display.install)
	bonsai_pset_display.uninstall()
	for cls in classes:
		bpy.utils.unregister_class(cls)
