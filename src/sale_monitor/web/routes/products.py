from flask import Blueprint, request, jsonify
from sale_monitor.domain.models import Product
from sale_monitor.storage.json_store import JSONStore

products_bp = Blueprint('products', __name__)
store = JSONStore('path/to/products.json')  # Update with the actual path to your JSON file

@products_bp.route('/products', methods=['GET'])
def get_products():
    products = store.load_products()
    return jsonify(products), 200

@products_bp.route('/products', methods=['POST'])
def add_product():
    data = request.json
    new_product = Product(**data)
    store.save_product(new_product)
    return jsonify(new_product), 201

@products_bp.route('/products/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    data = request.json
    updated_product = Product(**data)
    store.update_product(product_id, updated_product)
    return jsonify(updated_product), 200

@products_bp.route('/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    store.delete_product(product_id)
    return '', 204