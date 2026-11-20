"""Conservative IFC4 map-conversion preparation for GIS imports.

The caller must independently establish the projected CRS and origin.  Do not
call this with guessed CRS values or use it to re-georeference an existing
model: adding a map conversion changes the global position of every product.
"""
import math


class GeoreferenceError(ValueError):
	pass


def existing_map_origin(model, crs, unit_scale):
	"""Reuse a compatible map conversion without changing the IFC."""
	if model.schema.upper() != 'IFC4':
		raise GeoreferenceError('Shapefile georeferencing requires an IFC4 project')
	operations = model.by_type('IfcCoordinateOperation')
	if not operations:
		return None
	if len(operations) != 1 or not operations[0].is_a('IfcMapConversion'):
		raise GeoreferenceError('The project has an unsupported coordinate operation')
	op = operations[0]
	unit = op.TargetCRS.MapUnit
	if (op.TargetCRS.Name != crs or not unit or not unit.is_a('IfcSIUnit')
		or unit.Name != 'METRE' or unit.Prefix):
		raise GeoreferenceError('The target CRS or map unit differs from the existing IFC')
	from ifcopenshell.util.geolocation import auto_xyz2enh
	origin = auto_xyz2enh(model, 0, 0, 0)
	if not all(math.isfinite(v) for v in origin) or abs(origin[2]) > 1e-6:
		raise GeoreferenceError('The existing IFC has a vertical offset or invalid origin')
	for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
		point = auto_xyz2enh(model, *(value / unit_scale for value in axis))
		if any(abs(p - o - v) > 1e-6 for p, o, v in zip(point, origin, axis)):
			raise GeoreferenceError('The existing IFC uses a rotation or scale that this import cannot reuse')
	return float(origin[0]), float(origin[1])


def validate_new_map_conversion(model, eastings, northings, horizontal_crs, unit_scale):
	"""Validate an empty, unreferenced IFC model without changing it."""
	if model.schema.upper() != 'IFC4':
		raise GeoreferenceError('Automatic map conversion currently requires IFC4')
	if not horizontal_crs or not horizontal_crs.strip():
		raise GeoreferenceError('A confirmed projected CRS is required')
	if not all(math.isfinite(float(v)) for v in (eastings, northings, unit_scale)) or unit_scale <= 0:
		raise GeoreferenceError('Invalid origin or project unit scale')
	if model.by_type('IfcCoordinateOperation') or model.by_type('IfcProjectedCRS'):
		raise GeoreferenceError('The IFC already has georeferencing; existing coordinates will not be replaced')
	if any(getattr(product, 'Representation', None) for product in model.by_type('IfcProduct')):
		raise GeoreferenceError('The IFC already contains geometry; adding a map conversion would move it globally')
	contexts = [c for c in model.by_type('IfcGeometricRepresentationContext')
		if not c.is_a('IfcGeometricRepresentationSubContext') and c.CoordinateSpaceDimension == 3]
	if len(contexts) != 1:
		raise GeoreferenceError('Expected exactly one 3D IFC representation context')
	wcs = contexts[0].WorldCoordinateSystem
	coords = tuple(wcs.Location.Coordinates)
	if any(abs(c) > 1e-9 for c in coords):
		raise GeoreferenceError('The IFC has a non-zero world coordinate system')
	if wcs.Axis and tuple(wcs.Axis.DirectionRatios) != (0.0, 0.0, 1.0):
		raise GeoreferenceError('The IFC world coordinate system is rotated')
	if wcs.RefDirection and tuple(wcs.RefDirection.DirectionRatios) != (1.0, 0.0, 0.0):
		raise GeoreferenceError('The IFC world coordinate system is rotated')


def create_map_conversion(model, eastings, northings, horizontal_crs, unit_scale, vertical_datum=None):
	"""Create map conversion for metre map coordinates and aligned local axes.

	The zero height is a local reference, not evidence of a measured elevation.
	"""
	validate_new_map_conversion(model, eastings, northings, horizontal_crs, unit_scale)
	import ifcopenshell.api.georeference
	import ifcopenshell.api.unit
	map_unit = ifcopenshell.api.unit.add_si_unit(model, unit_type='LENGTHUNIT')
	ifcopenshell.api.georeference.add_georeferencing(model, name=horizontal_crs)
	crs = {'Name': horizontal_crs, 'MapUnit': map_unit}
	if vertical_datum:
		crs['VerticalDatum'] = vertical_datum
	ifcopenshell.api.georeference.edit_georeferencing(model, projected_crs=crs,
		coordinate_operation={'Eastings': float(eastings), 'Northings': float(northings),
			'OrthogonalHeight': 0.0, 'XAxisAbscissa': 1.0, 'XAxisOrdinate': 0.0,
			'Scale': float(unit_scale)})


def validate_new_direct_placement(model, eastings, northings, crs, unit_scale):
	"""Only shift an unused IFC project with its Site at the model origin."""
	validate_new_map_conversion(model, eastings, northings, crs, unit_scale)
	from ifcopenshell.util.placement import get_local_placement
	sites = model.by_type('IfcSite')
	if len(sites) != 1 or not sites[0].ObjectPlacement or not sites[0].ObjectPlacement.is_a('IfcLocalPlacement'):
		raise GeoreferenceError('Direct Site placement requires a fresh IFC4 project with one Site')
	placement = get_local_placement(sites[0].ObjectPlacement)
	if any(abs(placement[row, col] - (row == col)) > 1e-9 for row in range(4) for col in range(4)):
		raise GeoreferenceError('Direct placement requires a Site at the origin with no rotation')
	return sites[0]


def set_direct_site_placement(model, eastings, northings, crs, unit_scale):
	"""Put the Site and its descendants in projected axes, with no map conversion."""
	from ifcopenshell.api.geometry import edit_object_placement
	from numpy import eye
	site = validate_new_direct_placement(model, eastings, northings, crs, unit_scale)
	matrix = eye(4)
	matrix[0, 3], matrix[1, 3] = eastings, northings
	edit_object_placement(model, product=site, matrix=matrix, is_si=True, should_transform_children=True)
