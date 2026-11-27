# Government website assurance

This is the Phase 7 verification plan and applicability record, **not a declaration of
Section 508 conformance or authorization to deploy**. It covers public documentation and the
local administration companion, including complete user journeys. The owner requested this
review on 8 October 2026.

## NCI design and branding

The owner supplied [NCI Digital Standards](https://www.cancer.gov/digital-standards) on
8 October 2026. They apply to this project's NCI website design, including the documentation
site and administration application. The earlier custom palette is superseded by
[NCIDS color tokens](https://designsystem.cancer.gov/foundations/color).

Use the [NCIDS components](https://designsystem.cancer.gov/components) and
[adoption guidance](https://designsystem.cancer.gov/get-started/maturity-model) for the header,
footer, skip navigation, typography and controls. Level 1 requires the header/footer/banner
appearance and correct skip navigation; higher levels progressively adopt typography, colors
and component code. We target consistent NCIDS presentation, but claim no assessed maturity
level. The owner will organize OCPL review during production rollout and relay any change
requests. That future review is not an implementation or Phase 7 release gate.

| Requirement | Implementation and verification in Phase 7 |
|---|---|
| Palette | NCIDS cerulean, neutral, teal and state tokens; measure actual foreground/background pairs after changes |
| Typography | NCIDS Poppins headings, Open Sans body and Roboto Mono code; locally supplied assets with provenance, no runtime font tracking; the faces live in one shared `ncids.css`, and the portal's content security policy allows only same-origin styles (`style-src 'self'`) |
| Header/navigation | Consistent identity, home link, current section, responsive navigation, functional documentation search and skip link |
| Footer policies | Disclaimer Policy, Accessibility, FOIA, HHS Vulnerability Disclosure, and NCIDS Privacy and Security link |
| Agency links | HHS, NIH, NCI, USA.gov, in that order and with agency names spelled out |
| Footer utilities | Relevant contact route and Back to top; no placeholder subscriptions or unrelated cancer-information controls |
| Government banner | Official domain/HTTPS statements only on an approved deployment where they are true; local previews stay visibly labelled as previews |
| Identity assets | Use published NCI identity assets; do not invent a blended logo. The owner handles production review separately |

[NCIDS header](https://designsystem.cancer.gov/components/header),
[NCIDS footer](https://designsystem.cancer.gov/components/footer),
[NCIDS typography](https://designsystem.cancer.gov/foundations/typography).
Component integration and repository-level public-site verification are release gates in #196;
UAT/PROD exposure remains disabled under #197.

## Accessibility baseline

The Revised Section 508 Standards incorporate WCAG 2.0 Level A and AA; our design target remains
WCAG 2.2 AA. A percentage score or automated scan does not establish conformance. Use the federal
ICT Testing Baseline to organize automated, manual and assistive-technology evidence.
[Section 508 applicability](https://www.section508.gov/develop/applicability-conformance/),
[federal testing guidance](https://www.section508.gov/buy/).

| Check | Evidence required in #196 |
|---|---|
| Structure | Page titles, language, landmarks, reading order, headings and table header associations |
| Keyboard/focus | Skip links, search, navigation, filters, forms, controls; visible ordered focus and no traps |
| Visual presentation | Measured contrast, 200% text enlargement, 400% zoom/reflow, mobile widths, no color-only status |
| Interaction | Accessible names/roles/values, instructions, errors, announcements and target sizes |
| Alternatives | Diagram descriptions, equivalent chart tables, meaningful links and accessible downloads |
| Resilience | Essential no-JavaScript reading, reduced motion, no flashing or unexpected timeouts |
| Assistive technology | Screen-reader navigation and form feedback across representative complete journeys |

The repository evidence below records the #196 checks and their limits. Unperformed checks
remain **not verified**, with procedures and responsible roles. Confirmed repository defects
are fixed on the active branch; these checks do not certify the complete deployed experience.

### Repository verification, 8 October 2026

The packaging checks ran on PR #206 head `30662ecd379c1013f9dfb02f3b76b7ce26c3e655`;
[CI run 37846044387](https://github.com/CBIIT/nci-si-mcp/actions/runs/37846044387)
passed both container jobs and all test/quality jobs. Both companion scans reported zero
findings at every severity on that run. Source at the start of #196 is milestone merge
`064a901`; the typography/navigation changes below are part of #196, whose PR records the
final checked head. Do not treat a past scan as a scan of later commits.

| Area | Method and observed result | Limits |
|---|---|---|
| Public reading | Strict Zensical build and link validation; browser navigation overview → topic → story; expandable case evidence; requirement X-17 opens its exact specification entry | Representative browser journeys; the generator tests every case assignment and requirement destination |
| Search | Browser keyboard activation, query and Escape dismissal of native Zensical search; desktop placement above page navigation, responsive header placement at narrow widths | Native browser/assistive-technology combinations still need the checks below |
| Structure | HTML tests and browser accessibility tree show page titles, English language, main/navigation regions, ordered headings, labelled forms, captions and column headers; tables have named keyboard-scroll regions | Accessibility-tree inspection is not a screen-reader test |
| Local controls | Browser submitted a fixture run, cancelled it and refreshed to Cancelled/exit 130; a second fixture benchmark completed with exit 0 and displayed its report; unknown provenance and sample limits stayed visible | Committed fixture workers, not live upstream performance; native select was opened with pointer input before keyboard selection |
| Configuration | Browser showed unavailable state before snapshot selection; a disposable MCP wrote a real safe startup snapshot; proposal preview showed timeout 30 → 23 and explicitly said nothing was applied; snapshot value/revision remained unchanged | Snapshot is not a liveness claim; no remote configuration or deployment changes |
| Navigation and typography | Regression tests pin home navigation, one current-section indication, local font bytes and a closed asset route; browser computed Open Sans body/Poppins heading styles | Font and licence digests checked against pinned source; no external font service |
| Responsive presentation | 320px-wide review frames showed wrapping public story/cards and responsive search; normal-width admin proposal had no horizontal page overflow | Not a substitute for native 200% text enlargement/400% zoom or device testing |
| Isolation and truthful evidence | Unit/integration/HTTP tests cover hash and inventory mismatches, historical story mapping, missing reports, interrupted jobs, atomic history, unknown metrics and finite worker/remote budgets; CI container probes confirm no fixture egress or serving mounts and shutdown independence | Local host-process isolation alone is not a network sandbox; actual UAT/PROD platform integration remains deferred |
| Public/private separation | Documentation allowlist/canary tests reject raw operational artifacts; asset routes expose only named fonts/licences; deployment blueprint keeps admin disabled | Public repository engineering CI remains public by owner decision |

Measured NCIDS foreground/background pairs using WCAG relative luminance: body `#1b1b1b`
on `#f0f0f0` **15.11:1**; links `#004971` on white **9.58:1**; white header/footer text
on `#00314b` **13.63:1**; table text on `#edf3f6` **15.38:1**; secondary text `#3d4551`
on `#f0f0f0` **8.50:1**; admin focus `#004971` on `#f0f0f0` **8.41:1**;
documentation focus `#ad4e00` on white **5.43:1**; documentation links `#01679d` on white
**6.13:1**. These are declared pairs, not an exhaustive scan of every rendered state.

### Automated browser regression journeys

The documentation CI job runs three repository-owned Playwright journeys in its pinned
Chromium build against the generated Zensical site and the real local dashboard HTTP server.
They cover desktop/narrow search activation and results, story-to-requirement navigation,
case filtering and contextual help, and native run/cancel forms. Requests to any origin other
than the two disposable loopback servers are blocked and fail the check. Missing browser
dependencies fail rather than skip the tests.

For a fresh checkout, after `pdm install`, run:

```bash
npm ci --prefix docs/site-assets --ignore-scripts
npm run build --prefix docs/site-assets
npm exec --prefix docs/site-assets -- playwright install chromium
pdm run docs-build
npm run test:browser --prefix docs/site-assets
```

Linux CI downloads locked Chromium into an initially absent, run-owned directory. It relies
on the hosted Ubuntu 24.04 libraries; future runner drift fails visibly and requires
re-evaluation. No apt installation, host-Chrome substitution or dependency-validation bypass
is used. Browser downloads remain a network dependency; this does not eliminate all
transient failures. The browser suite has a 90-second internal deadline inside a two-minute
step, with separate job headroom for teardown and diagnostic upload. SHA-bound diagnostics
contain only named identity, timing, install/test log and JUnit files; only the validated
same-SHA site can feed the companion images.
The identity record labels the installed full Chromium executable as inventory. The
supported `pw:browser` diagnostics in the named browser test log record the actual launch
executable; the unchanged default-headless journeys use the separately pinned headless shell.
No private browser-selection accessor or channel override is used.
The dashboard imports synthetic, validated evidence into a temporary store. The run/cancel
journey injects a controlled executor through the existing worker interface; it proves browser
form admission, navigation, cancellation feedback and absence of invented successful evidence.
The worker lifecycle and container checks separately verify real child-process termination and
isolation. These journeys are not live upstream tests, a full keyboard audit, a screen-reader
assessment or Section 508 certification. Temporary servers and stores are closed after the run;
failure diagnostics under `tmp/browser-results/` are local test artifacts.

### Remaining manual checks

| Check | Procedure | Responsible role |
|---|---|---|
| Native assistive technology | With VoiceOver/Safari and the deployment-supported screen reader/browser, complete search → story → requirement; results → filter; run → cancel; configuration → invalid/valid proposal. Check reading order, labels, table navigation, errors and focus return | Accessibility tester |
| Text enlargement and zoom | Enlarge text to 200%; at a 1280px viewport zoom to 400%. Complete the same journeys, including open menus and validation errors; check clipping, obscured focus and permitted two-dimensional table scrolling | Accessibility tester |
| Complete keyboard-only journey | Repeat every journey using Tab/Shift+Tab, Enter, Space, arrows and Escape, without pointer assistance; include native selects and search dialog focus return | Accessibility tester |
| All states and devices | Check native controls, error/disabled/hover/focus states, forced colors, reduced motion and supported mobile browsers; verify alternatives for every published diagram | Accessibility tester / documentation maintainer |
| Deployed service | Verify actual HTTPS/domain/header behavior, contact/policy applicability, platform admin access and retention/records decisions | NCI deployment, privacy and records roles |

The first four rows are explicit accessibility coverage limits, not claims that those checks
passed. They remain in the assurance record for the accessibility assessment. The final row
requires the real deployment; it does not require repository visibility changes or an
institutional login for local users. OCPL review remains owner-managed at production rollout.

### Privacy and storage inventory

Public documentation serves local assets and a browser-side search index. No analytics,
survey, account or runtime font CDN is added. External reference/policy links navigate only
when followed. Admin pages render without client scripting; forms submit to the same local
origin. Original run bundles and bounded queue history stay in the selected local evidence
directory/volume; filtered HTML uses `no-store`. Safe configuration snapshots are opt-in local
files and proposals are previews, not target writes. The public build never reads these stores.
Container diagnostics go to stdout; raw reports, request query strings and credentials are
not application diagnostics. Platform access logs and retention need a deployment-specific
review and are not configured by this repository.

### Historical preliminary dashboard checks (8 October 2026, before NCIDS alignment)

The #193 working implementation was checked using synthetic test evidence, not live acceptance
results. Browser accessibility-tree inspection found named form controls and captioned tables;
keyboard submission of a tool filter returned the expected subset. Wide tables gained named,
focusable scroll regions, with a regression test that failed before the fix. This is not a
screen-reader test or a completed keyboard/reflow audit.

The dashboard's declared foreground/background pairs were measured using WCAG relative luminance:
body text on white **11.66:1**, teal links on white **7.52:1**, table text on shaded cells **10.41:1**,
and focus outlines **5.43:1** on white / **4.85:1** on shaded cells. These measurements cover those
pairs only. The refined palette also measures teal on pale slate **6.99:1**, secondary text on
slate **5.84:1**, white on navy **12.71:1**, and amber status text **7.80:1**.
Browser-native controls, all states and the public documentation theme still require
the #196 review. Final evidence must identify the tested release commit.

## Federal and HHS applicability

HHS web policy covers digital modernization, accessibility, plain language, information collection,
records, branding, domains, disclaimers and privacy. Apply requirements to the actual service;
this local preview is not an agency principal website.
[HHS web policies](https://www.hhs.gov/digital/policies/web/index.html).

| Area | Repository checks | Deployment evidence/decision |
|---|---|---|
| Identity and links | About, policies, accessibility and external-reference notices; truthful build label | NCI approves branding, contacts and applicable links; official `.gov`/HTTPS statements must match the actual host |
| Privacy | Inventory storage, search, logs, forms, analytics and third-party requests; describe actual behavior | Privacy/platform roles assess notices, logging, analytics, retention and any collection approval; no tracking or surveys added by default |
| Usability | Responsive search/navigation, understandable stories/errors, current version/content labels | Confirm intended audiences, languages and public feedback arrangements |
| Security | Keep public docs and local functionality accessible; exclude operational results from public builds | Platform verifies HTTPS, domains, headers, vulnerability reporting and UAT/PROD admin protection |
| Records | Trace commits and document content ownership/artifact lifetime | Agency records role determines applicable schedules; 30-day CI retention is not a federal records policy |
| Governance | Record findings, methods and unresolved applicability | NCI web/accessibility/platform roles accept deployed readiness and required assessments |

Federal guidance distinguishes principal agency sites from secondary sites. Review About,
accessibility, privacy, external-link notices, FOIA, USA.gov and vulnerability reporting;
do not assume every principal-site obligation must be duplicated here.
[Required content and links](https://digital.gov/resources/required-web-content-and-links).

Use current OMB M-23-22 and 21st Century IDEA guidance; M-23-22 superseded M-17-06.
[Digital-first public experience](https://digital.gov/resources/delivering-digital-first-public-experience).

## Release disposition

#196 records the tested commit, methods, findings/fixes and remaining limitations. Deployment-only
evidence stays explicitly pending. #197 still blocks UAT/PROD admin exposure without approved
platform integration. This review does not change repository visibility or introduce a custom
identity service or institutional login for local users. Agency policy and production settings
remain owner/platform decisions.

Once Pages is enabled (#646), a GitHub Pages project site serves the same validated static
documentation that CI builds. That host is not an approved agency deployment: the government
banner's official-domain statements do not apply to it, and it has none of the companion
container's health route, custom headers or request logs. The deployed-service checks above stay
pending for an approved deployment.
