<!--
Copyright 2026 Gestalt Works

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Gestalt Connector Protocol

This package defines the canonical `Document` contract and the connector
interface used by Gestalt Works deployments.

## Schema evolution

`Document` schema `1.0` is frozen for 18 months after release. Compatible
changes may add optional fields with defaults. Breaking changes require a new
semantic schema version, a generated `docs/schemas/document.vN.json` artifact,
and a migration adapter from the previous supported version.
