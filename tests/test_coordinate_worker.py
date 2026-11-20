"""Run with a Python environment that includes pyproj (e.g. QGIS Python)."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).parents[1] / 'core' / 'proj' / 'coordinate_worker.py'
spec = importlib.util.spec_from_file_location('coordinate_worker', path)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@unittest.skipUnless(importlib.util.find_spec('pyproj'), 'pyproj is unavailable')
class CoordinateWorkerTests(unittest.TestCase):
	def test_identity_preserves_coordinates(self):
		result = worker.main({'source': 'EPSG:25832', 'target': 'EPSG:25832',
			'points': [[500000.0, 5400000.0]]})
		self.assertAlmostEqual(result['points'][0][0], 500000.0, places=6)
		self.assertAlmostEqual(result['points'][0][1], 5400000.0, places=6)

	def test_geographic_target_is_rejected(self):
		with self.assertRaisesRegex(ValueError, 'must be projected'):
			worker.main({'source': 'EPSG:25832', 'target': 'EPSG:4326'})

	def test_real_example_to_db_ref(self):
		result = worker.main({'source': 'EPSG:4326', 'target': 'EPSG:9933',
			'points': [[9.5, 49.0]]})
		self.assertGreater(result['points'][0][0], 3500000)
		self.assertGreaterEqual(result['accuracy_metres'], 0)


if __name__ == '__main__':
	unittest.main()
