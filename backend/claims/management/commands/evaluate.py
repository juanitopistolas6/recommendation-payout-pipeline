"""Imprime la evaluación en una línea: MAE de la IA vs. la regla simple, contra la decisión humana.

Uso:
    docker compose exec backend python manage.py evaluate
    docker compose exec backend python manage.py evaluate --rules-version v1

    IA: MAE $663.00 | Regla simple: MAE $2,114.54 | 68.6% menos error | Bias IA: -$523.33 | n=6
"""

from django.core.management.base import BaseCommand

from claims.evaluation.metrics import evaluate
from claims.pipeline.rules import RULES_VERSION


def usd(value) -> str:
    if value is None:
        return "—"
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def improvement(pct) -> str:
    if pct is None:
        return "mejora —"
    return f"{pct}% menos error" if pct >= 0 else f"{-pct}% más error"


def summary_line(report: dict) -> str:
    return (f"IA: MAE {usd(report['ai_mae'])} | {report['baseline_name']}: MAE {usd(report['baseline_mae'])} | "
            f"{improvement(report['improvement_pct'])} | Bias IA: {usd(report['ai_bias'])} | n={report['n']}")


class Command(BaseCommand):
    help = "Compara el MAE de la IA con el de la regla simple min(claim_amount, max_benefit)."

    def add_arguments(self, parser):
        parser.add_argument("--rules-version", default=RULES_VERSION, help=f"Default: {RULES_VERSION}")

    def handle(self, *args, **opts):
        report = evaluate(opts["rules_version"])
        if report["n"] == 0:
            self.stdout.write(self.style.WARNING(
                f"Nada que evaluar en rules_version {report['rules_version']}: hacen falta claims con "
                "recomendación de esa versión y decisión humana (approved_benefit)."
            ))
            return
        self.stdout.write(summary_line(report))
