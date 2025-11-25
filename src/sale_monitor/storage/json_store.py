from typing import List, Dict
import json
import os

class JSONStore:
    """Handles storage of product data in JSON format."""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Ensure that the JSON file exists; create it if it does not."""
        if not os.path.exists(self.file_path):
            with open(self.file_path, 'w') as f:
                json.dump([], f)  # Initialize with an empty list

    def load_products(self) -> List[Dict]:
        """Load product data from the JSON file."""
        with open(self.file_path, 'r') as f:
            return json.load(f)

    def save_products(self, products: List[Dict]):
        """Save product data to the JSON file."""
        with open(self.file_path, 'w') as f:
            json.dump(products, f, indent=2)