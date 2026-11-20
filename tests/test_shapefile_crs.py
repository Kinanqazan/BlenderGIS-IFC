import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


path = Path(__file__).parents[1] / 'core' / 'proj' / 'shapefile_crs.py'
spec = importlib.util.spec_from_file_location('shapefile_crs', path)
crs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crs)


class InspectCRSTests(unittest.TestCase):
	def test_missing_prj_is_not_guessed(self):
		with patch.object(Path, 'is_file', return_value=False), patch.object(Path, 'glob', return_value=[]):
			with self.assertRaisesRegex(crs.CRSInspectionError, 'Missing .prj'):
				crs.inspect_shapefile_crs(Path('layer.shp'))

	def test_compound_crs_and_uncertain_candidates_are_preserved(self):
		definition = {'type': 'CompoundCRS', 'name': 'Example', 'components': [
			{'type': 'ProjectedCRS', 'name': 'Horizontal'},
			{'type': 'VerticalCRS', 'name': 'Vertical'},
		]}
		output = 'PROJJSON:\n' + json.dumps(definition) + '\nIdentification match count: 2\nEPSG:9933: 70 %\nEPSG:9929: 70 %\n'
		result = type('Result', (), {'returncode': 0, 'stdout': output, 'stderr': ''})()
		stat = type('Stat', (), {'st_size': 100})()
		with patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'stat', return_value=stat), \
			patch.object(crs, '_projinfo_path', return_value=Path('/mock/projinfo')), \
			patch.object(crs.subprocess, 'run', return_value=result):
			inspection = crs.inspect_shapefile_crs(Path('layer.shp'))
		self.assertEqual(inspection['crs'], definition)
		self.assertEqual(inspection['candidates'], [('EPSG:9933', 70), ('EPSG:9929', 70)])

	def test_unavailable_proj_reports_error(self):
		stat = type('Stat', (), {'st_size': 100})()
		with patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'stat', return_value=stat), \
			patch.object(crs, '_projinfo_path', return_value=None):
			with self.assertRaisesRegex(crs.CRSInspectionError, 'unavailable'):
				crs.inspect_shapefile_crs(Path('layer.shp'))


if __name__ == '__main__':
	unittest.main()
