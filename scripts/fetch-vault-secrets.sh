#!/usr/bin/env bash

set -euo pipefail

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$cmd" >&2
    exit 1
  fi
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf 'Missing required environment variable: %s\n' "$name" >&2
    exit 1
  fi
}

require_command curl
require_command jq

require_env VAULT_ADDR
require_env VAULT_SECRET_PATH

VAULT_KV_VERSION="${VAULT_KV_VERSION:-2}"
VAULT_SECRET_JSON_KEY="${VAULT_SECRET_JSON_KEY:-google_workspace_cli_credentials_json}"
VAULT_CLOUDINARY_CLOUD_NAME_KEY="${VAULT_CLOUDINARY_CLOUD_NAME_KEY:-cloudinary_cloud_name}"
VAULT_CLOUDINARY_API_KEY_KEY="${VAULT_CLOUDINARY_API_KEY_KEY:-cloudinary_api_key}"
VAULT_CLOUDINARY_API_SECRET_KEY="${VAULT_CLOUDINARY_API_SECRET_KEY:-cloudinary_api_secret}"

if [[ -n "${VAULT_TOKEN_FILE:-}" ]]; then
  VAULT_TOKEN="$(<"$VAULT_TOKEN_FILE")"
fi
require_env VAULT_TOKEN

secret_url="${VAULT_ADDR%/}/v1/${VAULT_SECRET_PATH#/}"
response="$(curl -fsSL -H "X-Vault-Token: $VAULT_TOKEN" "$secret_url")"

if [[ "$VAULT_KV_VERSION" == "2" ]]; then
  secret_data="$(jq -c '.data.data' <<<"$response")"
else
  secret_data="$(jq -c '.data' <<<"$response")"
fi

if [[ "$secret_data" == "null" || -z "$secret_data" ]]; then
  printf 'Vault response did not contain a data object\n' >&2
  exit 1
fi

runtime_dir="${AGENT_TOOLS_ROOT:-/opt/content-agent-tools}/runtime"
mkdir -p "$runtime_dir"

gws_credentials_file="$runtime_dir/gws-credentials.json"
jq -er --arg key "$VAULT_SECRET_JSON_KEY" '.[$key]' <<<"$secret_data" >"$gws_credentials_file"
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE="$gws_credentials_file"

cloud_name="$(jq -er --arg key "$VAULT_CLOUDINARY_CLOUD_NAME_KEY" '.[$key] // empty' <<<"$secret_data")"
api_key="$(jq -er --arg key "$VAULT_CLOUDINARY_API_KEY_KEY" '.[$key] // empty' <<<"$secret_data")"
api_secret="$(jq -er --arg key "$VAULT_CLOUDINARY_API_SECRET_KEY" '.[$key] // empty' <<<"$secret_data")"

if [[ -n "$cloud_name" ]]; then
  export CLOUDINARY_CLOUD_NAME="$cloud_name"
fi
if [[ -n "$api_key" ]]; then
  export CLOUDINARY_API_KEY="$api_key"
fi
if [[ -n "$api_secret" ]]; then
  export CLOUDINARY_API_SECRET="$api_secret"
fi

printf 'Vault secrets loaded into runtime environment\n' >&2
