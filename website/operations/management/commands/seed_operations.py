from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.utils import timezone

from operations.models import (
    Activity,
    AdminSecurityProfile,
    Client,
    Estimate,
    EstimateLineItem,
    Lead,
    MediaAsset,
    Milestone,
    ProcessStep,
    Project,
    ProjectUpdate,
    Service,
    SiteSettings,
    EmployeeProfile,
    Task,
)
from operations.services import ensure_role_groups


class Command(BaseCommand):
    help = "Populate or update the local Grand Coast Operations sample records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--client-password",
            help="Optionally set a password for the seeded Maya client account.",
        )

    def handle(self, *args, **options):
        staff_user = get_user_model().objects.filter(is_staff=True).order_by("id").first()
        role_groups = ensure_role_groups()
        for admin_user in get_user_model().objects.filter(is_superuser=True):
            AdminSecurityProfile.objects.get_or_create(user=admin_user)
        if staff_user:
            staff_user.groups.add(role_groups["Owner"] if staff_user.is_superuser else role_groups["Office"])
            EmployeeProfile.objects.get_or_create(user=staff_user)
            for staff_member in get_user_model().objects.filter(is_staff=True):
                EmployeeProfile.objects.get_or_create(user=staff_member)
        services = self.seed_services()
        process_steps = self.seed_process_steps()
        clients = self.seed_clients(options.get("client_password"), staff_user)
        leads = self.seed_leads(clients, staff_user)
        estimates = self.seed_estimates(leads, clients, staff_user)
        projects = self.seed_projects(leads, clients, estimates, staff_user)
        self.seed_content(projects["bathroom"])
        self.seed_milestones(projects)
        self.seed_updates(projects, staff_user)
        self.seed_media(projects, staff_user)
        self.seed_tasks(projects, leads, staff_user)
        self.seed_activities(leads, estimates, projects, staff_user)
        self.stdout.write(
            self.style.SUCCESS(
                "Operations data is ready: %d services, %d process steps, %d clients, %d leads, %d estimates, and %d projects."
                % (len(services), len(process_steps), len(clients), len(leads), len(estimates), len(projects))
            )
        )

    def seed_services(self):
        specs = [
            ("renovations", "Renovations", "Kitchens, baths, additions, and the thoughtful updates that make a home work better.", "operations/images/hero-kitchen.png", 1),
            ("custom-homes", "Custom homes", "A clear path from early ideas to a finished home shaped around the way you live.", "operations/images/project-adu.png", 2),
            ("restoration", "Restoration", "Careful assessment and durable repairs for spaces that need a considered second chapter.", "operations/images/project-bathroom.png", 3),
            ("commercial", "Commercial construction", "Organized build-outs and owner-side coordination for spaces made to support good work.", "operations/images/progress-kitchen.png", 4),
            ("project-management", "Project management", "Visible next steps, dependable coordination, and a calm point of contact from start to finish.", "operations/images/project-adu.png", 5),
        ]
        return [
            Service.objects.get_or_create(
                slug=slug,
                defaults={"title": title, "description": description, "image_path": image_path, "sort_order": order},
            )[0]
            for slug, title, description, image_path, order in specs
        ]

    def seed_process_steps(self):
        specs = [
            ("inquire", "Start with a conversation", "Tell us what you are building, what matters most, and where you want a clearer path."),
            ("plan", "Shape the right plan", "We turn the first conversation into scope, priorities, and a practical sequence of decisions."),
            ("build", "Build with visibility", "You always know the current stage, the next action, and what the team is moving forward."),
            ("handoff", "Finish well", "A careful final walkthrough brings the details together and leaves you ready for what comes next."),
        ]
        return [
            ProcessStep.objects.get_or_create(
                key=key,
                defaults={"title": title, "description": description, "sort_order": index},
            )[0]
            for index, (key, title, description) in enumerate(specs, start=1)
        ]

    def seed_clients(self, client_password, staff_user):
        specs = [
            ("maya.thompson@example.com", "Maya Thompson", "", "(805) 555-0148"),
            ("rivera.family@example.com", "James & Ana Rivera", "Rivera family", "(805) 555-0162"),
            ("miguel.santos@example.com", "Miguel Santos", "", "(805) 555-0174"),
            ("hello@harborstudio.example.com", "Harbor Studio", "Harbor Studio", "(805) 555-0183"),
            ("priya.patel@example.com", "Priya Patel", "", "(805) 555-0191"),
            ("ops@northshorefoods.example.com", "North Shore Foods", "North Shore Foods", "(805) 555-0198"),
        ]
        clients = {}
        for email, name, company, phone in specs:
            client, _ = Client.objects.get_or_create(
                email=email,
                defaults={"name": name, "company": company, "phone": phone},
            )
            if not client.phone:
                client.phone = phone
                client.save(update_fields=["phone", "updated_at"])
            clients[email] = client

        if client_password:
            user_model = get_user_model()
            user, created = user_model.objects.get_or_create(
                username="maya.client",
                defaults={
                    "email": clients["maya.thompson@example.com"].email,
                    "first_name": "Maya",
                    "last_name": "Thompson",
                },
            )
            if user.is_staff:
                raise RuntimeError("maya.client already belongs to a staff account.")
            user.set_password(client_password)
            user.save(update_fields=["password"])
            maya = clients["maya.thompson@example.com"]
            maya.user = user
            maya.save(update_fields=["user", "updated_at"])
            self.stdout.write("Seeded client login: maya.client")
        return clients

    def seed_leads(self, clients, staff_user):
        specs = [
            ("maya.thompson@example.com", "Maya Thompson", "Renovations", "Ventura, CA", "qualified", True, "$40k-$55k", "Start in 4-6 weeks", "Wants a calmer kitchen layout with durable, family-friendly finishes."),
            ("rivera.family@example.com", "James & Ana Rivera", "Residential construction", "Camarillo, CA", "new", False, "$85k-$120k", "This summer", "Exploring a backyard ADU for family visits and long-term flexibility."),
            ("miguel.santos@example.com", "Miguel Santos", "Restoration", "Oxnard, CA", "contacted", False, "$25k-$40k", "As soon as possible", "Needs an initial assessment after water damage in the lower level."),
            ("hello@harborstudio.example.com", "Harbor Studio", "Commercial construction", "Santa Barbara, CA", "quoted", True, "$140k-$175k", "Q4 2026", "Small creative studio build-out with a flexible client meeting room."),
            ("priya.patel@example.com", "Priya Patel", "Custom homes", "Ojai, CA", "won", False, "$420k+", "Planning phase", "Early planning for a compact, light-filled custom home."),
            ("ops@northshorefoods.example.com", "North Shore Foods", "Project management", "Ventura, CA", "new", False, "$60k-$90k", "Next 90 days", "Looking for owner-side coordination on a small restaurant refresh."),
        ]
        leads = {}
        for email, name, service, location, status, priority, budget, timeline, note in specs:
            lead, created = Lead.objects.get_or_create(
                email=email,
                defaults={
                    "name": name,
                    "service": service,
                    "location": location,
                    "status": status,
                    "priority": priority,
                    "budget": budget,
                    "timeline": timeline,
                    "source": "Website form" if email in {"maya.thompson@example.com", "ops@northshorefoods.example.com"} else "Referral",
                    "note": note,
                    "client": clients[email],
                    "created_by": staff_user,
                },
            )
            if lead.client_id is None:
                lead.client = clients[email]
                lead.save(update_fields=["client", "updated_at"])
            leads[email] = lead
        return leads

    def seed_estimates(self, leads, clients, staff_user):
        specs = [
            (1048, "maya.thompson@example.com", "Coastal Kitchen Renovation", "sent", Decimal("9720.00"), "A focused kitchen renovation with durable finishes, improved storage, and a calmer daily flow.", [("Cabinetry, demolition & prep", "1.00", "24000.00"), ("Electrical & plumbing", "1.00", "6800.00"), ("Surfaces & fixtures", "1.00", "9200.00"), ("Project management", "1.00", "8600.00")]),
            (1047, "hello@harborstudio.example.com", "Harbor Studio Build-out", "draft", Decimal("28000.00"), "A flexible creative studio build-out with a client meeting room and durable commercial finishes.", [("Selective demolition & prep", "1.00", "18000.00"), ("Partitions & finishes", "1.00", "52000.00"), ("Lighting & millwork", "1.00", "23000.00")]),
            (1042, "priya.patel@example.com", "Ojai Custom Home Planning", "accepted", Decimal("84400.00"), "Pre-construction planning and design-build management for a compact, light-filled custom home.", [("Pre-construction planning", "1.00", "42000.00"), ("Site coordination", "1.00", "188000.00"), ("Design-build management", "1.00", "105000.00")]),
        ]
        estimates = {}
        for number, email, title, status, deposit, notes, lines in specs:
            estimate, created = Estimate.objects.get_or_create(
                number=number,
                defaults={
                    "lead": leads[email],
                    "client": clients[email],
                    "title": title,
                    "status": status,
                    "deposit_amount": deposit,
                    "notes": notes,
                    "created_by": staff_user,
                    "sent_at": timezone.now() - timedelta(days=2) if status in {"sent", "accepted"} else None,
                    "accepted_at": timezone.now() - timedelta(days=1) if status == "accepted" else None,
                    "accepted_by": staff_user if status == "accepted" else None,
                },
            )
            if estimate.lead_id is None:
                estimate.lead = leads[email]
                estimate.client = clients[email]
                estimate.save(update_fields=["lead", "client", "updated_at"])
            if not estimate.line_items.exists():
                EstimateLineItem.objects.bulk_create([
                    EstimateLineItem(estimate=estimate, description=description, quantity=Decimal(quantity), unit_price=Decimal(price), sort_order=index)
                    for index, (description, quantity, price) in enumerate(lines, start=1)
                ])
            estimates[number] = estimate
        # Keep the presentation seed polished when upgrading an older local database.
        # Only exact legacy labels are changed, so custom staff edits remain untouched.
        EstimateLineItem.objects.filter(description="Cabinetry, demo & prep").update(description="Cabinetry, demolition & prep")
        EstimateLineItem.objects.filter(description="Selective demo & prep").update(description="Selective demolition & prep")
        return estimates

    def seed_projects(self, leads, clients, estimates, staff_user):
        specs = [
            ("kitchen", "Coastal Kitchen Renovation", "maya.thompson@example.com", 1048, "Ventura, CA", "renovation", "planning", "Review finish selections", "A calm, highly functional kitchen renovation shaped around durable materials and thoughtful storage.", "operations/images/progress-kitchen.png"),
            ("adu", "Ojai Custom Home Planning", "priya.patel@example.com", 1042, "Ojai, CA", "residential", "selections", "Finalize schematic set", "Early planning for a compact, light-filled custom home with a careful handoff from design into construction.", "operations/images/project-adu.png"),
            ("harbor", "Harbor Studio Build-out", "hello@harborstudio.example.com", 1047, "Santa Barbara, CA", "commercial", "construction", "Electrical rough-in", "A flexible studio build-out with a client meeting room, durable finishes, and a clear construction sequence.", "operations/images/project-bathroom.png"),
            ("bathroom", "Quiet Bathroom Retreat", "miguel.santos@example.com", None, "Oxnard, CA", "restoration", "complete", "Final walkthrough complete", "A warm, low-maintenance bathroom renovation finished with a considered material palette.", "operations/images/project-bathroom.png"),
        ]
        projects = {}
        for key, title, email, estimate_number, location, project_type, status, next_step, summary, fallback_image in specs:
            project, _ = Project.objects.get_or_create(
                title=title,
                defaults={
                    "estimate": estimates.get(estimate_number),
                    "lead": leads[email],
                    "client": clients[email],
                    "location": location,
                    "project_type": project_type,
                    "status": status,
                    "next_step": next_step,
                    "summary": summary,
                    "is_published": True,
                    "fallback_image": fallback_image,
                    "created_by": staff_user,
                },
            )
            changed = []
            for field, value in (("estimate", estimates.get(estimate_number)), ("lead", leads[email]), ("client", clients[email])):
                if getattr(project, f"{field}_id") is None and value is not None:
                    setattr(project, field, value)
                    changed.append(field)
            if changed:
                project.save(update_fields=changed + ["updated_at"])
            projects[key] = project
        return projects

    def seed_content(self, featured_project):
        settings, _ = SiteSettings.objects.get_or_create(pk=1)
        if settings.featured_project_id is None:
            settings.featured_project = featured_project
            settings.save(update_fields=["featured_project", "updated_at"])

    def seed_milestones(self, projects):
        milestone_states = {
            "kitchen": [True, True, False, False, False],
            "adu": [True, True, True, False, False],
            "harbor": [True, True, True, True, False],
            "bathroom": [True, True, True, True, True],
        }
        titles = ["Walkthrough", "Estimate approved", "Selections", "Construction", "Final walkthrough"]
        for key, project in projects.items():
            for index, title in enumerate(titles, start=1):
                Milestone.objects.get_or_create(
                    project=project,
                    sort_order=index,
                    defaults={"title": title, "is_complete": milestone_states[key][index - 1]},
                )

    def seed_updates(self, projects, staff_user):
        specs = [
            (projects["kitchen"], "Material selections are next.", "We’ve completed the walkthrough and are ready to review finishes with you.", "client"),
            (projects["kitchen"], "Cabinet layout confirmed.", "The revised layout is saved for the team’s internal coordination.", "internal"),
            (projects["adu"], "Schematic set is taking shape.", "The next review will focus on the final room relationships and natural light.", "client"),
            (projects["harbor"], "Electrical rough-in is underway.", "The field team is coordinating the rough-in sequence with the finish schedule.", "client"),
        ]
        for project, title, body, visibility in specs:
            ProjectUpdate.objects.get_or_create(
                project=project,
                title=title,
                defaults={"body": body, "visibility": visibility, "created_by": staff_user},
            )

    def seed_media(self, projects, staff_user):
        specs = [
            (projects["kitchen"], "Kitchen progress", "operations/images/progress-kitchen.png", "client", "The current kitchen progress view."),
            (projects["kitchen"], "Kitchen direction", "operations/images/hero-kitchen.png", "public", "A reference for the finished design direction."),
            (projects["adu"], "Warm wood entry", "operations/images/project-adu.png", "client", "Material direction for the entry sequence."),
            (projects["harbor"], "Studio materials", "operations/images/project-bathroom.png", "internal", "Internal material reference for the studio team."),
            (projects["bathroom"], "Finished retreat", "operations/images/project-bathroom.png", "public", "Final walkthrough reference."),
        ]
        for project, title, fallback_image, visibility, caption in specs:
            MediaAsset.objects.get_or_create(
                project=project,
                title=title,
                defaults={
                    "fallback_image": fallback_image,
                    "visibility": visibility,
                    "media_type": "photo",
                    "caption": caption,
                    "uploaded_by": staff_user,
                },
            )

    def seed_tasks(self, projects, leads, staff_user):
        if not staff_user:
            return
        specs = [
            ("Confirm finish selections", projects["kitchen"], leads["maya.thompson@example.com"], "in_progress", "high"),
            ("Prepare site walkthrough", projects["harbor"], leads["hello@harborstudio.example.com"], "open", "normal"),
        ]
        for title, project, lead, status, priority in specs:
            task, created = Task.objects.get_or_create(
                title=title,
                project=project,
                defaults={
                    "lead": lead,
                    "status": status,
                    "priority": priority,
                    "created_by": staff_user,
                },
            )
            if created:
                task.assigned_to.add(staff_user)

    def seed_activities(self, leads, estimates, projects, staff_user):
        specs = [
            ("New lead received", "Maya Thompson · Website form", leads["maya.thompson@example.com"], None, None),
            ("Estimate #1048 sent", "Coastal Kitchen Renovation", leads["maya.thompson@example.com"], estimates[1048], None),
            ("Project update added", "Coastal Kitchen Renovation · Client-visible", None, None, projects["kitchen"]),
            ("Estimate #1042 accepted", "Ojai Custom Home Planning", leads["priya.patel@example.com"], estimates[1042], projects["adu"]),
            ("Project media uploaded", "2 files · Coastal Kitchen Renovation", None, None, projects["kitchen"]),
        ]
        for message, detail, lead, estimate, project in specs:
            if not Activity.objects.filter(message=message, detail=detail).exists():
                Activity.objects.create(message=message, detail=detail, actor=staff_user, lead=lead, estimate=estimate, project=project)
