from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_spanish_defaults_and_snapshot_keys(apps, schema_editor):
    database = schema_editor.connection.alias
    SiteSettings = apps.get_model("operations", "SiteSettings")
    Service = apps.get_model("operations", "Service")
    ProcessStep = apps.get_model("operations", "ProcessStep")
    WebsiteGalleryPost = apps.get_model("operations", "WebsiteGalleryPost")
    MediaPublication = apps.get_model("operations", "MediaPublication")

    translations = {
        "headline": ("Build with confidence.", "Construimos con confianza."),
        "subheadline": (
            "Thoughtful construction, clear communication, and a better experience from first walkthrough to final handoff.",
            "Construcción bien pensada, comunicación clara y una mejor experiencia desde el primer recorrido hasta la entrega final.",
        ),
        "featured_title": ("Coastal Bathroom Renovation", "Renovación de baño costero"),
        "featured_body": (
            "A calm, highly functional renovation shaped around durable materials, thoughtful storage, and the small details that make a space feel finished.",
            "Una renovación tranquila y funcional, con materiales duraderos, almacenamiento bien pensado y detalles que hacen que el espacio se sienta terminado.",
        ),
        "services_title": ("Construction with a clear plan.", "Construcción con un plan claro."),
        "services_intro": (
            "Every project is different. Our job is to make the path from idea to finished space feel straightforward.",
            "Cada proyecto es diferente. Nuestro trabajo es hacer que el camino desde la idea hasta el espacio terminado sea claro y sencillo.",
        ),
        "services_note": (
            "Residential, commercial, renovation, restoration, and project oversight across Southern California.",
            "Construcción residencial y comercial, remodelación, restauración y supervisión de proyectos en el sur de California.",
        ),
        "process_page_title": ("A clearer way to build.", "Una forma más clara de construir."),
        "process_page_intro": (
            "A strong project experience comes from good communication, visible next steps, and an organized handoff.",
            "Una buena experiencia de proyecto se logra con comunicación clara, próximos pasos visibles y una entrega organizada.",
        ),
        "contact_page_kicker": ("Start here", "Comience aquí"),
        "contact_page_title": ("Tell us what you’re building.", "Cuéntenos qué quiere construir."),
        "contact_page_intro": (
            "Share a few details and our team will use them to shape the right next conversation.",
            "Comparta algunos detalles para que nuestro equipo pueda preparar la siguiente conversación adecuada.",
        ),
        "contact_service_areas_heading": ("Proudly serving", "Con orgullo atendemos"),
        "contact_service_areas": (
            "Ventura County\nSanta Barbara County\nLos Angeles County",
            "Condado de Ventura\nCondado de Santa Bárbara\nCondado de Los Ángeles",
        ),
        "contact_form_kicker": ("Request an estimate", "Solicite un presupuesto"),
        "contact_form_title": ("A better first step.", "Un mejor primer paso."),
        "contact_form_intro": (
            "Your request is securely saved for the Grand Coast team to review. We’ll follow up with a clear next step.",
            "Su solicitud se guarda de forma segura para que el equipo de Grand Coast la revise. Nos comunicaremos con usted para indicarle el siguiente paso.",
        ),
        "contact_form_privacy_note": ("Private request · No payment required", "Solicitud privada · No se requiere pago"),
        "contact_form_footer_note": ("Your request stays with Grand Coast.", "Su solicitud permanece con Grand Coast."),
        "contact_direct_heading": ("Contact us directly", "Comuníquese con nosotros"),
        "contact_success_message": ("Thanks for sharing your project. We will be in touch soon.", "Gracias por compartir su proyecto. Nos comunicaremos pronto."),
        "contact_form_submit_label": ("Send request", "Enviar solicitud"),
    }
    for site in SiteSettings.objects.using(database).all().iterator():
        changed = []
        for field, (english, spanish) in translations.items():
            if getattr(site, field) == english and not getattr(site, field + "_es"):
                setattr(site, field + "_es", spanish)
                changed.append(field + "_es")
        if changed:
            site.save(using=database, update_fields=changed)

    service_translations = {
        "renovations": (
            "Renovations",
            "Kitchens, baths, additions, and the thoughtful updates that make a home work better.",
            "Remodelaciones",
            "Cocinas, baños, ampliaciones y mejoras bien pensadas que hacen que una casa funcione mejor.",
        ),
        "custom-homes": (
            "Custom homes",
            "A clear path from early ideas to a finished home shaped around the way you live.",
            "Casas personalizadas",
            "Un camino claro desde las primeras ideas hasta una casa terminada, diseñada para su forma de vivir.",
        ),
        "restoration": (
            "Restoration",
            "Careful assessment and durable repairs for spaces that need a considered second chapter.",
            "Restauración",
            "Evaluaciones cuidadosas y reparaciones duraderas para espacios que necesitan una nueva etapa bien planificada.",
        ),
        "commercial": (
            "Commercial construction",
            "Organized build-outs and owner-side coordination for spaces made to support good work.",
            "Construcción comercial",
            "Adecuaciones bien organizadas y coordinación en representación del propietario para espacios diseñados para apoyar el trabajo.",
        ),
        "project-management": (
            "Project management",
            "Visible next steps, dependable coordination, and a calm point of contact from start to finish.",
            "Gestión de proyectos",
            "Próximos pasos claros, coordinación confiable y una persona de contacto disponible de principio a fin.",
        ),
    }
    for service in Service.objects.using(database).all().iterator():
        values = service_translations.get(service.slug)
        if not values:
            continue
        english_title, english_description, spanish_title, spanish_description = values
        changed = []
        if service.title == english_title and not service.title_es:
            service.title_es = spanish_title
            changed.append("title_es")
        if service.description == english_description and not service.description_es:
            service.description_es = spanish_description
            changed.append("description_es")
        if changed:
            service.save(using=database, update_fields=changed)

    process_translations = {
        "inquire": (
            "Start with a conversation",
            "Tell us what you are building, what matters most, and where you want a clearer path.",
            "Comience con una conversación",
            "Cuéntenos qué quiere construir, qué es lo más importante y en qué aspectos busca mayor claridad.",
        ),
        "plan": (
            "Shape the right plan",
            "We turn the first conversation into scope, priorities, and a practical sequence of decisions.",
            "Definamos el plan adecuado",
            "Convertimos la primera conversación en un alcance, prioridades y una secuencia práctica de decisiones.",
        ),
        "build": (
            "Build with visibility",
            "You always know the current stage, the next action, and what the team is moving forward.",
            "Construimos con claridad",
            "Siempre sabrá en qué etapa está el proyecto, cuál es el siguiente paso y qué está avanzando el equipo.",
        ),
        "handoff": (
            "Finish well",
            "A careful final walkthrough brings the details together and leaves you ready for what comes next.",
            "Terminamos bien",
            "Un recorrido final detallado reúne todos los detalles y le deja listo para lo que sigue.",
        ),
    }
    for step in ProcessStep.objects.using(database).all().iterator():
        values = process_translations.get(step.key)
        if not values:
            continue
        english_title, english_description, spanish_title, spanish_description = values
        changed = []
        if step.title == english_title and not step.title_es:
            step.title_es = spanish_title
            changed.append("title_es")
        if step.description == english_description and not step.description_es:
            step.description_es = spanish_description
            changed.append("description_es")
        if changed:
            step.save(using=database, update_fields=changed)

    for model, keys in (
        (WebsiteGalleryPost, ("title_es", "caption_es", "label_es")),
        (MediaPublication, ("title_es", "caption_es", "label_es")),
    ):
        for record in model.objects.using(database).all().iterator():
            data = dict(record.published_data or {})
            changed = False
            for key in keys:
                if key not in data:
                    data[key] = ""
                    changed = True
            if changed:
                record.published_data = data
                record.save(using=database, update_fields=["published_data"])


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0040_storage_category_prefixes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserLanguagePreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("language", models.CharField(choices=[("en", "English"), ("es", "Spanish")], default="en", max_length=2)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="language_preference", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddField(model_name="sitesettings", name="headline_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="subheadline_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="featured_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="featured_body_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="services_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="services_intro_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="services_note_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="process_page_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="process_page_intro_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="contact_page_kicker_es", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="sitesettings", name="contact_page_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="contact_page_intro_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="contact_service_areas_heading_es", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="sitesettings", name="contact_service_areas_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="contact_form_kicker_es", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="sitesettings", name="contact_form_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="contact_form_intro_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="sitesettings", name="contact_form_privacy_note_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="contact_form_footer_note_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="sitesettings", name="contact_direct_heading_es", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="sitesettings", name="contact_success_message_es", field=models.CharField(blank=True, max_length=240)),
        migrations.AddField(model_name="sitesettings", name="contact_form_submit_label_es", field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name="project", name="public_title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="project", name="public_location_es", field=models.CharField(blank=True, max_length=160)),
        migrations.AddField(model_name="project", name="public_summary_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="service", name="title_es", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="service", name="description_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="processstep", name="title_es", field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name="processstep", name="description_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="mediapublication", name="title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="mediapublication", name="caption_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="mediapublication", name="label_es", field=models.CharField(blank=True, max_length=60)),
        migrations.AddField(model_name="websitegallerypost", name="title_es", field=models.CharField(blank=True, max_length=180)),
        migrations.AddField(model_name="websitegallerypost", name="caption_es", field=models.TextField(blank=True)),
        migrations.AddField(model_name="websitegallerypost", name="label_es", field=models.CharField(blank=True, max_length=60)),
        migrations.RunPython(seed_spanish_defaults_and_snapshot_keys, migrations.RunPython.noop),
    ]
