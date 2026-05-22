# White-Label Architecture Design

> Codebase: `./lms-main` (Frappe LMS, app id `frappe_lms`).
> Sibling AI app: `ai_mentor` (to be built — see [lms-analysis.md](./lms-analysis.md) §9).
> Goal: turn LMS into a **white-label, multi-tenant SaaS** with **zero hardcoding** and **all behaviour driven by config**, while preserving Frappe conventions and backward compatibility.
>
> **Scope of this doc**: design only. No code in `lms-main` is modified. Implementation plan is the next phase.

---

## 0. Guiding Principles

1. **Frappe-native first.** A Frappe site is already a tenant boundary (own DB, own Redis namespace, own files dir, own host). Don't reinvent isolation.
2. **Extend, don't fork.** All new behaviour ships in a sibling app (`ai_mentor` or a new `lms_whitelabel`); LMS stays pristine so we can pull upstream updates.
3. **One source of truth per tenant.** A single `Tenant Profile` Single doctype that owns brand identity, plan, feature flags, AI toggles. Everything else (frontend, emails, jobs) reads from it.
4. **Config beats code.** No string, asset, color, link or feature toggle that a tenant might want to change should live in source. Defaults stay in source; values live in DB.
5. **Backward compatibility.** Every config field has a sensible default that reproduces today's behaviour for existing single-tenant installs.
6. **Async-safe.** Tenant config must be readable from background jobs and schedulers (which run with site context but may not have a logged-in user).

---

## 1. Multi-Tenancy Model — Decision

**Chosen: Site-per-tenant (Frappe native).**

### Why

| Model | Pros | Cons | Verdict |
|---|---|---|---|
| **Site-per-tenant** | Hard data isolation (separate DB), built-in Frappe support (`bench new-site`, `dns_multitenant`), per-site config files, no query scoping needed, per-site backup/restore, per-site upgrade rollback. | More disk, more DBs, slower to provision (~30s per site), shared bench code path so an LMS upgrade hits all tenants at once. | ✅ **Pick this.** |
| Schema-per-tenant | Single DB, lower overhead. | Frappe doesn't support it natively — would require deep ORM changes. Migrations 10× harder. | ❌ Off-pattern. |
| Row-level (tenant_id everywhere) | One site, lowest overhead. | Have to add `tenant` link to **70 doctypes**, rewrite every query, fix permissions, fix reports, fix exports, fix `search_link` lookups. **Touches `lms-main` extensively.** Cross-tenant leaks become a constant risk. | ❌ Forces a fork. |

**Mapping: 1 customer = 1 Frappe site.**
- Tenant `acme` → site `acme.lms.example.com` (or custom CNAME `learn.acme.com`).
- Frappe matches `Host` header → site directory under `sites/` thanks to `dns_multitenant: true` in `common_site_config.json`.
- Each site has its own `Website Settings`, `LMS Settings`, `Tenant Profile` (new), users, courses, enrollments — **no field on any LMS doctype needs a `tenant` column.** Isolation is free.

### What this implies

- Tenant provisioning = `bench new-site …` + `bench --site … install-app frappe_lms ai_mentor` + seed `Tenant Profile` + DNS pointer.
- Tenant deletion = `bench drop-site` (one command, complete).
- Cross-tenant analytics (platform-wide) requires a separate **control-plane** site or an external warehouse — see §9.

---

## 2. Tenant Profile — Central Config Doctype

A new **Single** doctype `Tenant Profile`, lives in the new `lms_whitelabel` (or `ai_mentor`) app. Single = one row per site, exactly the pattern `LMS Settings` already uses.

### Field groups

```
Tenant Profile (Single)
├── Identity
│   ├── tenant_code            Data, unique, slug-safe (e.g. "acme")    [REQUIRED]
│   ├── legal_name             Data
│   ├── display_name           Data            (overrides Website Settings.app_name when set)
│   ├── tagline                Data
│   ├── primary_domain         Data, unique    (e.g. learn.acme.com)
│   ├── alias_domains          Small Text      (newline-separated)
│   ├── support_email          Data
│   ├── support_url            Data
│   └── timezone               Data            (default Asia/Kolkata if unset)
│
├── Branding (assets)
│   ├── logo_full              Attach Image    (header / login)
│   ├── logo_square            Attach Image    (avatar / favicon source)
│   ├── logo_dark_full         Attach Image    (rendered when data-theme=dark)
│   ├── logo_dark_square       Attach Image
│   ├── favicon                Attach Image    (overrides Website Settings.favicon)
│   ├── og_image               Attach Image    (social meta)
│   └── pwa_icon               Attach Image    (overrides PWA manifest icon)
│
├── Branding (theme)            (see §4)
│   ├── primary_color          Color           (#0E7159 default — matches existing LMSLogo green)
│   ├── primary_color_hover    Color           (auto-derive if blank)
│   ├── accent_color           Color
│   ├── neutral_palette        Select          ("gray" | "slate" | "stone" | "zinc")
│   ├── radius                 Select          ("sharp" | "default" | "soft")
│   ├── font_family_heading    Data            (CSS font stack)
│   ├── font_family_body       Data
│   └── theme_overrides_css    Code (CSS)      (escape hatch for raw CSS-vars; sanitized)
│
├── Subscription / Plan
│   ├── plan                   Link → Tenant Plan
│   ├── plan_seats             Int
│   ├── trial_until            Date
│   ├── billing_status         Select ("Trial" | "Active" | "Past Due" | "Suspended")
│   └── billing_email          Data
│
├── Footer & vendor links      (replaces hardcoded frappe.io links)
│   ├── docs_url               Data            (default: empty → hide button)
│   ├── marketing_url          Data            (default: empty → hide button)
│   ├── powered_by_label       Data            (e.g. "Powered by Acme Learn")
│   ├── powered_by_url         Data
│   └── show_powered_by        Check           (1 by default for free plan, 0 for white-label plans)
│
├── Email
│   ├── sender_name            Data
│   ├── sender_email           Data            (verified domain)
│   └── email_signature        Text Editor
│
├── Feature flags (tenant-level overrides)    (see §5)
│   ├── feature_overrides      JSON / child table   (key, enabled, value)
│   └── ai_mentor_enabled      Check
│
└── Internal
    ├── created_at             Datetime, RO
    ├── provisioned_by         Link → User, RO
    └── notes                  Long Text
```

Helper API (whitelisted, allow_guest=True):

```python
@frappe.whitelist(allow_guest=True)
def get_tenant_profile() -> dict:
    """Returns the resolved profile (Tenant Profile + Website Settings fallback + LMS Settings fallback).

    Frontend calls this once on boot. Backend code calls get_tenant() which is server-side cached."""
```

A backend module-level helper:
```python
# ai_mentor/whitelabel/tenant.py
def get_tenant() -> "TenantProfile":
    return frappe.get_cached_doc("Tenant Profile")
```

Cache eviction is automatic — `frappe.get_cached_doc` invalidates on `on_update`.

### Existing fields we **reuse**, do not duplicate

- `Website Settings.app_name`, `.favicon`, `.app_logo`, `.banner_image`, `.footer_logo` — Tenant Profile **inherits from these as fallbacks**. If Tenant Profile is unset, the LMS keeps working exactly as today.
- `LMS Settings.meta_description`, `.meta_image`, `.meta_keywords`, `.contact_us_email`, `.contact_us_url`, `.livecode_url`, `.payment_gateway`, `.default_currency` — kept as-is. Tenant Profile **does not** re-expose these.
- All LMS Settings feature flags (§5) — kept as-is.

This keeps the upgrade path painless and lets old installs adopt Tenant Profile incrementally.

---

## 3. Tenant Plan — Subscription Scaffold

Separate doctype `Tenant Plan` (not a Single — multiple plans defined at the platform level):

```
Tenant Plan
├── name                      e.g. "Starter", "Pro", "Enterprise"
├── max_users                 Int (0 = unlimited)
├── max_courses               Int
├── max_active_batches        Int
├── allow_custom_domain       Check
├── allow_white_label         Check     (hides "Powered by")
├── allow_ai_mentor           Check
├── allowed_features          Table → Tenant Plan Feature   (see §5)
├── monthly_price_usd         Currency
└── support_sla               Select
```

Plans are seeded once per bench via fixtures (`fixtures = [..., "Tenant Plan"]` in the new app's `hooks.py`). Tenant Profile links to a plan; runtime feature checks resolve `Plan ⇒ Tenant override ⇒ LMS Settings ⇒ default`.

---

## 4. Branding System — Config-Driven, No Build-Time Coupling

The only acceptable branding pipeline is one where **changing a logo or color requires zero rebuild and zero deploy** — just save the doctype.

### 4.1 Asset resolution

Single resolver function, both frontend and backend agree on the order:

```
favicon  = TenantProfile.favicon         ?? WebsiteSettings.favicon ?? "/assets/lms/frontend/favicon.png"
logo     = TenantProfile.logo_full       ?? WebsiteSettings.banner_image ?? <inline default svg>
square   = TenantProfile.logo_square     ?? WebsiteSettings.app_logo     ?? <inline default svg>
appName  = TenantProfile.display_name    ?? WebsiteSettings.app_name     ?? "Learning"
```

**Impact on the SPA:** [`session.js`](../lms-main/frontend/src/stores/session.js) currently calls `lms.lms.api.get_branding`. We **don't change that call** — instead, the `ai_mentor` app overrides it via `override_whitelisted_methods` in its hooks:

```python
# ai_mentor/hooks.py
override_whitelisted_methods = {
    "lms.lms.api.get_branding": "ai_mentor.whitelabel.api.get_branding",
}
```

The new `get_branding` returns the **same shape** as today's response (`app_name`, `app_logo`, `banner_image`, `footer_logo`, `favicon`) **plus** the new fields (`primary_color`, `theme_css`, `powered_by`, `docs_url`, `support_email`…). Existing components keep working; new components consume the new fields. **Zero LMS code changes.**

### 4.2 Theme / colors — runtime CSS-variable injection

`frappe-ui` already exposes design-token CSS variables (`--surface-*`, `--ink-*`, `--outline-*`). Today they're set by the bundled stylesheet and the only runtime switch is the `data-theme="light|dark"` attribute.

To add per-tenant colors **without rebuilding `frappe-ui`**:

1. On boot, the SPA fetches `get_branding` → `theme_css` is a precomputed CSS string:
   ```css
   :root,[data-theme="light"]{--brand-primary:#0E7159;--brand-primary-hover:#0a5945; …}
   [data-theme="dark"]{--brand-primary:#3DBE9A; …}
   ```
2. Backend computes `theme_css` from `Tenant Profile.primary_color` + `accent_color` + `neutral_palette` using a small palette generator (HSL ramp: 50–900) — server-side so the values are stable and cacheable.
3. SPA injects this CSS once via a `<style id="tenant-theme">` tag in `App.vue` mount. Subsequent profile saves push a realtime event → SPA re-fetches `get_branding` → swaps the style content.
4. `theme_overrides_css` (sanitized) is appended after the generated palette so power users can override anything.

**Tailwind classes don't change.** We don't touch `tailwind.config.js`. We bridge by adding a thin layer of CSS rules in `theme_css`:
```css
.bg-brand-primary{background-color:var(--brand-primary)}
.text-brand-primary{color:var(--brand-primary)}
```
…then any new white-label component uses `bg-brand-primary` / `text-brand-primary`. **Existing LMS components are not edited** — they keep using `bg-surface-gray-2`, `text-ink-gray-9`, etc., which already adapt to light/dark. White-label color customization is opt-in per component.

### 4.3 Custom domain handling

DNS layer (operator's responsibility):
- Tenant CNAMEs `learn.acme.com → platform.example.com`.
- Platform reverse proxy (Caddy/Traefik/nginx) forwards `Host:` header unchanged.
- Frappe with `dns_multitenant: true` reads `Host:` header and resolves to `sites/learn.acme.com/`.
- We provide a `bench --site` provisioning script that adds the host alias to the site config.

App layer:
- `Tenant Profile.primary_domain` and `alias_domains` are **informational + audit** — Frappe already resolves the site by hostname. We mirror them so the UI can show "your domains" without poking files.
- A `before_request` hook (already supported by Frappe) can verify that the resolved site matches the expected tenant — guards against DNS misroutes.

### 4.4 Email branding per tenant

Already 90% solved — `LMS Settings` links three Email Template fields. Two gaps:

1. **Sender identity**: today emails ship from the global `auto_email_id`. Add `Tenant Profile.sender_email` + `sender_name`. We override Frappe's `make_communication` sender resolution via a small wrapper: when `Tenant Profile.sender_email` is set and verified for the tenant's email domain, use it; otherwise fall back.
2. **Footer / signature injection**: register a single jinja filter `tenant_email_footer` and update the three fallback email bodies in [`EmailTemplateModal.vue`](../lms-main/frontend/src/components/Modals/EmailTemplateModal.vue#L52) to use a placeholder. *Slight caveat: those fallbacks are inside the `lms-main` SPA and only seed the field default when the user creates a new template — they're a UX concern, not a runtime concern. We can ship our own seeded templates via `fixtures` in the new app and skip patching the modal.*

---

## 5. Feature Flag System — Layered Resolution

Today's [`LMS Settings`](../lms-main/lms/lms/doctype/lms_settings/lms_settings.json) already provides the runtime flags. We add a thin **resolution layer** so flags can be plan-locked and tenant-overridden without changing existing call sites.

### 5.1 Resolution order

```
resolve(feature) =
    1. Tenant Plan.allowed_features[feature].forced_value      # platform-locked (e.g. AI requires Pro)
    2. Tenant Profile.feature_overrides[feature]               # tenant admin override
    3. LMS Settings.<feature>                                  # existing per-site flag
    4. compiled-in default                                     # safe fallback
```

`forced_value` from a plan is honoured even if a tenant tries to override it (e.g. a Starter plan tenant cannot enable `ai_mentor` by toggling LMS Settings). The plan check is enforced at the resolver, not at the UI — UI just hides the toggle.

### 5.2 API

```python
# ai_mentor/whitelabel/features.py
def is_enabled(feature: str) -> bool: ...
def get_value(feature: str, default=None): ...

# Frontend
const { isEnabled } = useFeatures()    // boot-injected reactive map
v-if="isEnabled('ai_mentor')"
```

The boot-injected map is added via `extend_bootinfo` (already used by LMS, [hooks.py:228-230](../lms-main/lms/hooks.py#L228-L230)) so the SPA gets the flags before the first paint — no flicker.

### 5.3 Net-new flags

| Flag | Purpose |
|---|---|
| `ai_mentor_enabled` | Master switch for the Eduru system. |
| `ai_mentor_post_quiz_panel` | Show post-quiz AI analysis. |
| `ai_mentor_lesson_chat` | Show "ask AI" sidebar in lesson page. |
| `ai_mentor_instructor_insights` | Topic-difficulty heatmap on Course Dashboard. |
| `ai_mentor_open_ended_grading_assist` | AI draft grade for open-ended quiz answers. |
| `white_label_strict` | Hide all "Powered by" / vendor links / docs links. |
| `white_label_custom_footer_html` | Tenant-supplied footer HTML (sanitized). |

All default to `0`. Plans seeded via fixtures decide which can be turned on.

---

## 6. Data Isolation

### 6.1 What's already safe

Site-per-tenant means **the database is the boundary**. As of today, **no** LMS doctype has a `tenant` field that could cross-link, **no** API endpoint reads from another site, and Frappe's session resolution already pins to `frappe.local.site`. There is nothing to "scope" — there's nothing that even *could* leak.

### 6.2 What we must not do

- Don't introduce a global config DB shared across tenants.
- Don't write to `frappe.cache()` keys that omit the site name (Frappe namespaces these automatically — keep using the framework cache, never a process-global dict).
- Don't ship singletons that hold per-tenant state in module-level globals (background workers reuse processes across sites).
- Don't hardcode site names in cron jobs — use `bench --site all schedule` or `bench --site <name>` and let Frappe iterate.

### 6.3 Control-plane data (the unavoidable cross-tenant slice)

The platform operator needs *some* cross-tenant view: list of tenants, plan, billing status, last login, MAUs. Pattern:

- Create a dedicated **control-plane site** (e.g. `admin.platform.example.com`, separate Frappe site).
- Each tenant site emits a heartbeat (POST `/api/method/control_plane.api.heartbeat`) on a daily scheduler. Payload: `tenant_code`, `version`, `mau`, `enrollments`, `plan`, last `Tenant Profile.modified`.
- Heartbeat is server-to-server with HMAC-signed JWT. Tenants do not share DB.
- Control-plane site stores `Tenant Registry` rows for the operator dashboard.

This keeps customer data inside the customer's site and only ships aggregate, non-PII metrics across the boundary.

---

## 7. Hardcoding Audit — Concrete Externalization List

Each row is a **change to be made in the new sibling app** (overriding via hooks) — none of these touch `lms-main` source.

| # | Today (file:line) | Today's hardcoded value | Lift to |
|---|---|---|---|
| 1 | [hooks.py:6](../lms-main/lms/hooks.py#L6) | `app_title = "Learning"` (desk apps screen) | Override via the new app's own `add_to_apps_screen` entry; keep LMS's as fallback. |
| 2 | [hooks.py:274-282](../lms-main/lms/hooks.py#L274-L282) | `add_to_apps_screen` title/logo/route | Same — new app contributes its own entry reading from `Tenant Profile`. |
| 3 | [_lms.py:21](../lms-main/lms/www/_lms.py#L21) | Default page title `"Frappe Learning"` | Already falls through `Website Settings.app_name`; ensure tenant provisioning seeds it. |
| 4 | [_lms.py:20](../lms-main/lms/www/_lms.py#L20) | Fallback favicon `/assets/lms/frontend/favicon.png` | Provision step writes `Website Settings.favicon` → fallback never fires. |
| 5 | [api.py:1749-1759](../lms-main/lms/lms/api.py#L1749-L1759) | PWA manifest title + icon fallbacks | Override `lms.lms.api.get_pwa_manifest` via `override_whitelisted_methods` to read from Tenant Profile. |
| 6 | [api.py:289-305](../lms-main/lms/lms/api.py#L289-L305) | `get_branding` returns Website Settings only | Override via `override_whitelisted_methods` (§4.1) — extends shape, keeps backward compat. |
| 7 | [session.js:41](../lms-main/frontend/src/stores/session.js#L41) | Fallback logo `/assets/lms/frontend/learning.svg` | Provisioning seeds `Tenant Profile.logo_square` (and `Website Settings.app_logo` for compat) → fallback never fires. |
| 8 | [UserDropdown.vue:20](../lms-main/frontend/src/components/Sidebar/UserDropdown.vue#L20) | Renders `<LMSLogo>` SVG when `branding.banner_image` missing | Same fix as #7 — banner_image always set post-provision. |
| 9 | [InstallPrompt.vue:4,32](../lms-main/frontend/src/components/InstallPrompt.vue) | Hardcoded "Install Frappe Learning" string | Replace via SPA-side override component (we ship a Vue component the new app injects with `web_include_js`) reading from boot-injected branding. *(Acceptable interim: leave as-is — it's only seen during PWA install, low-traffic surface.)* |
| 10 | [PersonaForm.vue:21](../lms-main/frontend/src/pages/PersonaForm.vue#L21) | "Frappe Learning" in onboarding text | Same as #9 — interim acceptable; final fix via translation override (`__('…')` is already i18n; ship a per-tenant translation file). |
| 11 | [AppSidebar.vue:205-210, 232, 238, 676](../lms-main/frontend/src/components/Sidebar/AppSidebar.vue) | "Powered by Frappe Learning" tooltip + icon, hardcoded HelpModal title and `docsLink="https://docs.frappe.io/learning"`, opens `frappe.io/learning` | Read from boot `tenant_profile.powered_by_*`, `tenant_profile.docs_url`. **For the *interim* we ship an `extend_bootinfo` injection that overrides these via a `MutationObserver` Vue plugin — ugly but does not fork LMS.** Long-term: upstream PR to LMS to read these from boot. |
| 12 | [Apps.vue:49](../lms-main/frontend/src/components/Sidebar/Apps.vue#L49) | Desk app tile logo `/assets/lms/images/desk.png` | Same as #2 — handled by `add_to_apps_screen` override. |
| 13 | [EmailTemplateModal.vue:52,68](../lms-main/frontend/src/components/Modals/EmailTemplateModal.vue) | Default email body string mentions "Frappe Learning" | Provisioning seeds proper Email Templates via fixtures; the modal default is just a placeholder for new templates. **Skip — not a runtime path.** |
| 14 | [Home.vue:91](../lms-main/frontend/src/pages/Home/Home.vue#L91) | Filter `'%A guide to Frappe Learning%'` to hide demo course | Internal demo-data filter. **Skip.** |
| 15 | [ProgrammingExerciseSubmission.vue:178](../lms-main/frontend/src/pages/ProgrammingExercises/ProgrammingExerciseSubmission.vue#L178) | `https://falcon.frappe.io` code-runner default | Backend already has `LMS Settings.livecode_url`; frontend should read it. **Add to `extend_bootinfo`.** |
| 16 | [frappe-ui-colors.json](../lms-main/frontend/src/utils/frappe-ui-colors.json) | Frozen color palette compiled into the bundle | Don't touch this file. Tenant primary color is layered on top via runtime CSS vars (§4.2). |
| 17 | `lms_lesson_page_extension` slot ([hooks.py:240-242](../lms-main/lms/hooks.py#L240-L242)) | Empty by default | Use this hook from the new app to inject the Eduru lesson chat — already-provided extension point. |

**Items 1–8, 11, 15** are runtime-visible to tenants. **Items 9–10, 13–14** are low-traffic / internal — defer (and propose upstream fixes once the design proves out).

---

## 8. Folder Structure for New Modules

We deliberately keep the white-label code and the Eduru code in **one new Frappe app** because they share the same `Tenant Profile` lifecycle. Directory layout:

```
apps/
└── ai_mentor/                                  # the new sibling Frappe app
    ├── ai_mentor/
    │   ├── __init__.py
    │   ├── hooks.py                            # doc_events, scheduler, override_whitelisted_methods
    │   ├── install.py                          # post-install: seed default Tenant Plans
    │   ├── patches.txt                         # idempotent migrations
    │   │
    │   ├── whitelabel/                         # tenant + branding subsystem
    │   │   ├── doctype/
    │   │   │   ├── tenant_profile/
    │   │   │   ├── tenant_plan/
    │   │   │   ├── tenant_plan_feature/
    │   │   │   ├── tenant_audit_log/
    │   │   │   └── tenant_feature_override/
    │   │   ├── api.py                          # get_branding (override), get_pwa_manifest (override),
    │   │   │                                   # get_tenant_profile, save_tenant_profile
    │   │   ├── tenant.py                       # get_tenant(), is_enabled(), get_value()
    │   │   ├── theme.py                        # primary_color → CSS palette generator
    │   │   ├── boot.py                         # extend_bootinfo contributor
    │   │   └── provisioning.py                 # bench command helper
    │   │
    │   ├── ai/                                 # Eduru subsystem
    │   │   ├── doctype/
    │   │   │   ├── ai_mentor_insight/
    │   │   │   ├── ai_question_tag/
    │   │   │   ├── ai_risk_score/
    │   │   │   ├── ai_conversation/
    │   │   │   └── ai_conversation_message/
    │   │   ├── pipelines/
    │   │   │   ├── post_quiz.py                # triggered from LMS Quiz Submission.after_insert
    │   │   │   ├── grade_assist.py             # LMS Assignment Submission.on_update
    │   │   │   ├── lesson_complete.py          # LMS Course Progress.after_insert
    │   │   │   ├── daily_risk.py               # scheduler daily
    │   │   │   └── weekly_summary.py           # scheduler weekly
    │   │   ├── prompts/                        # prompt templates (jinja)
    │   │   ├── llm.py                          # provider-neutral wrapper (Anthropic SDK + caching)
    │   │   ├── rag.py                          # course-content retrieval
    │   │   └── api.py                          # @whitelist endpoints consumed by the SPA
    │   │
    │   ├── analytics/                          # cross-cutting metrics layer
    │   │   ├── aggregations.py
    │   │   └── api.py
    │   │
    │   ├── public/                             # static assets (the new app's own logo defaults)
    │   ├── templates/                          # email templates seeded via fixtures
    │   ├── fixtures/                           # Tenant Plan, default Email Template, default sidebar
    │   └── www/                                # optional public marketing pages
    │
    └── pyproject.toml
```

**Frontend additions** ride along inside the same Frappe app:

```
apps/ai_mentor/ai_mentor/public/js/
└── ai_mentor.bundle.js          # mounted via web_include_js; small Vue island
                                 # contributes:
                                 #   - boot-time tenant theme injection
                                 #   - <AiInsightsPanel/> for QuizPage
                                 #   - <AiLessonChat/> for Lesson page
                                 #   - <PoweredByOverride/> sidebar slot
```

Mounting strategy: `ai_mentor.hooks.py` adds `web_include_js = ["ai_mentor.bundle.js"]`. The bundle, on load, looks for known DOM anchors (or registers Vue plugins on the `frappe.lms` global if exposed) and mounts panels into them. This pattern is the same one the LMS uses for its own bundle.

If the LMS does not expose a clean mount API, **fallback option** is to ship the Eduru as a separate `/ai` SPA page that opens in a slide-over from the LMS sidebar (registered through `LMS Sidebar Item` — already a doctype). This avoids any LMS-source patching at the cost of UX integration depth.

---

## 9. Step-Down: What Changes vs What Stays

### What stays untouched in `lms-main`
- All 70 LMS DocTypes.
- All 63 whitelisted endpoints in `lms.lms.api`.
- `lms.hooks.py` (we add to it via the *sibling* app's hooks; Frappe merges hooks across apps).
- `frontend/` Vue source (we don't fork the SPA).
- Cypress tests, fixtures, demo data.

### What's new (in the sibling app)
- `Tenant Profile` (Single), `Tenant Plan`, `Tenant Plan Feature`, `Tenant Feature Override`, `Tenant Audit Log`.
- `whitelabel/api.py` overrides for `get_branding` and `get_pwa_manifest`.
- `boot.py` extending bootinfo with `tenant_profile`, `feature_flags`, `theme_css`.
- `theme.py` palette generator.
- `ai_mentor.bundle.js` — small JS bundle that injects theme `<style>` tag and overrides "Powered by" / docs links.
- Eduru doctypes + pipelines (per [lms-analysis.md](./lms-analysis.md)).
- A `bench` command `bench provision-tenant <code> --domain <host> --plan <name>` that creates the site, installs both apps, seeds Tenant Profile.

### What lives in deployment / infra (not in any app)
- DNS / reverse-proxy mapping `*.lms.example.com` → bench.
- `common_site_config.json`: `dns_multitenant: true`, `default_site` (none), Redis cluster sizing, queue worker count.
- Backups per site (`bench --site … backup`).
- Operator control-plane site with `Tenant Registry`.

---

## 10. Open Questions (call these out before implementation)

1. **PWA per tenant**: Frappe's PWA manifest is served per-site, but the SPA's service worker is bundled with `lms-main` assets. A logo change updates the manifest immediately, but the SW cache may serve stale icons until SW update — acceptable, but document the cache-busting expectation.
2. **Email sender domains**: per-tenant `sender_email` requires SPF/DKIM configured on the operator's email backend. We can't enforce this from app code — it's an infra checklist.
3. **"Powered by" override path**: §7 row 11 is the ugliest piece of the whole design (DOM mutation from the side bundle). It's correct to ship it that way for v1, but the right long-term answer is a **PR to upstream LMS** that routes those strings through bootinfo. Track as tech-debt; do not let it block v1.
4. **Theme generator quality**: HSL ramp from a single `primary_color` produces serviceable but not pixel-perfect palettes. If a tenant cares deeply, they use `theme_overrides_css`. Alternative: ship 4–6 curated palettes instead of arbitrary color picker. Recommend the curated approach for v1.
5. **Tenant offboarding**: when a tenant churns, we want to keep their data backed up for X days. Pattern: `bench backup --site` → S3 cold storage → `bench drop-site`. Operationalize via runbook, not app code.

---

## Phase Status

- ✅ White-label architecture designed (this doc).
- ⏸ **Pending approval** before:
  - Multi-tenancy implementation plan (`bench` provisioning command, control-plane heartbeat, DNS runbook).
  - Required model changes (Tenant Profile / Plan / Feature DocType JSON).
  - UI customization implementation (theme generator, boot extension, side-bundle).
  - API design (the override surface for `get_branding`, `get_pwa_manifest`, plus the new `get_tenant_profile` endpoint).
  - Step-by-step migration plan for existing single-tenant installs.
