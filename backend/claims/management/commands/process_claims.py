"""Procesa claims de punta a punta.

Uso:
    docker compose exec backend python manage.py process_claims --ids 1,2,3            # encola (worker)
    docker compose exec backend python manage.py process_claims --ids 1,2,3 --sync     # directo, para depurar
    docker compose exec backend python manage.py process_claims --ids 1 --sync --force-from-step 3

Por defecto encola cada claim para el worker. --sync corre directo en este proceso, sin cola.
"""

from django.core.management.base import BaseCommand, CommandError

from claims.llm.errors import DailyQuotaExceeded
from claims.models import Claim
from claims.pipeline.orchestrator import process_claim


class Command(BaseCommand):
    help = "Procesa claims: clasificación -> contrato -> claim -> recomendación."

    def add_arguments(self, parser):
        parser.add_argument("--ids", required=True, help="Tracking IDs separados por coma, ej. 1,2,3")
        parser.add_argument("--force-from-step", type=int, choices=[1, 2, 3, 4],
                            help="Ignora la caché desde este paso: 1 clasificación, 2 contrato, 3 claim, 4 reglas")
        parser.add_argument("--sync", action="store_true", help="Corre directo, sin cola")

    def handle(self, *args, **opts):
        ids = [i.strip() for i in opts["ids"].split(",") if i.strip()]
        missing = set(ids) - set(Claim.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if missing:
            raise CommandError(f"No existen los claims: {', '.join(sorted(missing))}")
        if not opts["sync"]:
            self._enqueue(ids, opts["force_from_step"])
            return

        for tracking in ids:
            self.stdout.write(f"Claim {tracking}...")
            try:
                process_claim(tracking, force_from_step=opts["force_from_step"])
            except DailyQuotaExceeded:
                self._print(tracking)
                self.stdout.write(self.style.ERROR("Cuota diaria agotada: se detiene el lote."))
                break
            self._print(tracking)

    def _enqueue(self, ids, force_from_step):
        from claims.jobs import enqueue_claim, queue_position

        claims = Claim.objects.in_bulk(ids)
        for tracking in ids:  # en el orden pedido
            claim = claims[tracking]
            status = enqueue_claim(claim, force_from_step=force_from_step)
            position = queue_position(claim.pk)
            where = f"posición {position}" if position else "ya empezó"
            self.stdout.write(f"Claim {claim.pk}: {status} ({where})")

    def _print(self, tracking):
        claim = Claim.objects.get(pk=tracking)
        rec = claim.recommendations.order_by("-created_at").first()
        line = f"  {claim.processing_status}"
        if claim.failure_reason:
            line += f": {claim.failure_reason}"
        elif rec is not None:
            line += f" -> {rec.recommended_amount} [{rec.route}] {', '.join(rec.flags)}"
        style = self.style.SUCCESS if claim.processing_status == "done" else self.style.WARNING
        self.stdout.write(style(line))
