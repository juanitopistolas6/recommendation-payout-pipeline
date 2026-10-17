from django.db import DatabaseError, connection
from ninja import NinjaAPI

from claims.api import metrics_router
from claims.api import router as claims_router

api = NinjaAPI(title='Corgi Claims API')
api.add_router('/claims/', claims_router)
api.add_router('/metrics', metrics_router)


@api.get('/health/', tags=['infra'])
def health(request):
    """Infrastructure check: confirms Django is up and can query Postgres."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except DatabaseError:
        return api.create_response(
            request, {'status': 'error', 'database': 'error'}, status=503
        )
    return {'status': 'ok', 'database': 'ok'}
