# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from collections.abc import AsyncIterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, cast

import yaml

from gestalt_connector_protocol.connector import Connector, ConnectorConfig, ConnectorValidationResult
from gestalt_connector_protocol.models import Document


class ConnectorValidationError(RuntimeError):
    pass


async def validate_connector(connector_ref: str, config_file: str | Path) -> ConnectorValidationResult:
    config = _load_config(config_file, connector_ref)
    connector = _load_connector(connector_ref)
    health = await connector.health_check(config)
    if health.status != "ok":
        raise ConnectorValidationError(f"health_check returned {health.status}: {health.message}")
    count = 0
    async for emitted in _iterate_documents(connector, config):
        document = Document.model_validate(emitted.model_dump(mode="json") if isinstance(emitted, Document) else emitted)
        _validate_document(document, config.connector_id)
        count += 1
    if count == 0:
        raise ConnectorValidationError("connector emitted zero documents")
    return ConnectorValidationResult(connector_id=config.connector_id, documents_validated=count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connector-test")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("connector_id")
    validate.add_argument("config_file")
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            result = asyncio.run(validate_connector(args.connector_id, args.config_file))
        except Exception as exc:
            print(f"connector-test failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"connector_id": result.connector_id, "documents_validated": result.documents_validated}))
        return 0
    return 1


def _load_config(config_file: str | Path, connector_ref: str) -> ConnectorConfig:
    path = Path(config_file)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix in {".yaml", ".yml"} else json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConnectorValidationError("config file must contain an object")
    data.setdefault("connector_id", connector_ref.split(":", 1)[0])
    return ConnectorConfig(
        connector_id=str(data["connector_id"]),
        display_name=str(data.get("display_name", "")),
        auth=cast(dict[str, str], data.get("auth", {})),
        settings=cast(dict[str, Any], data.get("settings", {})),
    )


def _load_connector(connector_ref: str) -> Connector:
    connector_cls: type[Any]
    if ":" in connector_ref:
        module_name, attr = connector_ref.split(":", 1)
        module = importlib.import_module(module_name)
        connector_cls = getattr(module, attr)
    else:
        matches = [item for item in entry_points(group="gestalt.connectors") if item.name == connector_ref]
        if not matches:
            raise ConnectorValidationError(f"connector not found: {connector_ref}")
        connector_cls = matches[0].load()
    return cast(Connector, connector_cls())


def _iterate_documents(connector: Connector, config: ConnectorConfig) -> AsyncIterator[Document]:
    return connector.discover_documents(config)


def _validate_document(document: Document, connector_id: str) -> None:
    if not document.body_text.strip():
        raise ConnectorValidationError(f"{document.doc_id} has empty body_text")
    if document.source.connector_id != connector_id:
        raise ConnectorValidationError(f"{document.doc_id} source.connector_id does not match {connector_id}")
    if not isinstance(document.privacy.redactions_applied, list):
        raise ConnectorValidationError(f"{document.doc_id} privacy.redactions_applied must be a list")


if __name__ == "__main__":
    raise SystemExit(main())