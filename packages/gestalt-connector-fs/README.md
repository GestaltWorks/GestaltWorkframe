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

# Gestalt Filesystem Connector

Walks a local, mounted NFS, or Windows UNC-accessible tree and emits canonical
documents. Hidden files and common build-output directories are skipped by
default. Polling-based change detection and full-walk reconciliation hooks ship
with the connector; OS-native watcher backends can be layered behind the same
interfaces later.
