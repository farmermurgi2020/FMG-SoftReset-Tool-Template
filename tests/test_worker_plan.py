import unittest

from geometry import Rect
from worker import Chunk, create_chunk_plan


class WorkerPlanTests(unittest.TestCase):
    def test_plan_protects_any_intersecting_chunk(self):
        chunks = [
            Chunk(0, 0, "/map/0/0.bin", 12, 1),
            Chunk(0, 1, "/map/0/1.bin", 12, 1),
            Chunk(1, 0, "/map/1/0.bin", 12, 1),
        ]
        keep, remove = create_chunk_plan(chunks, [Rect(7, 0, 2, 8)], 8)
        self.assertEqual({chunk.key() for chunk in keep}, {(0, 0), (1, 0)})
        self.assertEqual({chunk.key() for chunk in remove}, {(0, 1)})

    def test_plan_can_include_more_than_ninety_percent_reset_chunks(self):
        chunks = [Chunk(x, 0, f"/map/{x}/0.bin", 12, 1) for x in range(20)]
        keep, remove = create_chunk_plan(chunks, [Rect(0, 0, 8, 8)], 8)
        self.assertEqual(len(keep), 1)
        self.assertEqual(len(remove), 19)


if __name__ == "__main__":
    unittest.main()
