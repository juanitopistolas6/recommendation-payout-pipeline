# Quita los id autoincrementales y usa el Tracking Number como identificador:
#   claims_claim        -> claims          (llave primaria: tracking_id)
#   claims_claimoutcome -> claims_outcome  (llave primaria: tracking_id, FK a claims)
#
# Escrita a mano: la migración automática intentaba borrar claims_claim.id mientras
# claims_claimoutcome.claim_id (FK bigint) seguía apuntando a él, y Postgres lo rechaza.
# Aquí se mueve primero la FK de ClaimOutcome al tracking number, conservando sus filas.

import django.db.models.deletion
from django.db import migrations, models


def remember_tracking_ids(apps, schema_editor):
    ClaimOutcome = apps.get_model("claims", "ClaimOutcome")
    for outcome in ClaimOutcome.objects.select_related("claim"):
        outcome.claim_tracking = outcome.claim.tracking_number
        outcome.save(update_fields=["claim_tracking"])


def relink_outcomes(apps, schema_editor):
    ClaimOutcome = apps.get_model("claims", "ClaimOutcome")
    for outcome in ClaimOutcome.objects.all():
        outcome.claim_id = outcome.claim_tracking
        outcome.save(update_fields=["claim"])


class Migration(migrations.Migration):

    dependencies = [
        ('claims', '0001_initial'),
    ]

    operations = [
        # 1. Guardar a qué claim pertenece cada outcome usando el tracking number
        migrations.AddField(
            model_name='claimoutcome',
            name='claim_tracking',
            field=models.CharField(max_length=32, null=True),
        ),
        migrations.RunPython(remember_tracking_ids, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='claimoutcome',
            name='claim',
        ),

        # 2. Claim: sin id, tracking_id como llave primaria, tabla "claims"
        migrations.RemoveField(
            model_name='claim',
            name='id',
        ),
        migrations.AlterField(
            model_name='claim',
            name='tracking_number',
            field=models.CharField(max_length=32, primary_key=True, serialize=False),
        ),
        migrations.RenameField(
            model_name='claim',
            old_name='tracking_number',
            new_name='tracking_id',
        ),
        migrations.AlterModelOptions(
            name='claim',
            options={'ordering': ['source_row']},
        ),
        migrations.AlterModelTable(
            name='claim',
            table='claims',
        ),

        # 3. ClaimOutcome: volver a enlazar por tracking_id, que pasa a ser su llave primaria
        migrations.AddField(
            model_name='claimoutcome',
            name='claim',
            field=models.OneToOneField(db_column='tracking_id', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='outcome', to='claims.claim'),
        ),
        migrations.RunPython(relink_outcomes, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='claimoutcome',
            name='claim_tracking',
        ),
        migrations.RemoveField(
            model_name='claimoutcome',
            name='id',
        ),
        migrations.AlterField(
            model_name='claimoutcome',
            name='claim',
            field=models.OneToOneField(db_column='tracking_id', on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='outcome', serialize=False, to='claims.claim'),
        ),
        migrations.AlterModelTable(
            name='claimoutcome',
            table='claims_outcome',
        ),
    ]
