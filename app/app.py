from app import create_app

# Compatibilidad total:
# - gunicorn wsgi:app
# - gunicorn app:app
# - gunicorn app.app:app
#
# Todos apuntan al mismo factory moderno del ERP.
app = create_app()
