# Provider template scaffold

A template is declarative data. Change the identity in `manifest.yaml`, set
connection defaults in `template.yaml`, and list any known upstream model IDs in
`catalog`. Templates never contain credentials or executable code. `auth_scheme`
must be one of the referenced adapter's declared schemes. Optional defaults include
`timeout`, `note` and `allow_private_network`; they apply when creating an upstream,
and explicit submitted values take precedence. The template and upstream must use
the same `adapter_id`.

ZIP these two YAML files and optionally this README at the archive root. Install
through the console's provider templates page, then enable the extension. Select
the template when adding an upstream. Supply credentials separately, and review
private-network permission before saving. `profile.extra_body` and
`profile.extra_query` configure additional non-conflicting connection parameters.

This scaffold does not register automatically. Built-in connections in
`templates/provider_presets/` do register at startup. Installed packages live under
`extensions/provider_templates/`. The manifest's `verification` defaults to
`declared`; a model catalog is reference data and does not prove model capabilities.
Use `category: coding_service` for a coding tool service, or the default `provider`.
See the [agent operations manual](../../agent/_doc/README.md) for management.
