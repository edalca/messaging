# Messaging repository guide

## Scope and language

Messaging is a standalone Frappe v16 application with no required companion
Frappe apps. See [README.md](README.md) for the Bench and site requirements and
[pyproject.toml](pyproject.toml) for package dependencies. Read the owning
DocType or contract before changing an integration.

Use English for public documentation, comments, docstrings, developer-facing
messages, examples, engineering metadata, and new identifiers. Keep translation
catalogs, localized templates and resources, and language-specific test and
evaluation data in their target languages. Preserve public API names, import
paths, hooks, DocType and field names, identities, and serialized values when
editing text.

Use native Frappe translation lookup for interface copy. Python uses `_()`,
JavaScript uses `__()`, and Desk translates applicable metadata. Add or reuse
exact English keys in [es.csv](messaging/translations/es.csv), preserving contexts,
placeholders, and existing Spanish behavior. Display text can be an identity in
framework metadata. Check consumers and stored references before changing it.

## Modules and ownership

[modules.txt](messaging/modules.txt) lists the five Frappe modules. Messaging
declares `required_apps = []`. Business processes and external adapters belong
to the applications that implement them.

| Module | Ownership |
| --- | --- |
| [Communication](messaging/communication/) | Communication Providers and their operations, channels and accounts, Process Definition and the shared Process Runtime and execution log, neutral transport, notification rules, templates and queue |
| [Chatbot](messaging/chatbot/) | Bot Settings, Bot Parameters and their capture engine, Bot Flows and presets, Bot Flow Templates, Bot Dispatch and quotas, AI Provider, AI Configuration and evaluation documents |
| [Conversation](messaging/conversation/) | Chat Contact, Chat Session, Chat Turn and Chat Message, Chat Agent, conversation preferences, inbox, history and human support |
| [Emulator](messaging/emulator/) | EMULR adapter, fictional Emulator Contacts and Chat Emulator |
| [WhatsApp](messaging/whatsapp/) | Meta Cloud API adapter, webhook and administration |

The native [Dock](messaging/dock/messaging/messaging.json) orders Conversation,
Chatbot, and Communication. Each uses its declarative Sidebar. Conversation
provides Inbox, My Conversations, Conversation History, and Conversation
Supervision, with an Emulator section for Chat Emulator and Emulator Contact.
The `emulator-console` Page belongs to Emulator. Keep Child DocTypes in their
parent forms. Preserve native page routes and sidebar link identities.

Keep validation, administration, persistence, and document-specific operations
with the owning DocType. Cross-DocType consumers reuse those operations.
Functional module root files are internal unless they expose an explicit shared
boundary. Keep shared primitives in
`messaging.common`, authorization and scoped identities in `messaging.security`,
public declarations in `messaging.registry`, and lifecycle declarations in
`messaging.presets`. Avoid cycles, duplicate engines, and runtime dependencies on
preset declarations. Inspect a capability and its callers before adding another
implementation of permission checks, account resolution, locks, capture, language
selection, validation, retries, or delivery state.

## Code-first catalogs and lifecycle

There are three technical registry categories.

| Catalog | Public declaration module | Operational owner |
| --- | --- | --- |
| Process Definition | [messaging.registry.processes](messaging/registry/processes/) | [Process Definition](messaging/communication/doctype/process_definition/) |
| AI Provider | [messaging.registry.ai_providers](messaging/registry/ai_providers/) | [AI Provider](messaging/chatbot/doctype/ai_provider/) and [AI Configuration](messaging/chatbot/doctype/ai_configuration/) |
| Communication Provider | [messaging.registry.communication_providers](messaging/registry/communication_providers/) | [Communication Provider](messaging/communication/doctype/communication_provider/) |

An installed application declares capabilities in its own code. Its single
`messaging_registry` hook calls `messaging.registry.register_registry(app=...,
discover=True)` or supplies `definitions`. The registry verifies the installed
owner, function provenance, declaration signatures, and revisions. Conventional
discovery looks in the owner's `presets/` and `providers/` locations. Processes
provide a `process` declaration with distinct configuration and handler functions.
Provider modules supply a `definition()`. Registry operations require a System
Manager server context outside HTTP. Installed declaration lookup for providers
is read-only and scoped by capability.

Registration owns discovery and synchronization. DocType owners protect their
persisted projections. Use native transactions, locks, and savepoints. Identical
valid declarations preserve document versions and approvals. Lifecycle
synchronization isolates attributable declaration failures and diagnoses them
without exposing secrets or arbitrary exception text. An unresolved owning hook
protects the declarations that could not be resolved. Retired records remain for
history. Process Definition becomes Unavailable, Communication Provider requires
a matching synchronized fingerprint, and unavailable internal AI Providers block
dependent profiles. Runtime resolves current verified implementations.

[setup.py](messaging/setup.py) and [hooks.py](messaging/hooks.py) define the lifecycle.

1. **Install.** `after_install` prepares roles and the service identity, calls
   `synchronize_registry()`, and provisions the native Bot Flow Presets and AI
   Evaluation catalogs. The native technical declarations include EMULR, WhatsApp and the
   three processes `messaging.registration`, `messaging.greeting`, and
   `messaging.human_availability`. The installed providers and business
   processes of other apps come from their own hooks.
2. **Migrate.** `after_migrate` calls only `synchronize_registry()`. This also
   checks the technical dependencies of AI evaluation evidence. It leaves
   preset and rule provisioning, AI Configuration, and Bot Settings to their
   respective owners. Ordinary hooks leave transaction control to Frappe.
3. **Provision documents explicitly.** An owning app can add or update native
   preset, rule, template, or evaluation documents at installation or through
   a scoped patch. These use document-owner operations. There is no
   `messaging_presets` hook or additional registry category.
4. **Configure in Desk.** Administrators create and enable channels and accounts,
   enter credentials, configure languages and messages, install or create
   flows, and validate consumers. Setup leaves Bot Settings unconfigured and
   creates no active flows, channels, accounts, or notification queue entries.

When a Process revision or availability changes, Bot Flow and Notification Rule
invalidate affected receipts and disable the consumers while preserving
configuration and history. Changes to AI capabilities, configuration, or
implementation are checked against effective configuration and evaluation
dependencies. Synchronization performs no provider calls or business execution.
Preserve schema identities and historical evidence. Use scoped, explicit changes
for document defaults and avoid destructive schema work in lifecycle hooks.

### Document provisioning

- [Bot Flow Presets](messaging/chatbot/doctype/bot_flow_presets/) owns
  `provision_presets(app=..., declarations=...)` and `save_preset`. Contributors
  use `add_language` in
  [Bot Flow Preset Language](messaging/chatbot/doctype/bot_flow_preset_language/).
  A preset has a stable key, owner, Process Definition, common configuration,
  and revision. A language contribution can come from a separate installed app,
  and has its own immutable owner, revision, and availability. Provisioning
  verifies ownership and persisted Process metadata, uses a lock and savepoint,
  and updates the explicitly selected contributions. Omission does not retire
  them. Localized settings can supply prompts and Template content. Message
  outcomes use the Process message.
- [Bot Settings](messaging/chatbot/doctype/bot_settings/) is the administrative
  entry point for applying and restoring flow presets. Its dialog reads persisted
  catalog data and shows common and language changes and manual edits.
  `apply_flow_presets(keys, reset_keys=None, expected_versions=None)` delegates
  to `bot_flow.install_preset` with version checks. Installation needs complete
  content for each enabled language and creates disabled, unapproved flows.
  Restore requires confirmation for affected records and invalidates approval.
  Protected `preset_versions` tracks common and per-language hashes and applied
  snapshots. Preserve unaffected languages and related documents. When replacing
  content would overwrite shared Bot Parameters or templates, create the
  replacement through the relevant owner and retain the earlier document.
  Registration, greeting, and human flow selections remain explicit.
- [Notification Rule](messaging/communication/doctype/notification_rule/) owns
  `save_rule`. Its registered snapshots and hash distinguish declaration
  updates from administrative edits. Apply compatible declared changes to
  unmodified rules and require renewed approval. Divergence returns
  `administrative_changes`. Use `save_registered_template` on Communication
  Template for initial content. A declaration without a channel can supply
  content for later materialization. Send remains a decision on each outcome.
- [AI Evaluation Case Template](messaging/chatbot/doctype/ai_evaluation_case_template/)
  owns `synchronization.register_evaluations(app=..., evaluations=...)`.
  AI Evaluation Case owns case materialization and expectations. Registered
  scenarios and language variants retain their owner, identity, and revisions.
  Custom scenarios have separate identities. Provisioning never recreates or
  reinterprets evaluation run history.

## Process contract and approvals

`messaging.registry.processes` exports `BotProcess`, `NotifyProcess`,
their typed builders, requests and responses, and reference types.
`messaging.registry.processes.register_processes` exposes direct owner
registration. A `definition(*, params, outcomes)` declares the contract, and
the separately annotated handler consumes a single immutable request. Registry
discovery calls the definition with guarded reads and side effects restricted.
The Process Runtime alone invokes the business handler.

```python
from messaging.registry.processes import (
    BotParams,
    BotProcess,
    BotRequest,
    BotResponse,
    ProcessOutcomes,
)


def definition(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
    params.register("reference", type="string", required=True)
    outcomes.register("resolved", fields={"detail": "string"},
                      mode=["message"], action="complete_flow")


def handle(request: BotRequest) -> BotResponse:
    return BotResponse("resolved", data={"detail": "Reference received."},
                       message="Reference received.")


process = BotProcess(id="example.reference", label="Reference lookup",
                     definition=definition, handler=handle)
```

- `params.register` declares immutable inputs of type `string`, `integer`,
  `number`, `boolean`, or `date`. Preserve exact Decimal values, ISO dates, false
  and zero, and immutable tuples for multiple inputs. `register_configurable()`
  permits additional inputs bound by the consumer. `request.params` offers
  declared and configurable views and a metadata lookup by key.
- `outcomes.register` defines the status, exact fields and types, allowed
  `message` or `template` modes, and the Bot Flow action. The first mode is the
  default. The actions are `complete_flow`, `cancel_flow`, and
  `retry_parameter`. `configurable=True` explicitly projects compatible
  configurable input fields to the output contract. The handler still supplies
  the result data. Notification outcomes carry no conversational action.
- `BotRequest` provides `params`, `contact`, `language`, `channel`, `account`,
  `session`, and `message`. `NotifyRequest` provides `params`, `language`,
  `source`, `event`, `channel`, and `account`. References keep native
  permission checks on access. `NotifyNoResponse` completes without a
  functional result or queue entry.
- Process Schema v1 requires exactly `version`, `status`, `recipient`, `data`,
  and `message`. `version` is the integer `1` and `status` is a nonempty,
  declared functional outcome. `recipient` and `message` can be empty strings.
  `data` can be empty and each supplied scalar value must match its declaration.
  Consumers require the fields their selected action or template uses.
  Notification extracts a supplied, declared recipient for delivery. A rule
  controls the account, destination policy, and template.

[execution.py](messaging/communication/doctype/process_definition/execution.py)
owns `execute_process`. It reloads the persisted consumer, resolves the
available definition of the correct type, checks invocation authorization,
approval, deployed sources, signature and context, then invokes the verified
handler. Arbitrary client-provided Python paths are outside this contract. The
runtime enforces a 30-second thread-isolated deadline (Python bytecode
monitoring, MariaDB per-session statement and lock timeouts, and bounded
`time.sleep` without process-global `SIGALRM` mutation, compatible with `sync`,
`gthread`, and RQ workers), a savepoint, and redacted evidence in Process
Execution Log. Writes require an exact operational proof and use native
document persistence inside the executor's transaction. Direct SQL, hidden
commits, and external side effects are outside the callback contract. Static
inspection also rejects UI output from Process code. Opaque native C extensions
that neither execute Python bytecode, query MariaDB, nor call `time.sleep`
cannot be forcibly interrupted in-thread; external I/O remains blocked inside
Process handlers and bounded by subprocesses where permitted (discovery and
external AI REST transport).

The handler owns business document selection, operations, validation, and
response data. Messaging adds no business document or field permission checks
to native server-side APIs used by the handler. The runtime loads the source of
an authorized document event without requiring business document read permission
from the service identity. Frappe applies the permission
behavior of the selected API, including explicit `frappe.has_permission()` and
`doc.check_permission()` calls. The service identity needs only the permissions
required by Messaging itself and by the operations that the handler chooses.
Use `decimal_rows` for exact decimal reads from handler-selected records.
Process Definition preserves both `definition_method` and `method`, the
registered contract and source snapshot, and the contract and capability
revisions. Treat document recreation as a new approval identity. The explicit
validation and activation paths check deployed source and loaded code. Opening
a consumer or projecting its contract reads the persisted definition.

Desk projects registered contracts into configuration. A Bot Flow binds a
single available `bot_flow` Process Definition, unique across flows. A
Notification Rule binds a `notification` definition. Persisted definitions
supply derived routes and typed input and output metadata. Saving checks
document integrity. The shared
[approval boundary](messaging/communication/doctype/process_definition/approval.py)
owns the read-only `status` of both consumers. New documents start `Untested`.
Validate checks the saved contract and issues a receipt in `Disabled`. Enable
requires the current receipt and operational dependencies. Disable keeps the
receipt. The registered capability revision identifies the Process contract and
implementation. The execution runtime tracks its deployment manifest independently.
A change to the registered revision or availability invalidates only consumers
with an affected receipt and records the cause under `Unavailable`. Material
consumer changes also withdraw approval. Validate returns the document to
`Disabled`. The exclusive validation action checks the stored conditions and
bindings without invoking a business handler. Keep historical logs immutable.
Bot Flow approval excludes presentation text. Notification Rule delivery
approval includes recipient policy and the published template revision. Delay
changes leave the receipt and previously prepared queue entries intact.

## Bot configuration and localization

- [Bot Settings](messaging/chatbot/doctype/bot_settings/) is the Single for
  languages, global messages, interpretation, capture attempts, registration,
  greeting, human support, and quotas. The native Bot Language child table
  selects enabled Frappe Language records and exactly one default once
  configured. Required messages must be present in the default language.
  `bot_settings.language` owns `configured_languages`,
  `resolve_process_language`, and scoped presentation language. Conversation
  determines a message's language before registration or intent processing. It
  preserves that language on the turn for recovery. Local detection is
  conservative. It considers enabled languages and falls back through the
  conversation context, an enabled contact preference, and the configured default.
- [Bot Flow](messaging/chatbot/doctype/bot_flow/) owns capture relationships,
  localized input and menu text, functional outcomes, and validation. Priority
  zero is for flows with no parameters. Flows with parameters have unique
  positive priorities. An enabled flow can participate in natural interpretation
  even when hidden from the menu, subject to the catalog's eligibility rules.
  `execution.invoke` prepares captured arguments and maps the validated
  Process result to the configured output and action. Message uses the exact
  nonempty Process message. Template references a localized Bot Flow Template
  through Flow Output Translation. Default language fallback is a presentation
  choice and preserves the Process request language. Content from an
  administrator stays in the document and is outside UI string translation.
- [Bot Parameter](messaging/chatbot/doctype/bot_parameter/) is the shared
  parameter catalog. Its [parameters package](messaging/chatbot/doctype/bot_parameter/parameters/)
  owns the registered handlers for String, Int, Decimal, Boolean, Date, Name,
  and Expression, and the shared validation, recognition, and capture engine.
  Prompt translations belong to `Bot Parameter.request_phrases`. A consumer
  configures order, requiredness, multiplicity, and cardinality through its
  Flow Parameter relationship, respecting the Process input contract. Preserve
  referenced parameter identity and validate changes against consumers.
- Capture attributes unambiguous values locally and uses individual prompts for
  what remains pending. Expression uses the native `#` digit and `@` letter
  syntax, explicit escapes, and one pattern per line. Preserve original spelling
  and leading zeros and reject invalid composites atomically. Date captures
  DD/MM/YYYY and passes ISO dates internally. Local Name recognition preserves
  Unicode spans. Boolean recognizes complete responses in its supported
  languages. Decimal keeps exact values and language-aware directed capture.
  Optional parameters never authorize a partial invalid set. AI classifies
  intent and leaves capture and value validation to the local engine.
- Retry Parameter clears the affected value or list and preserves other
  validated arguments. Invalid capture and functional retry share the per-request
  attempt counter, counted at most once per turn. Missing values and transport
  or technical failures have separate handling. A completed or cancelled flow
  resolves its part of the request. The remaining queue and explicit recovery
  determine when the next independent request can start.
- Registration, greeting, and human support select ordinary, approved flows and
  allowed Complete Flow outcomes in Bot Settings. `flow_consumers` validates
  selections and projects declared fields. `Bot Flow State` and the
  `Flow Consumer State` child provide the native Link selection. Disabled
  functions retain configuration. Required registration runs before menus,
  interpretation, and human support. Successful registration merges the selected
  fields into the protected contact profile and records the evidence before
  resuming the original message. Greeting uses its configured flow response.
  The optional greeting menu and human transfer are decisions of the consumer.

The mechanical path uses a numbered menu with a scope-bound, accepted Bot
Dispatch snapshot. Rechecking eligibility and consuming valid selections prevents
a stale menu from running a flow. With interpretation enabled, a new request can
classify natural intent and reuse the menu composer for offers or clarification.
Capture, registration, human handling, and recovery preserve precedence.
An in-flight request retains its mode and captured values until resolved.

## Communication accounts and transport

`Communication Provider → Communication Channel → Communication Channel Account`
is the only channel/account model. The provider owns capabilities, callback
operations, and parameter definitions. A channel links to its provider and
stores channel configuration. An account links to its channel and stores account
configuration. The account's `account_id` is globally unique and supplies its
document name. The channel/account key is also protected by uniqueness. Preserve
account and channel identities and historical links.

The Communication Provider controller owns availability and the protected
administrative `status` field. Its `set_provider_enabled` action requires POST,
System Manager, and document access. Communication Channel owns its own
`status` and updates it via `set_channel_status` with the same requirements.
A usable binding requires current installed code, availability, an Enabled
provider and channel, an enabled account, and valid settings.
`single_channel` counts existing channels, including disabled ones.
`fixed_channel_id` controls the first channel name when supplied. The common
channel controller enforces both with native validation and locks.

Keep each Communication Provider's declared revision in its own `version.py`
within the provider module. The declaration reads that file for
`CommunicationProvider.version`. EMULR and WhatsApp use
[`messaging/emulator/version.py`](messaging/emulator/version.py) and
[`messaging/whatsapp/version.py`](messaging/whatsapp/version.py), respectively.
The Messaging application release version is independent of provider revisions.
During development, use release candidate versions. Messaging and its bundled
providers will start the stable series at `16.0.0`. After that baseline,
advance each provider's version independently when its own changes warrant it.

Provider parameters use the exact scopes Channel, Account, Template, and
Notification Rule. They use the supported Data, Small Text, Password, Check,
Int, Float, Select, and Link types. The parent stores a child
`Communication Provider Value` per key. Regular values use typed JSON scalars.
Password values use Frappe's encrypted Password storage. Forms use the scoped
schema API, and server validation checks the current provider and document.
Only authorize access to decrypted values in the exact account or registered
callback scope. Secrets belong outside ordinary JSON, logs, requests to Desk,
or command examples.

Installed adapters register `CommunicationProvider` and
`CommunicationOperations` with verified owner functions. See the native
[EMULR declaration](messaging/presets/communication_providers/emulr.py) for the
shape of a declaration and use the typed contracts for supported capabilities.
In particular:

1. Implement `resolve_account` and `send` for the actual transport. Implement
   `authenticate_ingress` and declare the minimal `ingress_parameters` for
   incoming events and reports. The adapter owns its protocol, authentication,
   and event normalization.
2. Pass the original protocol request and candidate provider/account to the
   [Communication Channel Account controller](messaging/communication/doctype/communication_channel_account/communication_channel_account.py)
   `dispatch_ingress(provider, account, request)`. The registered verifier
   authenticates the request for the exact binding and returns the closed
   `action` and `payload` descriptor. This is a private server boundary.
3. `receive` carries `event`. `report_result` carries `dispatch_name`,
   `result`, and `source_type`. `report_status` carries `dispatch_name`,
   `status`, `reference`, and `source_type`. The dispatch source is Bot Dispatch
   or Notification Queue. Chat Turn ingress validates the normalized event.
   Provider IDs, account IDs, and caller-controlled flags convey no permission.
4. The [Communication Provider controller](messaging/communication/doctype/communication_provider/communication_provider.py)
   owns neutral handoff, result validation, and delivery progression. The
   Communication Channel Account controller routes authenticated reports.
   Use `outbound_id` as the idempotency key. Accepted requires a reference.
   Rejected is definitive. Unknown needs reconciliation. The delivery progression is
   `Pending < Sent < Failed < Delivered < Read`, subject to exact reference and
   source verification. Acceptance and delivery are separate facts.
5. Bot Dispatch and Notification Queue own their own durable state, handoff
   marker, and reconciliation. A request timeout after handoff never authorizes
   a resend. Provider templates, template status, required redirection, and
   notification validation are explicit capabilities and operations. Consult the
   declaration and current owner before implementing them.

Authorized administrators generate the webhook verify token explicitly on a
saved WhatsApp channel. A channel shares that token across the associated Meta
numbers. Administrators use the channel's **Webhook Actions** menu to copy the
callback URL or verification token, generate the first token, or confirm rotation.
Enabling the channel validates the app configuration and the public HTTPS
callback origin. Each WhatsApp account stores one immutable Phone Number ID
and can share its Messaging Account ID with other numbers. Its enable action
requires a successful lookup of that exact number and a current configuration
snapshot. The last confirmed display number is read-only. The account persists
the latest sanitized connection, subscription and signed message webhook results
independently. Desk reads saved evidence and marks it historical when the
relevant configuration changes. **WhatsApp Actions** runs the explicit Graph
GET for the configured Messaging Account's subscribed apps. Meta Developers
owns webhook subscription management. Disabling a number only changes local status.
The Meta App Secret authenticates signed POST events. The native WhatsApp
declaration uses the Phone Number ID as the Messaging
account ID and binds signed inbound events to both the channel and WABA.
Delivery reports correlate through Meta's opaque callback data or an existing
transport reference and update the originating Bot Dispatch or Notification
Queue. Signed template notifications schedule a scoped reread of Meta's current
status. Communication Template owns the persisted readings. The optional
`mark_incoming_read` operation receives evidence from the bot processing
boundary or the assigned agent's native read cursor.

Communication Channel owns its public organization details and three legal
drafts. Explicit publication snapshots only the reviewed public fields and
sanitized body. Draft changes and operational channel status preserve published
content. The Website renderer resolves its three slugs by exact immutable
channel ID. Generate Desk URLs with the configured public HTTPS origin shared
with the WhatsApp callback. See [README.md](README.md) for the publishing steps.

[security](messaging/security/) owns the common service identity, authorization,
and scoped context restoration. Setup provision and read-only runtime
verification are separate operations. Automation uses the dedicated account
without Desk, administrator credentials or general document CRUD. An installed
consumer remains responsible for its native business permissions. Preserve the
original user for human operations and scope any automatic action to its verified
account, job or Process. Never expose private elevation helpers through REST.

## Conversation and human support

The canonical conversation identity is `(channel, account, sender)`. Chat Contact
preserves the cross-episode profile. Chat Session handles episode state, capture,
queueing, transfer and closure. Chat Turn persists receipt and processing
evidence. Chat Message owns visible content, sequence and origin labels.
Use the existing contact locks, durable checkpoints, and unique keys.

- [Chat Turn inbox](messaging/conversation/doctype/chat_turn/inbox.py) durably
  accepts and deduplicates an authenticated event. It schedules the contact
  after commit. Receipt order is independent of provider event time. A worker
  drains in receipt order and reuses the original turn and message.
  `chat_turn.capture_pending` can absorb contiguous compatible fragments
  through the shared capture engine. Assigned work and in-flight arguments
  remain stable.
- Lock and transaction ownership sit at the execution boundaries. Separate
  receipt ordering and the processing lock permit ongoing receipt while a
  Process runs. Workers preserve durable execution IDs and verify lock
  ownership before publishing results. Recover an unstarted attempt or a
  prepared send only when the persisted evidence permits it. Blocked, Running,
  Sending, and Unknown with uncertain effects require reconciliation through
  their owner. A verified result never invokes the business callback a second time.
- [Bot Dispatch](messaging/chatbot/doctype/bot_dispatch/) manages reservation,
  acceptance, and quota. A standard accepted compound response counts once.
  Daily windows are rolling 24 hours. Monthly windows use the configured time
  zone. Pending reservations retain capacity and definitive rejection releases
  it. Registration exemptions, courtesy messages and human responses have
  separate eligibility. Do not infer successful acceptance or refund capacity
  based on later delivery status.
- [Chat Session](messaging/conversation/doctype/chat_session/) owns human state
  transitions, permissions, inbox and supervision. Human Queued, Active and
  Paused suspend automatic work. Transfer uses the configured Human flow,
  approved Complete Flow outcome, and resulting request fields. Agents operate
  within their grants, capacity and assignment. Use the native permission
  queries for both list and document access. The active agent controls the
  read cursor and replies. Supervision respects its own permissions and current
  revisions for reassignment.
- The optional weekly schedule and request threshold belong to the native human
  availability Process and Bot Settings. Its schedule uses site time and does
  not establish agent capacity. The threshold counts eligible requests against
  their canonical incoming messages. The human state machine distinguishes
  queue wait from assigned inactivity. The hourly `human.close_inactive` job
  closes eligible sessions under the existing lock. Manual or inactivity
  closure can produce one localized completion notice through Bot Dispatch.
  Technical closure follows its own durable recovery. An idle automatic
  episode can expire after 15 minutes when it has no unresolved work.

## Templates and notifications

[Bot Flow Template](messaging/chatbot/doctype/bot_flow_template/) is internal
localized content linked from Bot Flow. It has language, enablement, content
components, variables and revision. It has no account or provider scope.
[Communication Template](messaging/communication/doctype/communication_template/)
belongs to a Communication Channel and, for providers declaring Account scope,
a Communication Channel Account. Its deterministic name includes the scope
before the normalized descriptive name and language. Administrative activation
uses `set_active`. Providers with external locale identifiers declare
`template_languages` as `TemplateLanguage(code, label, language=None)` entries.
The optional `language` is an explicit Frappe Language mapping. Template
identity and external projections use the exact `provider_language`; the native
`language` remains editable for notification presentation. Resolution checks
enabled explicit and conventional matches, then the site-wide Bot Settings
default. Persisted manual and provisional assignments survive subsequent
reads. Registered templates can specify a `provider_languages` mapping keyed
by their native content language when materializing into one of these channels.
A provider can declare external template states and the
`can_send` policy, and use `template_available` for extra availability conditions.
Local Channel templates render their saved content. Externally managed
templates keep a local draft, the latest provider reading, and an independently
verified snapshot containing the external identity, scope, content, and
parameter mapping. An ordinary save changes the draft. Import reads a complete
external scope, and publication submits a draft only after an explicit
administrative action with a durable claim. A provider can share an external
scope across several native bindings. A webhook requests a fresh scoped read. Legacy status rows remain readable.
Delivery requires the current published snapshot contract.

Communication Template validates the native `header`, `body`, `footer`, and
`template_variables` representation. Keep literal, single-pass interpolation
with exact declared snake_case variables and provider component limits. A
notification outcome links to a template compatible with the rule's account.
Its language comes from the template independently of chatbot languages.
External output validation and delivery use the published parameter mapping.
A changed external revision requires revalidation. Pending delivery retains
its original revision and waits if that revision is unavailable.

The notification path is document event → Notification Rule → shared Process
Runtime and process log → Notification Queue → dispatcher → neutral transport.

- [Notification Rule](messaging/communication/doctype/notification_rule/) owns
  source application, DocType, event/condition rows, approved input projection,
  outcomes and account binding. Supported events are `after_insert`,
  `on_update`, `on_submit`, `on_cancel`, and `on_update_after_submit`, subject
  to the DocType's native lifecycle. Each eligible row is an independent
  condition. Validate supported persisted field types and compare canonical
  values. Inputs use typed fixed values or allowed source fields. A Send outcome
  needs a compatible template and required variables. Event or delivery
  identities, and first account binding, follow the controller's immutability
  checks. New rules remain Untested pending approval.
- [events.py](messaging/communication/doctype/notification_rule/events.py) uses
  the global `doc_events` hook and a fast enabled-rule filter. Preparation,
  unique occurrence claim, execution evidence and queue snapshot participate
  in the business document transaction. A document rollback also rolls back the
  prepared notice. The occurrence key includes rule, source, event and canonical
  condition. A Running Process Execution Log claims it before invocation.
  A field-filtered `on_update_after_submit` skips unchanged values. Installed
  `messaging_business_event_observers` keep their after-commit contract.
  Channel ingress goes through its adapter.
- [Notification Queue](messaging/communication/doctype/notification_queue/)
  stores the immutable occurrence, account and original/effective destination,
  template revision, rendered content and schedule. The rule delay defaults
  to 60 minutes for new rules and is measured in site-local time. Changing a
  rule's delay leaves prepared dates intact. Redirection follows declared
  capabilities and replaces the original destination. The registered provider
  resolves and validates the destination selected by the rule. A missing or
  invalid destination suppresses delivery while preserving the functional
  outcome in the execution log. Provider template adaptations apply to the
  transport payload. The queue retains the original template values.
  Notification Process preparation and revalidation must be repeatable and
  free of external effects.
- `dispatch.dispatch_due` claims due work and checks scope, configured rule
  and current approval, original occurrence, document and delivery
  dependencies before sending the stored snapshot. A pending `on_submit`
  notice is cancelled with `source_cancelled` before transport when its source
  document `docstatus` is 2. That read runs on every attempt, including after a
  deferral, and again immediately before the handoff marker. `on_cancel`
  notices still send. An accepted handoff is not withdrawn or resent. Open
  conversation or human attention can defer delivery for the exact recipient
  identities. The lock and durable handoff marker distinguish safe deferral
  from a potentially completed send. Processing without a handoff can be
  recovered. Once a handoff may have happened, retain Unknown pending verified
  reconciliation. Notification delivery neither creates conversation records
  nor consumes bot quota.

Pending and Deferred work remains eligible for a future claim. Processing holds
the durable claim. Accepted, Rejected, and Cancelled end the send lifecycle.
An Unknown result can advance to Accepted or Rejected only with verified
evidence. Subsequent delivery updates remain separate from the send lifecycle.

## AI interpretation and evaluation

The public [AI contracts](messaging/registry/ai_providers/) define typed
Decision and Parameter Extraction requests and responses. Approved, available
Decision profiles can interpret new requests.
Extraction has a contract and generic execution entry point. Conversational
capture remains deterministic, and the Extraction evaluation worker reports
that capability as unavailable. Internal providers resolve verified installed
code. External providers use an HTTPS POST to the profile's `endpoint`. The
[REST representation](messaging/registry/ai_providers/rest.py) selects
`decision-v2` or `parameter-extraction-v2` by profile purpose and preserves
the typed schema version independently.

Decision operations return `DecisionEvidence` with one
`DecisionClassificationResult` per offered flow reference and each offered
signal (`greeting`, `human_attention`). Classifications are `request`,
`absent`, `excluded`, and `unclear`. Confidence and distribution scores may
be omitted and remain unknown. Messaging applies the Decision profile's
reserved acceptance thresholds and decides which flows to run. Internal
provider options use a separate, read-only mapping. REST can transmit declared
non-secret scalars marked `send_to_rest` in the separate `provider_parameters`
object. Keep connection settings and the native acceptance criteria out of it.

Parameter Extraction uses its own validator and typed observations.
It can include parameter descriptions, bounded context and optional evidence.
Exact decimal values are strings and dates use canonical ISO format. Messaging
preserves deterministic capture and has no conversational extraction executor.
Extraction profiles declare no shared acceptance thresholds.

[Bot Settings interpretation](messaging/chatbot/doctype/bot_settings/interpretation.py)
constructs the shared request. It recognizes and substitutes configured values
locally, keeps occurrence spans private, and publishes opaque flow references
with localized labels and context. Validate response type, version, request
correlation, outcome, permitted references and confidence before using it.
Greeting and human attention are separate signals. The decision cannot select
parameter values, authorize a transfer, or invoke a Process directly. Retain
complete capture and menu validation. The [wire validator](messaging/registry/ai_providers/wire.py)
is the source for closed payload fields, supported outcomes, and limits. The
request's opaque references are valid only in that request.

AI Configuration owns profile values, identity, connectivity, and approval
gates. Saving or reading a configuration never performs a provider call.
AI Profile is the Desk label for the AI Configuration DocType. Its read-only
Status starts Untested. A successful explicit technical evaluation enables it.
Disabling keeps the approval and permits explicit reactivation while evidence
is current. Material changes and confirmed failures withdraw availability.
Transient connection errors update diagnostics and preserve approval.
The Code-First registry manages internal AI Provider availability. External
REST providers retain administrator-managed availability. Provider withdrawal
suspends affected profiles and preserves evaluation evidence. A returning
provider can restore an approved profile to Disabled for explicit activation.
Connection testing is an explicit administrative action. It checks connectivity
and the wire contract. AI Provider parameters and credentials remain scoped to
the executor. HTTPS connections enforce destination and certificate validation
with bounded payloads and no automatic retry or redirect. The one-shot
[transport](messaging/chatbot/doctype/ai_configuration/rest_transport.py)
bounds DNS and blocking HTTPS operations with a subprocess deadline. Private
host allowlisting is a site configuration decision. AI Request Log holds
restricted, redacted diagnostic evidence. Never log credentials or full
unfiltered requests.

[AI Evaluation Run](messaging/chatbot/doctype/ai_evaluation_run/) owns preview,
selection, bounded worker execution, and approval. It snapshots revisions,
inputs, expectations, and coverage by scenario and language, and preserves
per-sample evidence in AI Evaluation Result. The catalog includes registered
multilingual Decision and Extraction fixtures. Coverage selection is
reproducible, scoped to the profile and active dependencies. The
`technical-required-v1` policy requires current Real evidence, complete
mandatory coverage, and technical success of every mandatory sample. Functional
differences are reported separately. Test evidence and connection checks alone
cannot approve a profile. Recheck dependencies at use, preserve invalidation
history, and never reinvoke an uncertain evaluation attempt automatically.

## EMULR and operations

[EMULR](messaging/emulator/provider.py) implements the same authenticated
account and transport boundaries as external adapters. The registered provider
permits one channel named `emulator` and multiple generic accounts. Its
account callback generates the stable `EMU-` account ID on creation.
`transport_outcome` applies to new simulated handoffs. The provider's
`notification_contact` parameter lives on each rule. Emulator Contact supplies
global, fictitious sender identities and optional generated names. Disabled
contacts and accounts preserve readable history. The real canonical Chat
Contact and session scope still include the account.

[Chat Emulator](messaging/emulator/page/emulator_console/) is a System Manager
Desk Page. It offers a unified conversation list and paginated, account-scoped
history, with independent notification history. It persists input via
`dispatch_ingress` before returning acknowledgement. Conversation processing is
asynchronous. AI availability is independent of EMULR ingress. Keep the
`client_id` stable on a duplicate send and correlate explicit menu selections
to an accepted dispatch. History reads issue scoped delivery receipts for
eligible accepted output. Delivered and Read are authenticated POST reports.
Read also requires evidence of continuous visible exposure. Diagnostic controls
can inspect or simulate supported transport outcomes. They cannot fabricate
Read evidence or authorize resends.

The controller at
[`messaging.emulator.page.emulator_console.emulator_console`](messaging/emulator/page/emulator_console/emulator_console.py)
defines the native Desk routes. Use `accounts`, `contacts`, `conversations`,
`available_pairs`, and `history` for authenticated reads. `send_message`,
`client_status`, `observe_message`, and the `simulate_*` diagnostics require
POST. History has separate message and notification cursors, and session-scoped
views use a session cursor. Reuse the original client ID when retrying ingress
after an uncertain HTTP acknowledgement. Read permission or HTTP success does
not authorize a send or prove that processing or delivery finished.

Use native Frappe Redis, scheduler and workers for runtime jobs. Conversation
recovery and due notification dispatch run on minutely cron hooks. Inactivity
closure runs hourly. Consume the `default` queue for the scheduled recovery job
and `long` for its contact workers and evaluation work. The contact worker
has a 1,200-second RQ timeout. Check the site scheduler, Bench-wide scheduler
tick and worker subscriptions when diagnosing latency. A minutely expression
sets an opportunity, with actual processing dependent on tick, backlog, and
worker capacity.

Inspect Scheduled Job Type history and the native job registry for worker
execution, Chat Turn for inbound receipt and processing evidence, Bot Dispatch
and Notification Queue for transport, and Process Execution Log for business
execution. Inspect the exact contact and source references.

## Development and verification

- Keep DocType tests in their Frappe locations, typically
  `messaging/<module>/doctype/<doctype>/test_<doctype>.py`. Use a development
  test site with synthetic data and reversible fixtures for document and
  transaction tests. Page tests can include standalone Node.js checks.
  See [README.md](README.md) for commands.
- Verify functional changes through the affected entry points so the checks
  cover Frappe permissions, document hooks, and transaction boundaries where
  applicable. Use fictional contacts and EMULR to exercise the Messaging
  transport boundary without contacting an external provider.

## Verificación estática obligatoria del código Python

### Obligación general

Todo agente que modifique código Python de Messaging debe ejecutar las
verificaciones estáticas descritas en esta sección antes de declarar completada
su tarea. Esta validación forma parte obligatoria de la definición de trabajo
terminado, independientemente del modelo de IA utilizado, y no constituye una
recomendación opcional.

Las versiones fijadas para reproducibilidad se mantienen en el grupo
`static-checks` de [pyproject.toml](pyproject.toml) (`ruff==0.17.6` y
`pyright==1.1.414`) y en la configuración versionada
[pyrightconfig.json](pyrightconfig.json), sin incorporarse como dependencias
del runtime productivo.

### Ruff obligatorio

Ejecutar desde la raíz del repositorio:

```bash
uvx ruff==0.17.6 check messaging --select F821,F822,F823
```

El análisis cubre tanto el código productivo como todas las pruebas
automatizadas (`F821` nombres indefinidos, `F822` símbolos indefinidos en
`__all__`, `F823` variables locales referenciadas antes de su asignación). No
excluir archivos de prueba para conseguir una aprobación artificial. El
resultado esperado es `All checks passed!`.

### Pyright obligatorio

Ejecutar desde la raíz del repositorio dentro del entorno Bench con Python 3.14
y Frappe v16 disponibles (resueltos mediante [pyrightconfig.json](pyrightconfig.json)):

```bash
uvx --from pyright==1.1.414 pyright messaging
```

Cuando el entorno Bench se encuentra en otra ruta o fuera del árbol estándar
`<bench>/apps/messaging`, indicar explícitamente `--pythonpath <ruta-python-bench>`.

La configuración utiliza modo `basic` sobre Python 3.14 y mantiene habilitados
los diagnósticos de variables indefinidas (`reportUndefinedVariable`),
importaciones inexistentes (`reportMissingImports`), símbolos desconocidos o
atributos inexistentes determinables estáticamente (`reportAttributeAccessIssue`),
errores generales de tipos (`reportGeneralTypeIssues`) y variables no enlazadas
(`reportUnboundVariable`). Revisar especialmente las referencias entre módulos
y las importaciones privadas que puedan quedar obsoletas tras reorganizaciones
arquitectónicas.

### Compilación obligatoria

Ejecutar con el intérprete Python 3.14 del entorno de desarrollo (Bench):

```bash
python -m compileall -q messaging
```

Esta comprobación verifica ausencia de errores de sintaxis en todo el paquete;
no sustituye a Ruff, Pyright ni a las pruebas funcionales.

### Corrección antes de finalizar

Si cualquiera de las comprobaciones detecta errores:

1. Identificar el origen exacto del diagnóstico.
2. Determinar si corresponde a un defecto real o a una limitación legítima del
   análisis estático frente a mecanismos dinámicos de Frappe.
3. Corregir los defectos reales introducidos o directamente afectados por la
   intervención (nombres indefinidos, importaciones inexistentes, referencias
   rotas entre módulos, contratos o llamadas incompatibles y mocks hacia
   símbolos inexistentes).
4. Repetir las verificaciones estáticas aplicables.
5. Ejecutar las pruebas funcionales focalizadas correspondientes según el
   impacto.

Ruff (`F821,F822,F823`) y `compileall` deben finalizar sin errores. En Pyright,
los diagnósticos legítimos derivados del tipado dinámico de Frappe (como campos
dinámicos de DocTypes, atributos construidos en tiempo de ejecución o retornos
dinámicos de `frappe.db.get_value`) no bloquean automáticamente la finalización
y deben distinguirse transparentemente en el informe en lugar de perseguirse
artificialmente con cero diagnósticos globales. No declarar aprobada ninguna
comprobación que mantenga defectos reales sin corregir.

### Prohibición de ocultar errores

Está prohibido ocultar defectos reales mediante:

- `# noqa` para silenciar problemas confirmados.
- `# type: ignore` sin justificación técnica explícita.
- Exclusiones generales injustificadas en la configuración.
- Desactivación global de diagnósticos relevantes.
- Imports ficticios o variables artificiales creadas solo para silenciar
  herramientas.

Cualquier excepción legítima motivada por comportamiento dinámico de Frappe
debe justificarse técnicamente y limitarse estrictamente al caso necesario.

### Tratamiento de errores preexistentes

No introducir errores nuevos. Si durante la auditoría se detectan diagnósticos
preexistentes:

- Corregir los que pertenezcan al alcance de la tarea o impidan validar las
  modificaciones actuales.
- Documentar por separado los problemas preexistentes no relacionados, sin
  ocultarlos ni presentar una aprobación inexistente.
- Evitar refactorizaciones generales fuera del alcance solicitado.

### Revalidación tras modificaciones

Si después de una verificación se vuelve a editar código Python —incluidos
archivos de prueba o ajustes finales—, repetir las verificaciones estáticas
aplicables y las pruebas afectadas. La aprobación final debe corresponder al
estado exacto del árbol de trabajo entregado.

### Evidencia en los informes

Todo informe de implementación que incluya cambios en código Python debe
registrar:

- Herramientas utilizadas y versiones exactas de Ruff y Pyright.
- Comandos ejecutados.
- Cantidad y naturaleza de los errores detectados inicialmente.
- Correcciones realizadas.
- Resultados finales verificados sobre el árbol entregado.
- Comprobaciones omitidas y su justificación técnica, si existieran.

No inventar resultados ni reutilizar comprobaciones históricas como evidencia
del estado actual.

### Alcance permanente

Esta política rige permanentemente para todos los trabajos futuros de
desarrollo Python en Messaging y se mantiene versionada en `AGENTS.md`. No
duplicar ni contradecir estas reglas en archivos locales ni modificar
`AGENTS.local.md` por motivo de esta política.
