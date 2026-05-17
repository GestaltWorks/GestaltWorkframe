# Connector Registry

| Connector | Package | Config schema | Redaction notes |
|---|---|---|---|
| Filesystem / UNC | `gestalt-connector-fs` | `root_path`, `include_extensions`, `exclude_dirs`, `redaction_whitelist` | Runs the protocol redaction pipeline over extracted text before emitting `Document` records. Whitelisted internal network data remains local-only. |
| ITGlue | `gestalt-connector-itglue` | `base_url`, OAuth/API-key auth, `page_size`, `organization_ids` | Password values and secure fields are stripped before redaction. All translated text still runs through the redaction pipeline. |
| Hudu | `gestalt-connector-hudu` | `base_url`, API-key auth, `page_size`, `company_ids` | Secure fields are stripped before emit; articles, assets, processes, and relationships still run through redaction. |
| Microsoft Graph Files | `gestalt-connector-msgraph-files` | Graph OAuth bearer token, `tenant_id`, `site_ids`, `drive_ids`, delta tokens | Downloaded file text still goes through the shared redaction pipeline; SharePoint list metadata becomes structured sections. |
| S3-compatible object storage | `gestalt-connector-s3` | AWS access key auth, `region`, `bucket`, optional `endpoint_url`, `prefixes`, `include_extensions` | Object bodies are fetched only for allowed text-like extensions and pass through the shared redaction pipeline before emit. |
