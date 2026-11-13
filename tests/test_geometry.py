import unittest

from geometry import GeometryError, Rect, protected_chunks, rectangles_from_export


class GeometryTests(unittest.TestCase):
    def test_negative_coordinates_use_floor_partition(self):
        area = Rect(-9, -8, 1, 1)
        self.assertEqual(protected_chunks([(-2, -1), (-1, -1)], [area], 8), {(-2, -1)})

    def test_touching_chunk_boundary_does_not_protect_neighbor(self):
        area = Rect(8, 0, 2, 8)
        self.assertEqual(protected_chunks([(0, 0), (1, 0)], [area], 8), {(1, 0)})

    def test_partial_overlap_protects_whole_chunk(self):
        area = Rect(7, 7, 2, 2)
        self.assertEqual(protected_chunks([(0, 0), (1, 0), (0, 1), (1, 1)], [area], 8),
                         {(0, 0), (1, 0), (0, 1), (1, 1)})

    def test_bad_coordinates_rejected(self):
        with self.assertRaises(GeometryError):
            Rect.from_mapping({"x": 1.5, "y": 0, "w": 1, "h": 1})

    def test_non_positive_dimensions_rejected(self):
        with self.assertRaises(GeometryError):
            Rect.from_mapping({"x": 0, "y": 0, "w": 0, "h": 1})

    def test_malformed_export_zone_is_not_silently_ignored(self):
        with self.assertRaises(GeometryError):
            rectangles_from_export({"zones": [{"x": 0, "y": 0, "w": 1, "h": 1}, None]})


if __name__ == "__main__":
    unittest.main()
