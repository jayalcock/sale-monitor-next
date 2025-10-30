from flask import Flask
from flask_cors import CORS
from sale_monitor.web.routes.products import products_bp

def create_app():
    app = Flask(__name__)
    CORS(app)  # Enable CORS for all routes

    # Register blueprints
    app.register_blueprint(products_bp, url_prefix='/api/products')

    @app.route('/')
    def index():
        return "Welcome to the Sale Monitor API!"

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)