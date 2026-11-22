# Messaging

Messaging is a standalone Frappe v16 application for conversations, configurable chatbots, human support, and document-driven notifications. Installed applications can supply business processes and transport or AI integrations. Administrators configure how those capabilities are used in Frappe Desk.

## Features

- **Code-first extensibility.** Register Process Definitions, Communication Providers, and AI Providers from their owning applications. Use the shared Process Runtime for validation, execution, and audit evidence.
- **Channel administration.** Configure a channel for a registered Communication Provider and one or more channel accounts, subject to the provider's capabilities.
- **Configurable bot flows.** Define intents, typed input capture, menus, and outcomes in Desk. A flow can route to an approved business process and return a process message or render a localized Bot Flow Template.
- **Conversation management.** Track contacts, sessions, turns, and messages. Use the inbox, My Conversations, History, and Supervision for human assignment, follow-up, and closure.
- **Document-driven notifications.** Select document events and conditions in Notification Rules, render localized Communication Templates, and track deferred delivery and transport results in Notification Queue.
- **Multilingual operation.** Configure chatbot languages and localized prompts, menus, and templates. Optional AI interpretation uses a configured provider and a shared versioned contract. A numbered menu is available without AI.
- **EMULR.** Use Chat Emulator and fictional contacts to exercise the channel, conversation, and delivery interfaces on a development site.

## Architecture

| Area | Ownership |
| --- | --- |
| [Registry](messaging/registry/) | Public declaration types and installed application discovery, validation, and synchronization |
| [Communication](messaging/communication/) | Provider catalog and operations, channel and account configuration, shared Process Runtime and transport, notification rules, templates, and queue |
| [Chatbot](messaging/chatbot/) | Bot Settings, flows, typed parameters and capture, interpretation and AI evaluation, Bot Flow Templates, and bot dispatch |
| [Conversation](messaging/conversation/) | Contact and session identity, inbound turns, message history, and scoped human support |
| [Emulator](messaging/emulator/) | EMULR operations, the fictional contact catalog, and Chat Emulator |
| [WhatsApp](messaging/whatsapp/) | Meta Cloud API operations, webhook ingress, and administration |

The technical registry comes from installed application code. Desk documents hold channel and account settings, flow bindings, localized content, notification rules, and operational approvals. The [repository guide](AGENTS.md) describes the ownership and lifecycle of each component.

## Requirements and installation

Use a Frappe v16 Bench with a MariaDB-backed site and Python 3.14. Bench manages Frappe. Messaging declares its Python dependencies, including Faker, Lingua, and regex, in [pyproject.toml](pyproject.toml). It has no required companion Frappe apps.

From an existing Bench directory, use the Messaging Git URL and your site name in place of the example variables.

```bash
bench get-app messaging "$MESSAGING_REPO_URL"
bench --site "$SITE_NAME" install-app messaging
```

Optional integrating apps, such as QOTA Presets, are installed separately according to their own instructions. They can contribute business declarations and provision their own documents.

Initial setup creates the technical roles and service identity, synchronizes the three registries, and provisions the native flow preset and evaluation catalogs. The application ships EMULR, WhatsApp, and three native processes for registration, greeting, and human availability. Channel accounts, external credentials, active bot flows, and notification sends require explicit setup. Migration synchronizes the technical registries and checks dependent evidence. Changes to preset or rule defaults are provisioned explicitly by their owning applications.

## Configuration in Desk

1. **Communication Provider, Channel, and Channel Account.** Enable a registered provider when it is ready for use. Create a channel and its accounts, supply the parameters for each scope, and set their enabled states. To use EMULR, configure its `emulator` channel, create an account, and add enabled Emulator Contacts.
2. **Bot Settings, Bot Parameters, and Bot Flows.** Configure the default chatbot language and the required messages. Define typed parameters and link flows to registered processes. Use Bot Settings to install available presets. Validate flows before enabling them and select the flows for registration, greeting, and human support as needed.
3. **AI Profile.** Configure a Decision profile for an installed Code-First provider or a generic external REST provider. Complete technical evaluation before selecting the enabled profile in Bot Settings.
4. **Notification Rule and Communication Template.** Select the registered notification process, document events and conditions, output templates, and channel account. Validate the rule before enabling it. The scheduler processes due rows in Notification Queue.
5. **Chat Agent.** Configure agent access and capacity within the required channel and account scopes.

### External REST AI providers

Create an External AI Provider in Desk, select its supported capabilities, and enable it. Create a separate AI Profile for each purpose. Set the **REST Service URL** on each profile. The profile can use no authentication, a Bearer credential, or Basic authentication with a username and credential. Frappe encrypts credentials in Password storage.

Messaging posts UTF-8 JSON to the HTTPS URL. It picks the wire version from the profile purpose and expects the service to echo `wire_version`, `schema_version`, and `request_id` in the response. The public typed schema remains version `"1"`.

Decision uses `wire_version: "decision-v2"`. A request has this shape.

```json
{
  "wire_version": "decision-v2",
  "schema_version": "1",
  "request_id": "b07d8c64",
  "message": {"text": "Check my service status"},
  "language": "en",
  "available_flows": [
    {"ref": "flow_a1", "label": "Service status", "description": "Check current service availability"}
  ],
  "greeting_available": true,
  "human_attention_available": false,
  "context": {"previously_recognized_flows": []},
  "allowed_outcomes": ["recognized", "unrecognized", "ambiguous", "empty_catalog"],
  "provider_parameters": {"model": "classifier"}
}
```

Return one row for each offered flow reference and each enabled signal (`greeting` or `human_attention`). The example response omits an unreported confidence and preserves an explicit zero probability.

```json
{
  "wire_version": "decision-v2",
  "schema_version": "1",
  "request_id": "b07d8c64",
  "results": [
    {"option_id": "flow_a1", "classification": "request", "confidence": 0.82,
     "probabilities": {"request": 0.9, "excluded": 0.0}},
    {"option_id": "greeting", "classification": "absent"}
  ]
}
```

Supported classes are `request`, `absent`, `excluded`, and `unclear`. Omitted scores and JSON `null` mean unreported. Messaging applies the native profile thresholds to the validated evidence and determines the outcome. The aggregate Decision v1 response is retired from external execution. A connection check verifies the current wire and leaves approval to the explicit technical evaluation.

Parameter Extraction uses `wire_version: "parameter-extraction-v2"` and the same version, correlation, message and language fields. The request includes optional text `context` and `parameters` with `ref`, `label`, `parameter_type`, `allow_multiple_values`, and optional `description`. The response contains one `parameters` row per offered reference with `ref`, `status`, `values`, and optional text array `evidence`. Statuses are `EXTRACTED`, `NOT_FOUND`, `AMBIGUOUS`, and `NOT_SUPPORTED`. Only `EXTRACTED` carries values. Numbers of type `Int` and values of type `Boolean` use JSON integers and booleans. `Decimal` uses exact decimal strings and `Date` uses canonical ISO date strings. This profile purpose supports configuration and contract validation. Conversational capture remains deterministic.

Mark additional scalar definitions **Send in REST Request** to include them in `provider_parameters`. Messaging omits absent values and the entire object when empty. Connection settings and Decision thresholds stay in Messaging. The service must return HTTP 200 with an uncompressed `application/json` body. Requests and responses are limited to 120,000 and 250,000 bytes respectively.

## Development and testing

Keep DocType validation and persistence with their owning DocTypes. Use the registration contracts when adding a capability from an integrating app. See the [repository guide](AGENTS.md) for module boundaries, public contracts, and verification entry points.

Run the test module affected by a change on a development test site. For example, to run the Bot Parameter module from the Bench directory:

```bash
bench --site "$TEST_SITE" run-tests --module messaging.chatbot.doctype.bot_parameter.test_bot_parameter
```

Standalone page checks use Node.js. From the Messaging repository root, one example is:

```bash
node messaging/conversation/page/conversation_supervision/test_conversation_supervision_ui.cjs
```

The repository includes [pre-commit configuration](.pre-commit-config.yaml) for linting and formatting.

## Localization

Use English source strings with Frappe's translation conventions, and add Spanish UI translations to [messaging/translations/es.csv](messaging/translations/es.csv). Administrators manage chatbot languages and localized messages and templates through their respective DocTypes. Language-specific presets and evaluation fixtures carry their content in the target language.

## Security

Provider code and process handlers are resolved from verified installed applications. Channel ingress and delivery reports are authenticated and scoped to their channel account. Desk access follows Frappe permissions and account or assignment scope. Configure credentials through the relevant Password fields and keep credentials, site settings, and internal evidence out of the repository.

## Public information by channel

A System Manager can maintain public legal information on the **Public
Information** tab of a saved Communication Channel. Enter the legal entity or
organization, public service name and contact email. Add an official website,
additional contact details and a service description where applicable.
The details come from this tab.

The tab offers **Privacy Policy**, **Terms of Service** and **Data Deletion
Instructions**. Open **Edit Draft**, optionally load the Spanish standard
template, then edit and save. Complete any sections marked for review and
use **Preview Draft** to review the saved content and contact details.
Confirm **Publish** to make that document available. Publication stores a
copy of the reviewed content and contact details. Later edits take effect
when you explicitly publish the updated draft. **Withdraw Publication**
requires confirmation and removes public access while retaining the draft.

Public URLs use the same configured HTTPS origin as the WhatsApp webhook.
Set the site's Frappe `host_name` to its public origin, such as
`https://messaging.example.com`. Messaging also accepts the configured
`hostname` fallback. **Copy Public URL** becomes available for each published
document when a valid public origin is configured. The routes use the immutable
channel ID and end in `privacy-policy`, `terms-of-service` and `data-deletion`
under `/messaging/<channel>/`. Published pages are available without login
and remain available when the channel is disabled.

For a WhatsApp channel, copy each applicable URL into the corresponding
Privacy Policy URL, Terms of Service URL or Data Deletion Instructions URL
field in the app's Meta Developers dashboard. The deletion page describes
how to submit a request. The organization handles requests through its
authorized procedures.

## WhatsApp Cloud API

Messaging includes a native WhatsApp provider. Install or migrate Messaging with
Bench to register it, then enable **WhatsApp** in Communication Provider.
Manage Meta apps, phone numbers, permissions, app publication and webhook
subscriptions in Meta Developers. Messaging configures the integration,
exposes the webhook endpoint and handles incoming and outgoing communications.

1. Create one Communication Channel for each Meta App and webhook. Enter the
   unique Meta App ID, the App Secret and the Graph API version, which defaults
   to `v27.0`. Save the channel and select **Generate verification token** under
   **Webhook Actions**.
   The Webhook Verify Token field is read-only. Enable the channel after
   completing the required settings and setting the public HTTPS origin in step 3.
   Use **Copy verification token** to configure webhook verification in Meta.
   After using **Regenerate verification token**, update it in Meta Developers.
   All numbers on the channel use this
   webhook configuration. Signed POST events require the Meta App Secret.
2. Create a Communication Channel Account for each Meta number. Enter the
   immutable **Phone Number ID**, the **Messaging Account ID**, and the number's
   Meta access token. Select **Check and enable** to verify the Phone Number ID
   and save the display number returned by Meta. **General** includes a status
   summary. **Diagnostics** retains the latest connection check, subscription
   query and signed message webhook evidence for that number. Failed checks
   also remain available after reload. Relevant configuration changes mark
   earlier evidence as historical. Use **WhatsApp Actions** to check the
   connection or query the configured Messaging Account's subscribed apps.
   The subscription query is read-only. Multiple numbers can share a Messaging
   Account ID and retain independent diagnostics. Disabling an account only
   changes local status.
3. Set the site's Frappe `host_name` to its public HTTPS origin. Open the saved
   channel and use **Copy webhook URL** under **Webhook Actions** to enter its callback in the
   configured app in Meta Developers. The
   `/api/method/messaging.whatsapp.webhook.handler` route uses the saved channel
   ID in its `channel` query parameter. Enter letters and spaces in **Channel
   Name**. New channels use a lowercase ASCII ID with underscores between
   words unless the provider assigns a fixed ID. Editing the display name
   preserves the ID and callback URL.
   Subscribe to `messages` and `message_template_status_update` in Meta Developers.
   Configure the reverse proxy to limit the webhook request body to 3 MiB and
   omit the verify-token query string from access logs.
4. Open **Synchronize templates** in the Communication Template list and select
   WhatsApp, the channel, and its account. Messaging reads the selected WABA and
   imports supported text templates with **POSITIONAL** or **NAMED** parameters.
   **Provider Language** keeps the
   exact Meta variant throughout the template lifecycle. **Language** stores the
   enabled Frappe Language used by notifications. When Messaging finds no
   matching native code, it assigns the Bot Settings default provisionally if
   configured. Review the mapping in Desk and set a native language before
   activating a template. Correcting it preserves the external variant and
   published revision. Accounts in the same WABA share external status and
   content readings through separate template documents.
5. Select **Provider Parameter Format** for a new WhatsApp template. Positions
   are the default. For **POSITIONAL** templates, row order in **Template
   Parameters** determines the external position within each component
   independently (Header and Body). For **NAMED** templates, each parameter name
   matches the exact Meta name with no aliases; a named variable can appear more
   than once in the body or in both the text header and the body, and Messaging
   uses one value per name. Meta names incompatible with Messaging parameter
   naming rules are reported explicitly instead of aliased.
   **Save** records the draft. **Publish to provider** submits saved Utility or
   Marketing content. **Synchronize status** reads current provider evidence.
   Supply one example per variable in the publication dialog.
   Activate the template separately in Messaging. Queued notifications carry
   the exact published revision. Later draft changes leave that revision intact.
6. Select the account and template in Notification Rule. Use an international
   destination with digits and an optional leading `+`. Bot and human text and
   interactive replies require a recent inbound message in the same account's
   24-hour customer service window.

Messaging stores incoming media and other unsupported message types as
unsupported conversation events with their original Meta message IDs. The
current channel envelope carries text, menu choices and text templates.
Template approval refreshes and read acknowledgements use Bench workers. Use
separate channel and account configuration for test and production credentials.

The transport and webhook protocol were adapted from the MIT-licensed
[upstream WhatsApp integration](https://github.com/frappe/whatsapp/tree/70f77255a276a5a0a68da13f2d4767914b50f70a).
See the [upstream license and attribution](messaging/whatsapp/LICENSE).
