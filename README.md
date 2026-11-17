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
| [Registration contracts](messaging/registrations/) and [registry](messaging/registry/) | Public declaration types and installed application discovery, validation, and synchronization |
| [Communication](messaging/communication/) | Provider catalog and operations, channel and account configuration, shared Process Runtime and transport, notification rules, templates, and queue |
| [Chatbot](messaging/chatbot/) | Bot Settings, flows, typed parameters and capture, interpretation and AI evaluation, Bot Flow Templates, and bot dispatch |
| [Conversation](messaging/conversation/) | Contact and session identity, inbound turns, message history, and scoped human support |
| [Emulator](messaging/emulator/) | EMULR operations, the fictional contact catalog, and Chat Emulator |

The technical registry comes from installed application code. Desk documents hold channel and account settings, flow bindings, localized content, notification rules, and operational approvals. The [repository guide](AGENTS.md) describes the ownership and lifecycle of each component.

## Requirements and installation

Use a Frappe v16 Bench with a MariaDB-backed site and Python 3.14. Bench manages Frappe. Messaging declares its Python dependencies, including Faker, Lingua, and regex, in [pyproject.toml](pyproject.toml). It has no required companion Frappe apps.

From an existing Bench directory, use the Messaging Git URL and your site name in place of the example variables.

```bash
bench get-app messaging "$MESSAGING_REPO_URL"
bench --site "$SITE_NAME" install-app messaging
```

Optional integrating apps, such as QOTA Presets, are installed separately according to their own instructions. They can contribute business declarations and provision their own documents.

Initial setup creates the technical roles and service identity, synchronizes the three registries, and provisions the native flow preset and evaluation catalogs. The application ships EMULR and three native processes for registration, greeting, and human availability. Channel accounts, external credentials, active bot flows, and notification sends require explicit setup. Migration synchronizes the technical registries and checks dependent evidence. Changes to preset or rule defaults are provisioned explicitly by their owning applications.

## Configuration in Desk

1. **Communication Provider, Channel, and Channel Account.** Enable a registered provider when it is ready for use. Create a channel and its accounts, supply the parameters for each scope, and set their enabled states. To use EMULR, configure its `emulator` channel, create an account, and add enabled Emulator Contacts.
2. **Bot Settings, Bot Parameters, and Bot Flows.** Configure the default chatbot language and the required messages. Define typed parameters and link flows to registered processes. Use Bot Settings to install available presets. Validate flows before enabling them and select the flows for registration, greeting, and human support as needed.
3. **AI Configuration.** To enable AI interpretation, install an app that declares a compatible AI Provider, configure a profile, and complete its connection and evaluation requirements. Bot Settings holds the selected profile.
4. **Notification Rule and Communication Template.** Select the registered notification process, document events and conditions, output templates, and channel account. Validate the rule before enabling it. The scheduler processes due rows in Notification Queue.
5. **Chat Agent.** Configure agent access and capacity within the required channel and account scopes.

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
