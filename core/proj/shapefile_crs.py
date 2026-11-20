"""Inspect shapefile CRS metadata without guessing an authority code.

An ESRI .prj can omit EPSG identifiers, contain a compound horizontal/vertical
CRS, or match several database entries.  Candidate matches are evidence for a
user to review, not permission to change a model's georeferencing.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


class CRSInspectionError(ValueError):
	pass


def _qgis_roots():
	"""Find QGIS installations independently of a standalone PROJ on PATH."""
	path = shutil.which('projinfo')
	if path:
		yield Path(path).parent.parent
	for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramW6432')):
		if root:
			yield from sorted(Path(root).glob('QGIS*'), reverse=True)


def _projinfo_path():
	path = shutil.which('projinfo')
	if path:
		return Path(path)
	if os.name == 'nt':
		for qgis in _qgis_roots():
			candidate = qgis / 'bin' / 'projinfo.exe'
			if candidate.is_file():
				return candidate
	return None


def inspect_shapefile_crs(filepath):
	"""Return the parsed CRS and identification candidates from a .prj file.

	Requires PROJ's ``projinfo`` (available on PATH or in a Windows QGIS
	installation). No network lookup is performed. Missing or unparseable CRS
	metadata raises CRSInspectionError; callers must not infer a default.
	"""
	shp = Path(filepath)
	prj = shp.with_suffix('.prj')
	if not prj.is_file():
		alternatives = list(shp.parent.glob(shp.stem + '.*'))
		prj = next((p for p in alternatives if p.suffix.lower() == '.prj'), prj)
	if not prj.is_file():
		raise CRSInspectionError('Missing .prj file for ' + shp.name)
	if prj.stat().st_size > 1048576:
		raise CRSInspectionError('CRS definition is unexpectedly large: ' + prj.name)
	projinfo = _projinfo_path()
	if projinfo is None:
		raise CRSInspectionError('PROJ projinfo is unavailable; install QGIS or PROJ to inspect .prj files')
	env = os.environ.copy()
	env['PROJ_NETWORK'] = 'OFF'
	data = projinfo.parent.parent / 'share' / 'proj'
	if (data / 'proj.db').is_file():
		env['PROJ_DATA'] = str(data)
		env.pop('PROJ_LIB', None)
	try:
		result = subprocess.run([str(projinfo), '--identify', '-o', 'PROJJSON', '@' + str(prj)],
			capture_output=True, text=True, encoding='utf-8', errors='replace',
			timeout=20, env=env, check=False,
			creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise CRSInspectionError('Could not run projinfo: ' + str(exc)) from exc
	if result.returncode:
		raise CRSInspectionError('Could not parse ' + prj.name + ': ' + result.stderr.strip())
	try:
		start = result.stdout.index('{')
		crs, end = json.JSONDecoder().raw_decode(result.stdout[start:])
		match_text = result.stdout[start + end:]
		matches = [(m.group(1).upper(), int(m.group(2))) for m in
			re.finditer(r'(?m)^\s*(EPSG:\d+)\s*:\s*(\d+)\s*%\s*$', match_text, re.IGNORECASE)]
	except (ValueError, json.JSONDecodeError) as exc:
		raise CRSInspectionError('Unexpected projinfo output for ' + prj.name) from exc
	return {'path': str(prj), 'crs': crs, 'candidates': matches}


def inspect_shapefile_set(filepaths):
	"""Group selected files by exact .prj content, preserving per-file errors."""
	groups = {}
	errors = []
	for filepath in filepaths:
		try:
			inspection = inspect_shapefile_crs(filepath)
			key = json.dumps(inspection['crs'], sort_keys=True)
			if key not in groups:
				groups[key] = {'crs': inspection['crs'], 'candidates': inspection['candidates'], 'files': []}
			groups[key]['files'].append(str(filepath))
		except (CRSInspectionError, OSError) as exc:
			errors.append((str(filepath), str(exc)))
	return list(groups.values()), errors


def transform_coordinates(source, target, points=None):
	"""Choose the best offline, non-ballpark PROJ operation and transform XY.

	Requires QGIS on Windows. A caller must present the operation accuracy to
	the user and must not silently substitute the returned target for a CRS they
	have not selected.
	"""
	if os.name != 'nt':
		raise CRSInspectionError('Coordinate transformation requires a QGIS installation')
	for root in _qgis_roots():
		python = root / 'bin' / 'python.exe'
		homes = sorted((root / 'apps').glob('Python3*'), reverse=True)
		if (root / 'bin' / 'projinfo.exe').is_file() and python.is_file() and homes and (root / 'share' / 'proj' / 'proj.db').is_file():
			break
	else:
		raise CRSInspectionError('QGIS Python with pyproj is unavailable')
	env = os.environ.copy()
	env['PYTHONHOME'] = str(homes[0])
	env['PROJ_DATA'] = str(root / 'share' / 'proj')
	env['PROJ_NETWORK'] = 'OFF'
	env.pop('PYTHONPATH', None)
	env.pop('PROJ_LIB', None)
	request = {'source': source, 'target': target}
	if points is not None:
		request['points'] = points
	try:
		result = subprocess.run([str(python), str(Path(__file__).with_name('coordinate_worker.py'))],
			input=json.dumps(request), capture_output=True, text=True, encoding='utf-8',
			errors='replace', env=env, timeout=60,
			creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
		response = json.loads(result.stdout)
	except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
		raise CRSInspectionError('Could not run QGIS coordinate transformation: ' + str(exc)) from exc
	if result.returncode or not response.get('ok'):
		raise CRSInspectionError(response.get('error') or result.stderr or 'Coordinate transformation failed')
	return response['result']
