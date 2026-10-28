from django.db import migrations


def migrate_destinations(apps, schema_editor):
    Asset = apps.get_model("operations", "MediaAsset")
    Publication = apps.get_model("operations", "MediaPublication")
    alias = schema_editor.connection.alias
    for asset in Asset.objects.using(alias).select_related("project").exclude(visibility="internal").iterator():
        destinations = ("website", "client") if asset.visibility == "public" else ("client",)
        for destination in destinations:
            data = {"title": asset.title, "caption": asset.caption, "label": "", "location": "", "client_name": "", "sort_order": 0}
            Publication.objects.using(alias).get_or_create(
                asset_id=asset.pk, destination=destination,
                defaults={"title": asset.title, "caption": asset.caption,
                          "published_at": asset.created_at, "published_data": data,
                          "shared_client_id": asset.project.client_id if destination == "client" and asset.project_id else None},
            )


class Migration(migrations.Migration):
    dependencies = [("operations", "0032_media_publications_and_public_content")]
    operations = [migrations.RunPython(migrate_destinations, migrations.RunPython.noop)]
