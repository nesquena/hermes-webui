# Installed apps directory

Apps is a dedicated navigation destination for the installation's creative,
productivity, automation and platform tools. An app opens in a separate tab and
retains its own sign-in and permissions. A configured link means its address is
configured; it does not claim that the service is healthy, signed in or available
under the current subscription. The directory does not fetch app pages or health
endpoints, proxy services, create sessions or activate modules.

## Deployment configuration

`SP_APP_LINKS` is a JSON object mapping audited app IDs to HTTPS URLs. Only IDs
in `api/app_catalog.py` are accepted. Names, descriptions, categories, module
associations and access classes come from that registry, never from the supplied
JSON or request parameters. An absent ID is not listed. An explicit `null` or
empty string keeps a disabled `unconfigured` entry visible. Invalid URLs and
wrong value types are omitted. An invalid top-level JSON value exposes no named
entries and does not break independently valid legacy links.

Example using documentation-only addresses:

```json
{
  "opendesign": "https://design.example.test/",
  "craft_studio": "https://studio.example.test/",
  "photocraft": "https://studio.example.test/photo/",
  "n8n": "https://workflows.example.test/",
  "hermes_docs": "https://office.example.test/docs/"
}
```

The curated registry includes OpenDesign, Craft Studio and all eight editors,
HyperFrames, n8n, OmniRoute, Nango, DocuSeal, RAGFlow,
Langfuse and five separate SynthPulse Office editors: Docs (Word), Sheets (Excel),
Slides (PowerPoint), PDF and Markdown. OpenWebUI, MSP Cockpit, personal dashboards,
and the classic design entry are omitted. Craft Studio and its editors share
Design & video with OpenDesign and HyperFrames. Every destination follows the
same trusted-host rule; there is no external desktop download exception.

SynthPulse Office destinations point to the independently deployed browser workspace
on the VPS. Each editor starts directly from its own tile. The workspace requires
an authenticated owner account with platform access, connects AI to OmniRoute,
and offers uploads and downloads for its document folder. It does not turn a
shared owner workspace into an isolated workspace per tenant. The Apps guide
explains these boundaries, file handling and each editor in Dutch and English.

The URL boundary is the existing launcher boundary: HTTPS without credentials,
query parameters or fragments; an installation-owned `SP_DOMAIN` or subdomain on
port 443, or its exact `SP_MESH_FQDN` with an explicit service port if needed.
The existing internal-component hostname denylist still applies. Use deployment
aliases or the exact configured private hostname, not new implicit trust for an
app brand. Request Host headers do not establish trust. App names being visible
in the directory do not relax hostname validation.

## Access and module behavior

### Shared organisation space

An operator can point department installations at one already configured app
using deployment-owned `SP_SHARED_APPS` JSON. Entries use a known app ID with
`org_id` equal to the local governance organisation, `scope` equal to
`organisation` or `departments`, a `departments` list of policy keys (empty for
organisation scope), and a credential-free HTTPS `url`. The exact destination
hostname must also be in the deployment-owned `SP_SHARED_APP_HOSTS` JSON list.
Only standard HTTPS port 443 is accepted, without query strings or fragments.

The launcher validates current organisation identity and department membership
on every read. Organisation/platform administrators can see their organisation's
shared entries; other users see only their selected departments or organisation
entries. Unknown users, tenants, departments, scopes and hosts fail closed.
Platform-only app restrictions and managed app availability continue to apply.
A dedicated customer Twenty CRM or RAGFlow workspace can be made discoverable
to its organisation or selected departments after the operator verifies tenant
isolation and individual app access. Set `customer_workspace: true` on that
specific shared binding. This is allowed only on an explicitly managed `client`
installation whose signed central organisation matches the local policy, if the
app is centrally enabled. A regular local app link, an unmarked shared entry,
an HQ installation or a shared admin console never acquires this permission.
Use a separate tenant destination, never the HQ CRM, knowledge base or owner
account. This is a launcher shortcut, not app authentication or data sharing.
A shared external app needs no second local module installation. The directory
does not activate local routes or modules. The UI labels these entries as the
**Gedeelde organisatieruimte / Shared organisation space**, with a workspace filter.

This directory controls discovery only. Before configuring an entry, provision
the central app inside the customer's organisation, configure and verify its own
authentication and permissions, and use individual accounts. Never reuse HQ
owner workspaces, credentials or data. Private networking and a visible launcher
link alone do not protect app data. There is no automatic conversion of existing
department apps into shared apps, and the directory adds no subscription.

Members see only member entries whose associated module is active. Creative
apps use `design_studio`; DocuSeal uses `e_signing` and SynthPulse Office uses `office_apps`. n8n requires platform scope
unless `SP_WORKFLOWS_CLIENT_EDITOR=1` explicitly permits member access; it still
requires the `workflows` module for members.

OmniRoute, Nango, RAGFlow, Langfuse and the SynthPulse Office editors are platform-only entries. Organisation,
department and billing administrators do not thereby gain platform access.

Platform administrators can discover all explicitly configured named apps,
even if the corresponding WebUI module is inactive. This is an inventory of
external installed applications, not module activation: module route gates,
entitlements and each destination's access checks remain unchanged. For example,
a platform administrator may see an existing DocuSeal address while
`/api/signing/` remains blocked because `e_signing` is inactive.

Authentication-disabled and anonymous requests are refused. Failure to resolve
identity and policy returns 503 without revealing app entries.

## API and compatibility

`GET /api/apps` returns `{ "apps": [...] }`. Every entry has:

- `id`: the audited app ID, or the legacy module ID.
- `module`: its existing module association.
- `name` and `description`: objects with `nl` and `en` text.
- `category`: `creative`, `productivity`, `automation` or `platform`.
- `access`: `member` or `platform` after applying the workflow editor opt-in.
- `url`: a validated HTTPS address, or `null`.
- `status`: `configured` or `unconfigured`.

`SP_MODULE_LINKS` retains its earlier active-module, workflow-scope and URL
validation behavior, including omission of empty links. These legacy entries
also carry the extended API fields. A configured named OpenDesign, n8n or
DocuSeal entry suppresses its corresponding legacy `design_studio`, `workflows`
or `e_signing` tile. An unconfigured or invalid named entry does not suppress a
working legacy link. Other Craft editors do not suppress the OpenDesign entry.

No new persistent state, permission, background process or outbound request is
introduced. Configuration remains owned by the deployment environment.

## Browser verification

The committed `scripts/e2e/server_fixture.py` seeds all 22 apps with synthetic
HTTPS addresses, separate platform,
organisation-admin and member identities. Browser networking remains restricted
to loopback. Run `scripts/e2e/run_full.py --engine <engine-checkout>` with
`--grep 'Apps directory|every app has|organisation admin modules|member module boundary|platform launcher'`
to exercise Apps and module navigation, role gates, NL/EN copy, viewport widths,
loading failures, restored filters and drafts, and every app spotlight. The
runner creates private test state and stops its fixture processes on exit.
