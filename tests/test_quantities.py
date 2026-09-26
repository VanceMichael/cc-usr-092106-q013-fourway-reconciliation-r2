import unittest

from src.quantities import QuantityError, parse_qty


class QuantityTest(unittest.TestCase):
    def test_value_按最小单位(self):
        q = parse_qty({"value": 12}, "盒")
        self.assertEqual(q.value, 12)
        self.assertEqual(q.unit, "盒")

    def test_boxes_按每箱换算并加零盒(self):
        q = parse_qty({"boxes": 3, "loose": 10}, "盒", per_box=20)
        self.assertEqual(q.value, 70)

    def test_boxes_可在行内声明换算量(self):
        q = parse_qty({"boxes": 2, "per_box": 15}, "盒")
        self.assertEqual(q.value, 30)

    def test_缺换算量不猜测(self):
        with self.assertRaises(QuantityError):
            parse_qty({"boxes": 1}, "盒")

    def test_单位不一致报错(self):
        with self.assertRaises(QuantityError):
            parse_qty({"value": 1, "unit": "瓶"}, "盒")


if __name__ == "__main__":
    unittest.main()
