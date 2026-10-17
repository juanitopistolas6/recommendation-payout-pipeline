"""Recalcula las recomendaciones con el RULES_VERSION actual a partir de las extracciones guardadas.

No llama a Gemini. Una recomendación por (claim, RULES_VERSION): si ya existe, se actualiza.

Uso:
    docker compose exec backend python manage.py recompute_recommendations --ids 1,2,3
    docker compose exec backend python manage.py recompute_recommendations --all
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from claims.models import Claim, Recommendation
from claims.pipeline.rules import RULES_VERSION, build_recommendation


class Command(BaseCommand):
    help = "Recalcula Recommendation desde las ClaimExtraction guardadas, sin llamar a Gemini."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--ids", help="Tracking IDs separados por coma, ej. 1,2,3")
        target.add_argument("--all", action="store_true", help="Todos los claims con una extracción")

    def handle(self, *args, **opts):
        claims = Claim.objects.filter(claim_extractions__isnull=False).distinct()
        if opts["ids"]:
            ids = [i.strip() for i in opts["ids"].split(",") if i.strip()]
            missing = set(ids) - set(Claim.objects.filter(pk__in=ids).values_list("pk", flat=True))
            if missing:
                raise CommandError(f"No existen los claims: {', '.join(sorted(missing))}")
            claims = claims.filter(pk__in=ids)

        routes, created, updated = Counter(), 0, 0
        for claim in claims:
            extraction = claim.claim_extractions.order_by("-created_at").first()  # la más reciente
            rec = build_recommendation(claim, extraction, extraction.lease_extraction)
            _, was_created = Recommendation.objects.update_or_create(
                claim=claim,
                rules_version=RULES_VERSION,
                defaults={
                    field: getattr(rec, field)
                    for field in ("extraction", "recommended_amount", "eligible_owed", "excluded_total",
                                  "cap_applied", "flags", "reasoning", "explanation", "route")
                },
            )
            created += was_created
            updated += not was_created
            routes[rec.route] += 1
            self.stdout.write(f"  {claim.pk}: {rec.recommended_amount} [{rec.route}] {', '.join(rec.flags)}")

        self.stdout.write(self.style.SUCCESS(
            f"\nReglas {RULES_VERSION}: {created} creadas, {updated} actualizadas. "
            f"Rutas: {dict(routes) or 'ninguna'}"
        ))
