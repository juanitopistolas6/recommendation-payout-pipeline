"""ClaimDocument.file_path y converted_pdf_path: de absolutas (/data/claims/...) a relativas a DATA_ROOT
(claims/...). Si alguna ruta absoluta no empieza con DATA_ROOT, se detiene y lista los casos en lugar de adivinar.
"""

from django.conf import settings
from django.db import migrations

FIELDS = ("file_path", "converted_pdf_path")


def to_relative(apps, schema_editor):
    ClaimDocument = apps.get_model("claims", "ClaimDocument")
    prefix = settings.DATA_ROOT.rstrip("/") + "/"

    changes, invalid = [], []
    for doc in ClaimDocument.objects.only("id", *FIELDS).iterator():
        updates = {}
        for field in FIELDS:
            value = getattr(doc, field)
            if not value or not (value.startswith("/") or "\\" in value or value[1:2] == ":"):
                continue  # vacía o ya relativa
            if value.startswith(prefix) and "\\" not in value:
                updates[field] = value[len(prefix):]
            else:
                invalid.append(f"  ClaimDocument {doc.id}.{field}: {value}")
        if updates:
            changes.append((doc.id, updates))

    if invalid:
        raise RuntimeError(
            f"{len(invalid)} rutas no empiezan con DATA_ROOT ({prefix}); corrígelas a mano y vuelve a migrar:\n"
            + "\n".join(invalid[:50]) + ("\n  ..." if len(invalid) > 50 else "")
        )
    for doc_id, updates in changes:
        ClaimDocument.objects.filter(id=doc_id).update(**updates)


def to_absolute(apps, schema_editor):
    ClaimDocument = apps.get_model("claims", "ClaimDocument")
    prefix = settings.DATA_ROOT.rstrip("/") + "/"
    for doc in ClaimDocument.objects.only("id", *FIELDS).iterator():
        updates = {f: prefix + getattr(doc, f) for f in FIELDS if getattr(doc, f) and not getattr(doc, f).startswith("/")}
        if updates:
            ClaimDocument.objects.filter(id=doc.id).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0006_alter_leaseextraction_options_and_more"),
    ]

    operations = [
        migrations.RunPython(to_relative, to_absolute),
    ]
