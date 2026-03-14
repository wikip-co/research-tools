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

fetch_vault_secret_data() {
  local secret_path="$1"
  local secret_url="${VAULT_ADDR%/}/v1/${secret_path#/}"
  local response
  response="$(curl -fsSL -H "X-Vault-Token: $VAULT_TOKEN" "$secret_url")"

  if [[ "$VAULT_KV_VERSION" == "2" ]]; then
    jq -ec '.data.data' <<<"$response"
  else
    jq -ec '.data' <<<"$response"
  fi
}

read_env_or_file() {
  local target_name="$1"
  local file_name="${2:-${target_name}_FILE}"
  if [[ -n "${!target_name:-}" ]]; then
    return
  fi
  if [[ -n "${!file_name:-}" ]]; then
    printf -v "$target_name" '%s' "$(<"${!file_name}")"
    export "$target_name"
  fi
}

vault_login_with_userpass() {
  require_env VAULT_USERNAME
  require_env VAULT_PASSWORD

  local auth_path="${VAULT_AUTH_PATH:-auth/userpass/login}"
  local login_url="${VAULT_ADDR%/}/v1/${auth_path#/}/${VAULT_USERNAME}"
  local login_payload
  login_payload="$(jq -cn --arg password "$VAULT_PASSWORD" '{password: $password}')"

  local login_response
  login_response="$(
    curl -fsSL \
      -H 'Content-Type: application/json' \
      -X POST \
      -d "$login_payload" \
      "$login_url"
  )"

  VAULT_TOKEN="$(jq -er '.auth.client_token' <<<"$login_response")"
  export VAULT_TOKEN
}

require_command curl
require_command jq

require_env VAULT_ADDR

VAULT_KV_VERSION="${VAULT_KV_VERSION:-2}"
VAULT_SECRET_PATH="${VAULT_SECRET_PATH:-}"
VAULT_GOOGLE_SECRET_PATH="${VAULT_GOOGLE_SECRET_PATH:-$VAULT_SECRET_PATH}"
VAULT_CLOUDINARY_SECRET_PATH="${VAULT_CLOUDINARY_SECRET_PATH:-$VAULT_SECRET_PATH}"
VAULT_SECRET_JSON_KEY="${VAULT_SECRET_JSON_KEY:-google_workspace_cli_credentials_json}"
VAULT_CLOUDINARY_CLOUD_NAME_KEY="${VAULT_CLOUDINARY_CLOUD_NAME_KEY:-cloud_name}"
VAULT_CLOUDINARY_API_KEY_KEY="${VAULT_CLOUDINARY_API_KEY_KEY:-key}"
VAULT_CLOUDINARY_API_SECRET_KEY="${VAULT_CLOUDINARY_API_SECRET_KEY:-secret}"

read_env_or_file VAULT_TOKEN
read_env_or_file VAULT_PASSWORD

if [[ -z "${VAULT_TOKEN:-}" ]]; then
  vault_login_with_userpass
fi
require_env VAULT_TOKEN

if [[ -z "$VAULT_GOOGLE_SECRET_PATH" && -z "$VAULT_CLOUDINARY_SECRET_PATH" ]]; then
  printf 'Missing required environment variable: VAULT_GOOGLE_SECRET_PATH or VAULT_CLOUDINARY_SECRET_PATH or VAULT_SECRET_PATH\n' >&2
  exit 1
fi

runtime_dir="${AGENT_TOOLS_ROOT:-/opt/content-agent-tools}/runtime"
mkdir -p "$runtime_dir"

google_secret_data=''
cloudinary_secret_data=''

if [[ -n "$VAULT_GOOGLE_SECRET_PATH" ]]; then
  google_secret_data="$(fetch_vault_secret_data "$VAULT_GOOGLE_SECRET_PATH")"
fi
if [[ -n "$VAULT_CLOUDINARY_SECRET_PATH" ]]; then
  cloudinary_secret_data="$(fetch_vault_secret_data "$VAULT_CLOUDINARY_SECRET_PATH")"
fi

gws_credentials_file="$runtime_dir/gws-credentials.json"
if [[ -n "$google_secret_data" ]]; then
  if jq -e --arg key "$VAULT_SECRET_JSON_KEY" 'has($key)' <<<"$google_secret_data" >/dev/null; then
    jq -er --arg key "$VAULT_SECRET_JSON_KEY" '.[$key]' <<<"$google_secret_data" >"$gws_credentials_file"
  elif jq -e 'has("installed")' <<<"$google_secret_data" >/dev/null; then
    jq -ec '.installed' <<<"$google_secret_data" >"$gws_credentials_file"
  elif jq -e 'has("web")' <<<"$google_secret_data" >/dev/null; then
    jq -ec '.web' <<<"$google_secret_data" >"$gws_credentials_file"
  else
    jq -ec '.' <<<"$google_secret_data" >"$gws_credentials_file"
  fi
  export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE="$gws_credentials_file"
fi

cloud_name=''
api_key=''
api_secret=''
if [[ -n "$cloudinary_secret_data" ]]; then
  cloud_name="$(jq -er --arg key "$VAULT_CLOUDINARY_CLOUD_NAME_KEY" '.[$key] // empty' <<<"$cloudinary_secret_data")"
  api_key="$(jq -er --arg key "$VAULT_CLOUDINARY_API_KEY_KEY" '.[$key] // empty' <<<"$cloudinary_secret_data")"
  api_secret="$(jq -er --arg key "$VAULT_CLOUDINARY_API_SECRET_KEY" '.[$key] // empty' <<<"$cloudinary_secret_data")"
fi

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
