from celery import Celery

def make_celery(app):
    celery = Celery(
        app.import_name,
        backend='redis://localhost:6379/0',  # Stocke les résultats
        broker='redis://localhost:6379/0'    # File d’attente
    )
    celery.conf.update(app.config)
    return celery

# Initialisation dans votre app Flask
from flask import Flask
app = Flask(__name__)
celery = make_celery(app)