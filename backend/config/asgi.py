"""
ASGI config for config project (uvicorn).

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.conf import settings
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_asgi_application()

if settings.DEBUG:
    # runserver servía /static/ solo; uvicorn no. Sin esto /admin/ y /django-rq/ pierden sus estilos.
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    application = ASGIStaticFilesHandler(application)
