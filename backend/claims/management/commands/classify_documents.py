"""Clasifica los documentos registrados (doc_type pending o unknown) y resume por método.

Uso:
    docker compose exec backend python manage.py classify_documents --ids 1,2,3
    docker compose exec backend python manage.py classify_documents --ids 1,2,3 --no-llm   # solo extensión y reglas
    docker compose exec backend python manage.py classify_documents --all
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from claims.llm.errors import DailyQuotaExceeded, LLMError, LLMStageNotConfigured
from claims.models import Claim, ClaimDocument
from claims.pipeline.classify import classify_claim_documents


class Command(BaseCommand):
    help = "Asigna doc_type a los documentos: extensión -> reglas -> LLM en lote (Prompt C)."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--ids", help="Tracking IDs separados por coma, ej. 1,2,3")
        target.add_argument("--all", action="store_true", help="Todos los claims con documentos")
        parser.add_argument("--no-llm", action="store_true", help="Solo extensión y reglas; lo demás queda unknown")

    def handle(self, *args, **opts):
        if opts["ids"]:
            ids = [i.strip() for i in opts["ids"].split(",") if i.strip()]
            claims = list(Claim.objects.filter(pk__in=ids))
            missing = set(ids) - {c.pk for c in claims}
            if missing:
                raise CommandError(f"No existen los claims: {', '.join(sorted(missing))}")
        else:
            claims = list(Claim.objects.filter(documents__isnull=False).distinct())

        use_llm = not opts["no_llm"]
        errors = []
        for claim in claims:
            try:
                classify_claim_documents(claim, use_llm=use_llm)
            except LLMStageNotConfigured as exc:
                self.stdout.write(self.style.WARNING(f"LLM desactivado: {exc}. Se sigue solo con extensión y reglas."))
                use_llm = False
                classify_claim_documents(claim, use_llm=False)
            except DailyQuotaExceeded as exc:
                errors.append(f"{claim.pk}: {exc}")
                self.stdout.write(self.style.ERROR(f"Cuota diaria agotada, se detiene: {exc}"))
                break
            except LLMError as exc:
                errors.append(f"{claim.pk}: {type(exc).__name__}: {exc}")

        self._report(claims, errors)

    def _report(self, claims, errors):
        docs = ClaimDocument.objects.filter(claim__in=claims)
        by_method = Counter(docs.values_list("classification_method", flat=True))
        by_type = Counter(docs.values_list("doc_type", flat=True))

        self.stdout.write(self.style.SUCCESS(f"\n{len(claims)} claims, {docs.count()} documentos"))
        self.stdout.write("Resueltos por método:")
        for method in ClaimDocument.ClassificationMethod.values:
            if by_method[method]:
                self.stdout.write(f"  {method:16} {by_method[method]}")
        self.stdout.write(f"  {'sin resolver':16} {by_type['unknown'] + by_type['pending']}")
        self.stdout.write("Por doc_type:")
        for doc_type, count in by_type.most_common():
            self.stdout.write(f"  {doc_type:20} {count}")
        for e in errors:
            self.stdout.write(self.style.ERROR(f"  Error {e}"))
