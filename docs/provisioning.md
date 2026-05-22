# Tenant provisioning runbook

> Consumes the design in [white-label-architecture.md](./white-label-architecture.md).
> Implements the model in the new sibling Frappe app at [`/ai_mentor/`](../ai_mentor/).

A tenant in this platform = one Frappe site. This runbook covers everything that has to happen between "we just sold a deal" and "the customer can log in to learn.acme.com".

---

## Prerequisites

- A working Frappe `bench` with `frappe`, `frappe_lms` and our new `ai_mentor` app available.
- `dns_multitenant: true` in `common_site_config.json`.
- Reverse proxy (Caddy / nginx / Traefik) terminating TLS and forwarding the `Host:` header unchanged to the bench.
- The FastAPI mentor backend (`backend/`) reachable from the bench network — needed only if the tenant has Eduru enabled.

---

## 1. Create the Frappe site

```bash
bench new-site learn.acme.com \
    --admin-password '<strong>' \
    --mariadb-root-password '<root>'

bench --site learn.acme.com install-app frappe_lms
bench --site learn.acme.com install-app ai_mentor
```

The `ai_mentor` install hook seeds three default `Tenant Plan` rows (`Starter`, `Pro`, `Enterprise`) and creates an empty `Tenant Profile` Single. No tenant identity yet — that's step 2.

---

## 2. Provision the tenant identity

```bash
bench --site learn.acme.com execute \
    ai_mentor.whitelabel.provisioning.provision \
    --kwargs '{
        "tenant_code": "acme",
        "primary_domain": "learn.acme.com",
        "display_name": "Acme Learn",
        "primary_color": "#FF6A00",
        "support_email": "support@acme.com",
        "plan": "Pro",
        "ai_backend_url": "http://ai-mentor:8000"
    }'
```

This:

1. Updates the `Tenant Profile` Single in this site.
2. Mirrors `display_name` / `favicon` / `logo_full` / `logo_square` into `Website Settings` so the LMS's hardcoded fallback paths in `lms-main` (`/assets/lms/frontend/learning.svg`, `"Frappe Learning"` strings) never get hit.
3. Marks the site as Trial unless overridden.

**The `bench execute` call writes only to the *current* site's database.** No cross-tenant leak is possible.

---

## 3. Brand assets

Brand assets are uploaded as ordinary Frappe Files, then linked from `Tenant Profile`. Either:

- **From the desk** (recommended): log in as `Administrator` → "Tenant Profile" → upload `logo_full`, `logo_square`, `favicon`, `pwa_icon`, `og_image`, set `primary_color`, save.
- **Programmatically** (CI / automation):

  ```python
  bench --site learn.acme.com console
  >>> import frappe
  >>> from frappe.core.doctype.file.file import create_new_file
  >>> # Upload via REST: POST /api/method/upload_file (multipart)
  >>> doc = frappe.get_doc("Tenant Profile", "Tenant Profile")
  >>> doc.logo_full = "/files/acme-logo.svg"
  >>> doc.save()
  ```

The side-bundle `/assets/ai_mentor/js/ai_mentor.bundle.js` will pick up the new `theme_css` and tenant fields on the next page reload — no rebuild.

---

## 4. Wire Eduru (Pro / Enterprise plans)

If `Tenant Profile.ai_mentor_enabled = 1`:

1. Set `ai_backend_url` on the profile (the FastAPI service URL).
2. Set `ai_backend_secret` — outbound bearer token Frappe sends on `POST {ai_backend_url}/lms/events/*`.
3. Set `ai_webhook_secret` — inbound bearer token the FastAPI service must send when calling `/api/method/ai_mentor.mentor.webhook.*` to write insights back. Use a different value than `ai_backend_secret`.
4. Configure the FastAPI service's per-tenant settings (out of scope here — see the backend's own README).

---

## 5. DNS + TLS

```text
learn.acme.com.   CNAME   platform.example.com.
```

Reverse proxy (Caddy example):

```caddyfile
*.platform.example.com, learn.acme.com {
    tls /path/to/acme.com.crt /path/to/acme.com.key
    reverse_proxy bench-host:8000 {
        header_up Host {host}
    }
}
```

Frappe matches `Host:` against the site directory name. The site directory MUST be named exactly `learn.acme.com`.

---

## 6. Verification

```bash
# branding endpoint returns tenant fields
curl -sS https://learn.acme.com/api/method/lms.lms.api.get_branding | jq '.message | {app_name, primary_color, theme_css: (.theme_css|length)}'
# → app_name: "Acme Learn", primary_color: "#FF6A00", theme_css: <int>

# tenant profile loads
curl -sS https://learn.acme.com/api/method/ai_mentor.whitelabel.api.get_tenant_profile | jq '.message.tenant_code'
# → "acme"

# feature flags resolved with plan + tenant + LMS Settings layered
curl -sS https://learn.acme.com/api/method/ai_mentor.whitelabel.api.get_feature_flags | jq '.message.ai_mentor_enabled'
# → true (because plan=Pro)
```

Open `https://learn.acme.com/lms` in a browser — the page title, favicon, sidebar logo, and primary buttons should all be Acme's.

---

## 7. Going live (white label hard mode)

If the customer has the `Pro` plan with `white_label_strict = 1`, the side-bundle:

- Hides the `Powered by Frappe Learning` icon entirely.
- Replaces the `https://docs.frappe.io/learning` link with `Tenant Profile.docs_url` (or hides it if blank).
- Replaces remaining `"Frappe Learning"` strings inside the sidebar's `data-component='AppSidebar'` and HelpModal scopes with `display_name`.

These DOM rewrites are scoped narrowly and stop after 30s — they exist as an intentional **interim measure**. The clean fix is an upstream PR to `lms-main` that sources those strings from boot. Tracked in [white-label-architecture.md §10](./white-label-architecture.md#10-open-questions-call-these-out-before-implementation).

---

## 8. Migrating an existing single-tenant install

For a site that's already running `frappe_lms` without `ai_mentor`:

```bash
bench --site existing.lms.com install-app ai_mentor
bench --site existing.lms.com migrate
bench --site existing.lms.com execute ai_mentor.whitelabel.provisioning.provision \
    --kwargs '{"tenant_code":"existing","primary_domain":"existing.lms.com","plan":"Starter"}'
```

After this:

- `Website Settings.app_name` is preserved (the provision step only writes if `display_name` is given).
- All existing courses, batches, enrollments, quiz submissions are unchanged.
- `Tenant Profile` is now the source of truth for new branding fields, but every legacy field still falls back to `Website Settings` / `LMS Settings`. Backward compatible by design.

---

## 9. Offboarding a tenant

```bash
bench --site learn.acme.com backup --with-files
aws s3 cp sites/learn.acme.com/private/backups/ s3://platform-cold-storage/acme/ --recursive
bench drop-site learn.acme.com
```

Update DNS to remove the CNAME. Done — the site, its DB and its files are gone from the live bench.

---

## 10. Operational notes

- **Cache**: `Tenant Profile` is read via `frappe.get_cached_doc`; saves invalidate automatically. After a `bench restart`, the first request rebuilds the cache.
- **Background workers**: hooks call `frappe.enqueue(...)` which preserves `frappe.local.site` — no need to thread tenant context through manually.
- **Webhook auth**: rotate `ai_webhook_secret` periodically; the bridge re-reads from Tenant Profile every call (cached).
- **Schema drift**: the new app declares `require_type_annotated_api_methods = True` in its hooks — keep it that way; it has caught a real bug class in upstream LMS.
