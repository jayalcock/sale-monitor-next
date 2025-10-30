import json
import os
import unittest
from src.sale_monitor.storage.json_store import JSONStore
from src.sale_monitor.domain.models import Product

class TestJSONStore(unittest.TestCase):
    def setUp(self):
        self.test_file = 'test_products.json'
        self.store = JSONStore(self.test_file)
        self.product_data = {
            'name': 'Test Product',
            'url': 'http://example.com/test-product',
            'target_price': 50.0,
            'current_price': 45.0,
            'discount_threshold': 10.0,
            'selector': '.price',
            'enabled': True
        }
        self.product = Product(**self.product_data)

    def tearDown(self):
        if os.path.exists(self.test_file):
            os.remove(self.test_file)

    def test_save_and_load(self):
        self.store.save([self.product])
        loaded_products = self.store.load()
        self.assertEqual(len(loaded_products), 1)
        self.assertEqual(loaded_products[0].name, self.product.name)

    def test_load_empty_file(self):
        with open(self.test_file, 'w') as f:
            f.write('[]')
        loaded_products = self.store.load()
        self.assertEqual(len(loaded_products), 0)

    def test_save_overwrite(self):
        self.store.save([self.product])
        new_product_data = {
            'name': 'Another Product',
            'url': 'http://example.com/another-product',
            'target_price': 30.0,
            'current_price': 25.0,
            'discount_threshold': 5.0,
            'selector': '.price',
            'enabled': True
        }
        new_product = Product(**new_product_data)
        self.store.save([new_product])
        loaded_products = self.store.load()
        self.assertEqual(len(loaded_products), 1)
        self.assertEqual(loaded_products[0].name, new_product.name)

if __name__ == '__main__':
    unittest.main()