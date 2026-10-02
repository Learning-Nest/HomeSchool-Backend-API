from app.db import get_engine
from sqlalchemy import text

ADMIN_ID = "99c00f1d-cfc6-4313-9003-49d03a5e9a76"  # creator attribution; optional

TASKS = [
    # --- Must-fix blockers ---
    ("Replace static test OTP with real SMS delivery",
     "Replace the static test OTP (OTP_STATIC_TEST_CODE=482915) with real SMS delivery end to end. "
     "The config already refuses to let that flag exist when APP_ENV=prod, so this isn't a toggle to "
     "flip later on - it's an unfinished feature, not a shortcut.", "dev"),
    ("Register for TRAI DLT (SMS sender/template registration)",
     "India's mandatory sender-ID and template registration for SMS. No gateway - MSG91 included - can "
     "legally send OTP or transactional SMS in India without it; this is the real blocker on the SMS "
     "work, not which provider you pick.", "other"),
    ("Decide the dev/nonprod/prod environment story",
     "Only dev is deployed on Azure today; nonprod and prod are still Terraform placeholders, and all "
     "three mobile app flavors point at the same dev backend. Either stand up a genuine prod environment "
     "before launch, or deliberately collapse the environments and document that choice - don't let real "
     "user traffic land on a backend called \"dev\" by accident.", "dev"),
    ("Add brute-force protection on guardian OTP verification",
     "Lockout or backoff after a handful of wrong attempts, plus a short code expiry. A 6-digit code is "
     "only about 1,000,000 combinations.", "dev"),
    ("Fix GitHub OIDC federated credentials for infra/web/mobile-app repos",
     "Finish patching the GitHub OIDC federated credentials for the infra repo (and web/mobile-app once "
     "their pipelines run) for the same rename-triggered subject mismatch already fixed in backend-api.", "dev"),
    ("Push and deploy already-committed local fixes",
     "OTP error message, Switch profile button, baseline-checklist layout fix, build-number bump - so "
     "what's actually running matches what's been fixed.", "dev"),

    # --- Backend, infra ---
    ("Wire up monitoring and alerting (Azure Monitor / App Insights)",
     "For the Container Apps, the Postgres instance, and the release job - right now failures surface "
     "only when someone happens to check GitHub Actions or a tester reports a bug.", "dev"),
    ("Add error tracking (Sentry or equivalent)",
     "On both the API and the mobile app so crashes and exceptions are visible without waiting on "
     "tester reports.", "dev"),
    ("Verify Postgres automated backups and test a restore",
     "Verify automated backups on the Azure Database for PostgreSQL instance, and actually test a "
     "restore - a backup no one has restored from isn't a backup plan.", "dev"),
    ("Add rate limiting on all public-facing endpoints",
     "Not just OTP verification - also signup, login, and any search/list endpoint that could be "
     "scraped or abused.", "dev"),
    ("Rotate and audit secrets",
     "Confirm nothing sensitive (DB passwords, provider API keys) is sitting in Terraform state or "
     ".tfvars files outside Key Vault.", "dev"),
    ("Add data export and deletion endpoints for a family's data",
     "Needed both for basic good practice and for the data-subject rights described in the compliance "
     "items.", "dev"),
    ("Commission a security review / penetration test",
     "Before opening the app to the public - the guardian-verification flow and anything touching a "
     "child's data are the highest-value targets to check first.", "other"),
    ("Load-test the Container App and confirm autoscaling",
     "Confirm autoscaling rules actually trigger before a real traffic spike does.", "dev"),
    ("Write a rollback plan for prod releases",
     "If a prod release misbehaves, what's the actual sequence to revert the container image and/or "
     "the database migration?", "dev"),

    # --- Mobile app ---
    ("Complete Play Console app content declarations",
     "Content rating questionnaire, Target audience, Data safety form, Ads declaration, App access "
     "instructions, Privacy policy URL. An incomplete section here is almost certainly why the listing "
     "still shows \"(unreviewed)\".", "other"),
    ("Declare children's-audience status under Play Families policy",
     "Explicitly decide whether the app is \"designed for\" or \"appeals to\" children - the answer "
     "changes what ad SDKs and data collection are allowed, and opens up the Families/Teacher Approved "
     "program if it fits.", "other"),
    ("Meet Play's closed-testing requirement (12 testers / 14 days)",
     "Required for new personal developer accounts before applying for production access. Internal "
     "testing doesn't count toward this - budget the time for it.", "other"),
    ("Add crash and ANR reporting (Firebase Crashlytics or similar)",
     "Before opening up beyond the current small tester group.", "dev"),
    ("Test on a spread of real budget Android devices",
     "Not just one development phone - performance and layout bugs (like the recent "
     "one-character-per-line dialog) show up disproportionately on lower-end hardware common in India.", "dev"),
    ("Confirm the forced-update gate is server-config backed",
     "Confirm kAppVersion / isUpdateRequired in app_version.dart is actually backed by a server-side "
     "minimum-version config, and test that an old build really gets blocked.", "dev"),
    ("Produce real store listing assets",
     "App icon, feature graphic, phone screenshots, and a short promo description - currently still a "
     "placeholder listing.", "design"),
    ("Decide the production package/app identity",
     "in.homeschoolapp.homeschooling - the application ID can't be changed later without shipping as a "
     "new app and losing all reviews/install history.", "other"),

    # --- Legal, compliance ---
    ("Publish Privacy Policy and Terms of Service",
     "Hosted at a stable URL - required for the Play Console listing and as basic groundwork for "
     "everything else.", "other"),
    ("Review DPDPA 2023 children's-data provisions",
     "Verifiable parental/guardian consent before processing a child's personal data, a ban on "
     "behavioural tracking or targeted advertising to children, and data-retention limits once a "
     "purpose is served.", "other"),
    ("Register for TRAI DLT (legal requirement)",
     "Legal requirement before sending any OTP or transactional SMS in India - also listed in the "
     "must-fix blockers since it's the real SMS blocker.", "other"),
    ("Write a data retention and deletion policy for children's data",
     "Make sure the export/delete endpoints from the infrastructure items actually implement it.", "other"),
    ("Plan basic moderation for any user-generated content",
     "If forum posts or shared resources will be visible to other users, plan for basic moderation "
     "before launch - required both for trust and likely for Play policy compliance.", "other"),
    ("Decide account recovery when a guardian loses their phone",
     "Both a UX gap and a data-protection question - who else can prove they're the legitimate "
     "guardian?", "other"),

    # --- Quality assurance ---
    ("Build automated test coverage for critical paths",
     "Guardian verification/OTP, child-profile creation, and anything billing-related if a paid tier "
     "is added.", "dev"),
    ("Stand up a basic support channel",
     "An email address or in-app \"help\" link is enough at launch, but it needs to exist and be "
     "monitored before real families depend on it.", "other"),
    ("Write an incident-response plan",
     "Who gets paged if the API goes down or OTP delivery silently fails, and what the first three "
     "steps are.", "other"),
    ("Write a release checklist / runbook",
     "Covering the dev -> nonprod -> prod promotion path once the environment story is settled, "
     "including rollback steps.", "other"),
    ("Decide a versioning and release-cadence policy",
     "How often builds ship, what counts as a hotfix vs. a normal release.", "other"),

    # --- Feature completeness ---
    ("Offline-first content download",
     "Let a family download a chunk of lessons/skills over Wi-Fi and use them without a live "
     "connection - important for patchy mobile data areas.", "dev"),
    ("Add multi-language support (Hindi + regional)",
     "Starting with Hindi and whichever regional languages the first real user base needs - an "
     "English-only UI meaningfully narrows who this app can serve in India.", "dev"),
    ("Multi-guardian access with roles",
     "Both parents, a grandparent - rather than a single guardian phone number per child.", "dev"),
    ("Exportable/shareable progress reports",
     "PDF summary, printable certificate - useful on its own and doubles as documentation for NIOS or "
     "state open-schooling board registration.", "dev"),
    ("Broaden the content library across age levels/subjects",
     "Right now the skills/mastery content appears to cover a single level; a full-fledged app needs "
     "a real curriculum arc.", "content"),
    ("Add push notifications for reminders and milestones",
     "Gentle reminders and milestone celebrations - not currently present in the codebase.", "dev"),

    # --- Creative ideas ---
    ("NIOS/state-board curriculum alignment + exam docs",
     "With an option to generate the documentation a family needs if they later enroll their child for "
     "board exams - the single biggest differentiator for Indian homeschoolers, since \"not "
     "CBSE/affiliated\" is the top worry stopping parents from homeschooling.", "content"),
    ("Gamification: streaks, badges, rewards",
     "Aimed at the child's side of the app rather than the parent's.", "dev"),
    ("Parent-facing AI teaching assistant",
     "Answers \"how do I teach X\" or \"what should we cover this week\", grounded in the app's own "
     "curriculum - a natural fit given the structured skills/mastery data already in the schema.", "dev"),
    ("Moderated community space for parents",
     "High engagement value, but should wait until there's a moderation plan.", "other"),
    ("Weekly/daily planner view",
     "Turns the skill checklist into an actual day-by-day homeschool schedule.", "dev"),
    ("Integrate existing open content (NCERT e-books etc.)",
     "Rather than building every lesson from scratch.", "content"),
    ("Referral flow for word-of-mouth growth",
     "Once the app is stable - homeschooling parents tend to be tightly networked within local and "
     "online communities.", "dev"),
    ("Video/audio lesson formats for pre-reading-age children",
     "Current skill names (\"Sings simple songs\", \"Draws and colours freely\") suggest an "
     "early-childhood audience that benefits more from audio/video than text.", "content"),
]

rows = [
    {
        "title": title,
        "description": desc,
        "category": category,
        "position": i * 10,
        "admin_id": ADMIN_ID,
    }
    for i, (title, desc, category) in enumerate(TASKS)
]

engine = get_engine()
with engine.connect() as conn:
    result = conn.execute(
        text(
            """
            INSERT INTO scrum_tasks (title, description, category, status, position, created_by_member_id)
            VALUES (:title, :description, :category, 'backlog', :position, :admin_id)
            """
        ),
        rows,
    )
    conn.commit()
    print(f"Inserted {len(rows)} tasks")
