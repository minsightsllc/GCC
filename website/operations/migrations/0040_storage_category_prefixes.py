import operations.models
import operations.storage
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0039_websitegallerypostitem_poster_image"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="cover",
            field=models.FileField(
                blank=True,
                upload_to="Public Content/projects/covers/",
                validators=[operations.models.validate_uploaded_media],
            ),
        ),
        migrations.AlterField(
            model_name="mediaasset",
            name="file",
            field=models.FileField(
                blank=True,
                upload_to=operations.models.media_asset_upload_to,
                validators=[operations.models.validate_uploaded_media],
            ),
        ),
        migrations.AlterField(
            model_name="websitegallerypostitem",
            name="poster_image",
            field=models.ImageField(
                blank=True,
                upload_to="Public Content/website-posters/%Y/%m/",
            ),
        ),
        migrations.AlterField(
            model_name="leadattachment",
            name="file",
            field=models.FileField(
                storage=operations.storage.ContactSubmissionStorage,
                upload_to="Documents and Files/contact-form/%Y/%m/",
                validators=[operations.models.validate_contact_upload],
            ),
        ),
        migrations.AlterField(
            model_name="service",
            name="image",
            field=models.FileField(
                blank=True,
                upload_to="Public Content/services/",
                validators=[operations.models.validate_uploaded_media],
            ),
        ),
        migrations.AlterField(
            model_name="processstep",
            name="image",
            field=models.FileField(
                blank=True,
                upload_to="Public Content/process-steps/",
                validators=[operations.models.validate_uploaded_media],
            ),
        ),
        migrations.AlterField(
            model_name="projectdocument",
            name="file",
            field=models.FileField(
                upload_to="Documents and Files/projects/documents/%Y/%m/",
                validators=[operations.models.validate_uploaded_document],
            ),
        ),
        migrations.AlterField(
            model_name="agreement",
            name="signed_pdf",
            field=models.FileField(
                blank=True,
                upload_to="Documents and Files/projects/agreements/%Y/%m/",
                validators=[operations.models.validate_signed_pdf],
            ),
        ),
        migrations.AlterField(
            model_name="costentry",
            name="receipt",
            field=models.FileField(
                blank=True,
                upload_to="Documents and Files/projects/receipts/%Y/%m/",
                validators=[operations.models.validate_construction_document],
            ),
        ),
    ]
