"""Offline coordinate operations in a separate Python with pyproj installed.

Communicates one request/response as JSON on stdin/stdout. This is run by the
add-on and must not execute GIS data as code.
"""
import json
import sys


def main(request):
	from pyproj import CRS, network
	from pyproj.transformer import TransformerGroup
	network.set_network_enabled(False)
	source = CRS.from_user_input(request['source'])
	target = CRS.from_user_input(request['target'])
	if source.is_compound:
		source = source.sub_crs_list[0]
	if target.is_compound:
		target = target.sub_crs_list[0]
	if not target.is_projected:
		raise ValueError('The IFC target CRS must be projected')
	if any(abs(axis.unit_conversion_factor - 1.0) > 1e-12 for axis in target.axis_info[:2]):
		raise ValueError('The IFC target CRS must use metres')
	group = TransformerGroup(source, target, always_xy=True, allow_ballpark=False)
	if not group.best_available:
		raise ValueError('Best transformation requires unavailable PROJ grid files')
	if not group.transformers:
		raise ValueError('No non-ballpark coordinate operation is available')
	if len(group.transformers) > 1:
		raise ValueError('Multiple coordinate operations are available; select a verified operation in QGIS')
	transformer = group.transformers[0]
	if transformer.accuracy < 0 and not source.equals(target, ignore_axis_order=True):
		raise ValueError('Transformation accuracy is unknown')
	result = {'description': transformer.description, 'accuracy_metres': transformer.accuracy,
		'target_name': target.name}
	if 'points' in request:
		points = request['points']
		if points:
			x, y = transformer.transform([p[0] for p in points], [p[1] for p in points], errcheck=True)
			result['points'] = list(zip(x, y))
		else:
			result['points'] = []
	return result


if __name__ == '__main__':
	try:
		request = json.load(sys.stdin)
		response = {'ok': True, 'result': main(request)}
	except Exception as exc:
		response = {'ok': False, 'error': str(exc)}
	json.dump(response, sys.stdout, allow_nan=False)
